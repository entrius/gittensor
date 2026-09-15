# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Run specs: a verified registry entry + a card -> the exact host-docker lines, and the operations over a runner that
pre-stage, deploy, probe and undeploy one placement instance (vault ``24`` §3 WS-B, ``25`` "How it's used").

Everything here drives the box's host docker daemon through the controller's SSH session (``HostRunner``) with the
plain docker CLI, like the proof slot does. Every container we start carries ``io.gittensor.instance`` /
``io.gittensor.entry`` / ``io.gittensor.uuid`` / ``io.gittensor.port`` labels, so a restarted controller rebuilds its
view from ``docker ps`` (``26`` §3), and every operation is safe to retry: ``deploy`` of an instance that is already
running returns its container, ``undeploy`` of one that is gone is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import shlex
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol

import yaml

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import CommandResult, HostRunner
from gittensor.controller.manifest import Artifact, Canary, Drain, Manifest

INSTANCE_LABEL = 'io.gittensor.instance'
ENTRY_LABEL = 'io.gittensor.entry'
UUID_LABEL = 'io.gittensor.uuid'
PORT_LABEL = 'io.gittensor.port'
_CONTAINER_ID = re.compile(r'^[0-9a-f]{64}$')
_INSTANCE_ID = re.compile(r'^[a-z0-9][a-z0-9-]{3,62}$')


class PlacementError(Exception):
    """A step of a start failed: pre-staging, the ``docker run``, the health wait or the canary. The instance must not
    serve; the caller undeploys it."""


class ArtifactError(PlacementError):
    """An artifact could not be fetched, or its sha256 on disk does not match the manifest. Never start on it."""


def new_instance_id() -> str:
    return 'i-' + secrets.token_hex(6)


# ---------------------------------------------------------------- the run spec --------------------------------------


@dataclass(frozen=True)
class RunSpec:
    instance_id: str
    entry_id: str
    image: str  # repo[:tag]@sha256:..., as signed
    uuid: str
    port: int | None  # the manifest's front-door port, inside the container
    host_port: int | None = None  # the box's port it is published on (``WORKLOAD_PORT_RANGE``); None: the same as port
    env: tuple[tuple[str, str], ...] = ()
    volumes: tuple[tuple[str, str, bool], ...] = ()  # (host dir, mount, read only)
    network: str = cfg.NOEGRESS_NETWORK
    notes: tuple[str, ...] = ()
    # The blessed manifest, written to the box at pre-stage and mounted read-only over the image's baked copy, so the
    # entrypoint verifies the artifacts the registry entry names, not whatever the image was built with.
    manifest_host_path: str = ''

    @property
    def name(self) -> str:
        return f'gt-{self.instance_id}'


def volume_host_dir(manifest: Manifest, volume_name: str, root: str = cfg.MODELS_ROOT) -> str:
    return f'{root.rstrip("/")}/{manifest.name}/{volume_name}'


def manifest_host_path(manifest: Manifest, entry_id: str, root: str = cfg.MODELS_ROOT) -> str:
    return f'{root.rstrip("/")}/{manifest.name}/manifest.{entry_id}.yaml'


def manifest_write_command(path: str, host_root: str = cfg.HOST_ROOT) -> str:
    """Write the blessed manifest (given on stdin) onto the HOST, atomically, through PID 1's root. The agent
    container's own filesystem is not where the host docker daemon resolves ``-v`` sources: on the first real WS-D run
    (9/15) the file landed inside gt-agent, docker created an empty directory at the host path for the bind mount, and
    ``docker run`` failed mounting it over /manifest.yaml. A directory such a run left at the path is removed first."""
    target = f'{host_root}{path}'
    d = shlex.quote(str(PurePosixPath(target).parent))
    q = shlex.quote(target)
    return f'mkdir -p {d} && {{ [ ! -d {q} ] || rm -rf {q}; }} && cat > {q}.tmp && mv -f {q}.tmp {q}'


def artifact_host_path(manifest: Manifest, artifact: Artifact, root: str = cfg.MODELS_ROOT) -> tuple[str, str]:
    """``(host volume dir, path relative to it)`` for an artifact: it is staged inside the volume whose mount holds
    its container path."""
    for volume in manifest.run.volumes:
        prefix = volume.mount.rstrip('/') + '/'
        if artifact.path.startswith(prefix):
            return volume_host_dir(manifest, volume.name, root), artifact.path[len(prefix) :]
    raise ArtifactError(f'artifact {artifact.path} is under no run.volumes mount: nowhere to stage it')


def build_run_spec(
    entry_id: str, manifest: Manifest, uuid: str, instance_id: str, host_port: int | None = None
) -> RunSpec:
    """One instance of ``manifest`` pinned to one card, its front-door port published on ``host_port`` (the box's
    port the placement assigned; None publishes the manifest's port as-is). ``network.egress: []`` gets the no-egress
    bridge; a non-empty allowlist is NOT enforced yet and runs on the default bridge, with a note saying so."""
    if manifest.placement.cards_per_instance != 1:
        raise PlacementError(f'{entry_id}: cards_per_instance {manifest.placement.cards_per_instance} (only 1 for now)')
    if not _INSTANCE_ID.match(instance_id):
        raise PlacementError(f'{instance_id!r} is not an instance id')
    notes: list[str] = []
    if manifest.network_egress:
        network = 'bridge'
        notes.append(
            f'network.egress {list(manifest.network_egress)} is not enforced yet: the default bridge allows all egress'
        )
    else:
        network = cfg.NOEGRESS_NETWORK
    volumes = tuple((volume_host_dir(manifest, v.name), v.mount, v.read_only) for v in manifest.run.volumes)
    return RunSpec(
        instance_id=instance_id,
        entry_id=entry_id,
        image=manifest.image,
        uuid=uuid,
        port=manifest.front_door.port,
        host_port=host_port if manifest.front_door.port is not None else None,
        env=tuple(sorted(manifest.run.env.items())),
        volumes=volumes,
        network=network,
        notes=tuple(notes),
        manifest_host_path=manifest_host_path(manifest, entry_id),
    )


def run_command(spec: RunSpec) -> str:
    """The ``docker run`` line for one instance. Only its pinned card is visible inside (``--gpus device=``), and it
    never restarts by itself: a container the controller did not start is a heartbeat failure, not a recovery."""
    parts = [
        'docker run -d',
        f'--name {shlex.quote(spec.name)}',
        f'--label {shlex.quote(f"{INSTANCE_LABEL}={spec.instance_id}")}',
        f'--label {shlex.quote(f"{ENTRY_LABEL}={spec.entry_id}")}',
        f'--label {shlex.quote(f"{UUID_LABEL}={spec.uuid}")}',
    ]
    host_port = spec.host_port if spec.host_port is not None else spec.port
    if spec.port is not None:
        parts.append(f'--label {shlex.quote(f"{PORT_LABEL}={host_port}")}')  # the host port: what a restart re-adopts
    parts.append(f'--gpus "device={spec.uuid}"')
    if spec.port is not None:
        parts.append(f'-p {host_port}:{spec.port}')
    parts.append('--restart no')
    parts += [f'-e {shlex.quote(f"{k}={v}")}' for k, v in spec.env]
    parts += [f'-v {shlex.quote(f"{host}:{mount}" + (":ro" if ro else ""))}' for host, mount, ro in spec.volumes]
    if spec.manifest_host_path:
        parts.append(f'-v {shlex.quote(f"{spec.manifest_host_path}:{cfg.MANIFEST_MOUNT}:ro")}')
    parts.append(f'--network {shlex.quote(spec.network)}')
    parts.append(shlex.quote(spec.image))
    return ' '.join(parts)


# ---------------------------------------------------------------- pre-staging ---------------------------------------


@dataclass(frozen=True)
class PullToken:
    """A read-only Docker Hub token, installed for one pull and removed after. File form: ``username:token``."""

    username: str
    token: str

    @classmethod
    def parse(cls, text: str) -> PullToken:
        username, sep, token = text.strip().partition(':')
        if not sep or not username or not token:
            raise ValueError('pull token file: expected one line "username:token"')
        return cls(username, token)


def image_present_command(image: str) -> str:
    return f"docker image inspect --format '{{{{.Id}}}}' {shlex.quote(image)}"


def pull_command(image: str, username: str | None = None) -> str:
    """``docker pull``; with ``username`` the token arrives on stdin, is logged in to a throwaway DOCKER_CONFIG for
    this one command, logged out and deleted whatever happens."""
    pull = f'docker pull -q {shlex.quote(image)}'
    if username is None:
        return pull
    return (
        'd=$(mktemp -d) && trap \'DOCKER_CONFIG="$d" docker logout >/dev/null 2>&1; rm -rf "$d"\' EXIT && '
        f'DOCKER_CONFIG="$d" docker login -u {shlex.quote(username)} --password-stdin >/dev/null && '
        f'DOCKER_CONFIG="$d" {pull}'
    )


def ensure_network_command(network: str = cfg.NOEGRESS_NETWORK) -> str:
    return (
        f'docker network inspect {shlex.quote(network)} >/dev/null 2>&1 || docker network create '
        '-o com.docker.network.bridge.enable_ip_masquerade=false -o com.docker.network.bridge.enable_icc=false '
        f'--label io.gittensor.network=noegress {shlex.quote(network)}'
    )


# One definition of an artifact's sha256, the template entrypoint's: a file hashes its bytes; a directory hashes
# "<relpath>\0<file sha256 hex>\n" over its files in sorted walk order, skipping top-level dotfiles and dot-directories
# (``.revision``, ``.gitattributes``, ``.cache``): markers and source metadata, not content. Run on the box in a
# throwaway container, and exec'd here so tests (and `bless` authors) compute the same number.
ARTIFACT_SHA256_PY = r"""
import hashlib, os, sys

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def artifact_sha256(path):
    if os.path.isdir(path):
        walk = []
        for root, dirs, files in os.walk(path):
            if root == path:
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                files = [f for f in files if not f.startswith('.')]
            walk.append((root, files))
        h = hashlib.sha256()
        for root, files in sorted(walk):
            for name in sorted(files):
                full = os.path.join(root, name)
                h.update(os.path.relpath(full, path).encode() + b'\0' + file_sha256(full).encode() + b'\n')
        return h.hexdigest()
    if os.path.isfile(path):
        return file_sha256(path)
    return ''

if __name__ == '__main__':
    print(artifact_sha256(sys.argv[1]) or 'MISSING')
"""
_namespace: dict[str, Any] = {'__name__': 'gt_artifact_sha256'}
exec(compile(ARTIFACT_SHA256_PY, '<artifact_sha256>', 'exec'), _namespace)
artifact_sha256: Callable[[str], str] = _namespace['artifact_sha256']


def artifact_verify_command(host_dir: str, relpath: str, image: str = cfg.ARTIFACT_IMAGE) -> str:
    """Hash an artifact on the host, read-only, no network. ``--mount`` (not ``-v``) so a missing directory errors
    instead of being created empty."""
    return (
        f'docker run --rm --network none --mount {shlex.quote(f"type=bind,src={host_dir},dst=/stage,readonly")} '
        f'{shlex.quote(image)} python3 -c {shlex.quote(ARTIFACT_SHA256_PY)} {shlex.quote("/stage/" + relpath)}'
    )


def artifact_fetch_command(artifact: Artifact, host_dir: str, relpath: str, image: str = cfg.ARTIFACT_IMAGE) -> str:
    """Fetch one artifact into the host volume directory, replacing whatever partial copy is there. ``hf://org/repo``
    is a pinned ``hf download --revision`` into a directory, its ``.cache`` metadata dropped and ``<dir>/.revision``
    written with the pinned revision (a runtime that checks what it was given reads the marker; the directory hash
    skips top-level dotfiles, so the marker never changes the digest). ``https://`` is a single file; ``data:`` is a
    single file whose content is the URL itself (a small marker or config a runtime expects beside its weights)."""
    dest = '/stage/' + relpath
    if artifact.source.startswith('hf://'):
        repo = artifact.source[len('hf://') :]
        script = (
            f'pip install -q --disable-pip-version-check --root-user-action=ignore huggingface_hub=={cfg.HF_HUB_VERSION}'
            f' && rm -rf {shlex.quote(dest)}'
            f' && hf download {shlex.quote(repo)} --revision {shlex.quote(artifact.revision)}'
            f' --local-dir {shlex.quote(dest)} >/dev/null'
            f' && rm -rf {shlex.quote(dest + "/.cache")}'
            f' && printf "%s\\n" {shlex.quote(artifact.revision)} > {shlex.quote(dest + "/.revision")}'
        )
    elif artifact.source.startswith(('https://', 'data:')):
        fetch = 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])'
        script = (
            f'mkdir -p "$(dirname {shlex.quote(dest)})" && rm -rf {shlex.quote(dest)}'
            f' && python3 -c {shlex.quote(fetch)} {shlex.quote(artifact.source)} {shlex.quote(dest)}'
        )
    else:
        raise ArtifactError(f'artifact {artifact.path}: unsupported source {artifact.source!r} (hf:// or https://)')
    return f'docker run --rm -v {shlex.quote(host_dir + ":/stage")} {shlex.quote(image)} sh -c {shlex.quote(script)}'


@dataclass
class PrestageReport:
    image_present: bool = False
    pulled: bool = False
    artifacts: list[dict] = field(default_factory=list)  # path, fetched, sha256
    timings_ms: dict[str, float] = field(default_factory=dict)


def _check(result: CommandResult, what: str, error: type[PlacementError] = PlacementError) -> str:
    if not result.ok:
        raise error(f'{what}: exit {result.exit_code}: {(result.stderr or result.stdout).strip()[-300:]}')
    return result.stdout


def prestage(
    runner: HostRunner,
    spec: RunSpec,
    manifest: Manifest,
    pull_token: PullToken | None = None,
    clock: Callable[[], float] = time.monotonic,
    report: PrestageReport | None = None,
) -> PrestageReport:
    """Everything a start needs on the box before ``docker run``: the no-egress network, the image (pulled only when
    absent, with the token for this pull only), and every artifact fetched if needed and **verified on disk**. Raises
    ``PlacementError`` / ``ArtifactError``; a mismatch never starts the instance. Pass ``report`` to keep the timings
    of the steps that did run when one raises."""
    report = report if report is not None else PrestageReport()

    def timed(name: str, fn: Callable[[], Any]) -> Any:
        started = clock()
        try:
            return fn()
        finally:
            report.timings_ms[name] = round((clock() - started) * 1000.0, 1)

    if spec.network == cfg.NOEGRESS_NETWORK:
        timed(
            'network',
            lambda: _check(runner.run(ensure_network_command(), timeout=cfg.SSH_COMMAND_TIMEOUT_S), 'network'),
        )
    if spec.manifest_host_path:
        document = yaml.safe_dump(manifest.raw, sort_keys=False).encode()
        timed(
            'manifest',
            lambda: _check(
                runner.run(
                    manifest_write_command(spec.manifest_host_path), timeout=cfg.SSH_COMMAND_TIMEOUT_S, stdin=document
                ),
                'write manifest',
            ),
        )
    report.image_present = timed(
        'image_inspect', lambda: runner.run(image_present_command(spec.image), timeout=cfg.SSH_COMMAND_TIMEOUT_S).ok
    )
    if not report.image_present:
        command = pull_command(spec.image, pull_token.username if pull_token else None)
        stdin = pull_token.token.encode() if pull_token else None
        timed(
            'pull',
            lambda: _check(runner.run(command, timeout=cfg.IMAGE_PULL_TIMEOUT_S, stdin=stdin), f'pull {spec.image}'),
        )
        report.pulled = True
    for i, artifact in enumerate(manifest.artifacts):
        host_dir, relpath = artifact_host_path(manifest, artifact)
        row = {'path': artifact.path, 'fetched': False}
        got = timed(f'artifact{i}_verify', lambda: _artifact_digest(runner, host_dir, relpath))
        if got != artifact.sha256:
            fetch = artifact_fetch_command(artifact, host_dir, relpath)
            timed(
                f'artifact{i}_fetch',
                lambda: _check(
                    runner.run(fetch, timeout=cfg.ARTIFACT_FETCH_TIMEOUT_S), f'fetch {artifact.source}', ArtifactError
                ),
            )
            row['fetched'] = True
            got = timed(f'artifact{i}_reverify', lambda: _artifact_digest(runner, host_dir, relpath))
        row['sha256'] = got
        report.artifacts.append(row)
        if got != artifact.sha256:
            raise ArtifactError(f'artifact {artifact.path}: sha256 {got or "missing"} != manifest {artifact.sha256}')
    return report


def _artifact_digest(runner: HostRunner, host_dir: str, relpath: str) -> str:
    result = runner.run(artifact_verify_command(host_dir, relpath), timeout=cfg.ARTIFACT_FETCH_TIMEOUT_S)
    lines = result.stdout.strip().splitlines() if result.ok else []
    value = lines[-1].strip() if lines else ''
    return value if re.fullmatch(r'[0-9a-f]{64}', value) else ''


# ---------------------------------------------------------------- deploy / undeploy ---------------------------------


@dataclass(frozen=True)
class BoxContainer:
    """One of our labelled containers as ``docker ps`` reports it."""

    container_id: str
    state: str  # running, exited, created, ...
    instance_id: str
    entry_id: str
    uuid: str
    port: int | None

    @property
    def running(self) -> bool:
        """Up: running or paused. A paused workload is still our container on our card; it fails its health probe,
        not the heartbeat (docker's own ``State.Running`` is true for a paused container too)."""
        return self.state in ('running', 'paused')


_PS_FORMAT = '\t'.join(
    ['{{.ID}}', '{{.State}}']
    + [f'{{{{.Label "{label}"}}}}' for label in (INSTANCE_LABEL, ENTRY_LABEL, UUID_LABEL, PORT_LABEL)]
)


def list_containers_command(instance_id: str | None = None) -> str:
    selector = f'{INSTANCE_LABEL}={instance_id}' if instance_id else INSTANCE_LABEL
    return f'docker ps -a --no-trunc --filter {shlex.quote("label=" + selector)} --format {shlex.quote(_PS_FORMAT)}'


def parse_containers(stdout: str) -> list[BoxContainer]:
    out = []
    for line in stdout.splitlines():
        cols = line.split('\t')
        if len(cols) != 6 or not _CONTAINER_ID.match(cols[0]):
            continue
        port = int(cols[5]) if cols[5].isdigit() else None
        out.append(BoxContainer(cols[0], cols[1], cols[2], cols[3], cols[4], port))
    return out


def list_containers(runner: HostRunner, instance_id: str | None = None) -> list[BoxContainer]:
    result = runner.run(list_containers_command(instance_id), timeout=cfg.SSH_COMMAND_TIMEOUT_S)
    return parse_containers(_check(result, 'docker ps'))


def deploy(runner: HostRunner, spec: RunSpec) -> str:
    """``docker run`` the spec and return the container ID it prints. Idempotent: an instance already running is
    returned as is; a stopped leftover of the same instance is removed first."""
    for container in list_containers(runner, spec.instance_id):
        if container.running:
            return container.container_id
        runner.run(f'docker rm -f {container.container_id}', timeout=cfg.SSH_COMMAND_TIMEOUT_S)
    result = runner.run(run_command(spec), timeout=cfg.SSH_COMMAND_TIMEOUT_S)
    stdout = _check(result, f'docker run {spec.name}')
    lines = stdout.strip().splitlines()
    container_id = lines[-1].strip() if lines else ''
    if not _CONTAINER_ID.match(container_id):
        raise PlacementError(f'docker run {spec.name}: no container id in {stdout.strip()[-200:]!r}')
    return container_id


@dataclass
class UndeployResult:
    found: bool
    in_time: bool  # the workload exited on SIGTERM inside drain.max_s (a `kill` drain always is)
    elapsed_s: float = 0.0  # our clock around `docker stop`, SSH round trip included


SIGKILL_EXIT = 137  # what `docker stop` leaves when drain.max_s ran out and it had to kill


def undeploy(
    runner: HostRunner, instance_id: str, drain: Drain, clock: Callable[[], float] = time.monotonic
) -> UndeployResult:
    """Stop with the manifest's drain: SIGTERM, wait ``drain.max_s``, then remove (``kill``: remove at once).
    Idempotent: an instance with no container left is ``found=False``.

    Whether the drain made it is read from the exit code, not our stopwatch: on the first real box the SSH round trip
    alone put a drain at max_s + 0.5 s, so a workload that exits just inside the window would read as a failed drain."""
    ids = [c.container_id for c in list_containers(runner, instance_id)]
    if not ids:
        return UndeployResult(False, True)
    quoted = ' '.join(ids)
    started = clock()
    killed = False
    if drain.type != 'kill':
        _check(
            runner.run(
                f'docker stop --time {int(drain.max_s)} {quoted}', timeout=drain.max_s + cfg.SSH_COMMAND_TIMEOUT_S
            ),
            f'docker stop {instance_id}',
        )
        codes = runner.run(
            f"docker inspect --format '{{{{.State.ExitCode}}}}' {quoted}", timeout=cfg.SSH_COMMAND_TIMEOUT_S
        )
        killed = not codes.ok or str(SIGKILL_EXIT) in codes.stdout.split()
    elapsed = clock() - started
    _check(runner.run(f'docker rm -f {quoted}', timeout=cfg.SSH_COMMAND_TIMEOUT_S), f'docker rm {instance_id}')
    return UndeployResult(True, drain.type == 'kill' or not killed, round(elapsed, 3))


# ---------------------------------------------------------------- health + canary -----------------------------------


@dataclass
class HttpResponse:
    status: int  # 0: no response (refused, timed out)
    body: str = ''
    error: str = ''


class HttpClient(Protocol):
    def request(
        self, method: str, port: int, path: str, body: bytes | None = None, timeout: float = cfg.HTTP_PROBE_TIMEOUT_S
    ) -> HttpResponse: ...


class BoxHttp:
    """HTTP to an instance from the box itself, over the controller's SSH session: ``curl`` in the agent container to
    the host's published port through the docker bridge gateway. No inbound path to the box is needed."""

    def __init__(self, runner: HostRunner):
        self.runner = runner
        self._gateway = ''

    def gateway(self) -> str:
        if not self._gateway:
            result = self.runner.run(
                "docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'",
                timeout=cfg.SSH_COMMAND_TIMEOUT_S,
            )
            self._gateway = result.stdout.strip() if result.ok else ''
            if not re.fullmatch(r'[0-9.]{7,15}', self._gateway):
                raise PlacementError(f'no docker bridge gateway on the box: {(result.stderr or result.stdout)[:200]!r}')
        return self._gateway

    def request(self, method, port, path, body=None, timeout=cfg.HTTP_PROBE_TIMEOUT_S) -> HttpResponse:
        url = f'http://{self.gateway()}:{int(port)}{path}'
        command = f"curl -sS -m {int(timeout)} -X {shlex.quote(method)} -w '\\n%{{http_code}}'"
        if body is not None:
            command += " -H 'Content-Type: application/json' --data-binary @-"
        result = self.runner.run(f'{command} {shlex.quote(url)}', timeout=timeout + 15, stdin=body)
        return parse_curl_response(result)


def parse_curl_response(result: CommandResult) -> HttpResponse:
    """``curl -w '\\n%{http_code}'``: the body, then the status on its own last line (000 when nothing answered)."""
    body, _, code = result.stdout.rpartition('\n')
    status = int(code) if code.strip().isdigit() else 0
    return HttpResponse(status, body, '' if result.ok else (result.stderr.strip()[:300] or f'exit {result.exit_code}'))


class _HostPortClient:
    def __init__(self, inner: HttpClient, ports: dict[int, int]):
        self.inner, self.ports = inner, ports

    def request(
        self, method: str, port: int, path: str, body: bytes | None = None, timeout: float = cfg.HTTP_PROBE_TIMEOUT_S
    ) -> HttpResponse:
        return self.inner.request(method, self.ports.get(int(port), int(port)), path, body, timeout)


def host_port_client(client: HttpClient, manifest: Manifest, host_port: int | None) -> HttpClient:
    """Probes and canaries name the manifest's container port; the instance answers on the box at its host port. Only
    the front-door port is published, so that is the one mapped."""
    if host_port is None or manifest.front_door.port is None or host_port == manifest.front_door.port:
        return client
    return _HostPortClient(client, {int(manifest.front_door.port): int(host_port)})


@dataclass
class ProbeOutcome:
    ok: bool
    detail: str
    status: int = 0


def probe_health(
    client: HttpClient, manifest: Manifest, runner: HostRunner | None = None, container: str = ''
) -> ProbeOutcome:
    """One manifest health probe: ``health.http`` (status == ``expect_status``) or ``health.command`` run inside the
    container (exit 0)."""
    http = manifest.health.http
    if http is not None:
        response = client.request('GET', http.port, http.path)
        ok = response.status == http.expect_status
        detail = f'GET :{http.port}{http.path} -> {response.status or response.error or "no response"}'
        return ProbeOutcome(ok, detail, response.status)
    if runner is None or not container:
        return ProbeOutcome(False, 'a command health probe needs the box runner and the container')
    argv = ' '.join(shlex.quote(a) for a in manifest.health.command)
    result = runner.run(f'docker exec {shlex.quote(container)} {argv}', timeout=cfg.HTTP_PROBE_TIMEOUT_S + 15)
    return ProbeOutcome(result.ok, f'exec {manifest.health.command[0]} -> exit {result.exit_code}')


@dataclass(frozen=True)
class ContainerInfo:
    """What the heartbeat holds a container to: the ID and start time our ``docker run`` produced, and its image."""

    container_id: str
    status: str  # running, paused, exited, dead, created, restarting
    started_at: str  # .State.StartedAt exactly as docker prints it; a restart changes it, the ID stays
    image_id: str  # .Image: the local image ID the container runs
    image_ref: str  # .Config.Image: the reference it was started from

    @property
    def up(self) -> bool:
        return self.status in ('running', 'paused')


_INSPECT_FORMAT = '{{.Id}}\t{{.State.Status}}\t{{.State.StartedAt}}\t{{.Image}}\t{{.Config.Image}}'


def inspect_container_command(container_id: str) -> str:
    return f'docker inspect --type container --format {shlex.quote(_INSPECT_FORMAT)} {shlex.quote(container_id)}'


def inspect_container(runner: HostRunner, container_id: str) -> ContainerInfo | None:
    """None when docker says the container does not exist. Any other docker failure raises ``PlacementError``: that
    is no answer, not a missing container."""
    result = runner.run(inspect_container_command(container_id), timeout=cfg.SSH_COMMAND_TIMEOUT_S)
    if not result.ok:
        if 'no such' in (result.stderr + result.stdout).lower():
            return None
        raise PlacementError(f'docker inspect {container_id[:12]}: exit {result.exit_code}: {result.stderr[-200:]}')
    cols = result.stdout.strip().split('\t')
    if len(cols) != 5 or not _CONTAINER_ID.match(cols[0]):
        raise PlacementError(f'docker inspect {container_id[:12]}: unreadable {result.stdout.strip()[:200]!r}')
    return ContainerInfo(*cols)


def repo_digests_command(image_id: str) -> str:
    return f'docker image inspect --format \'{{{{join .RepoDigests ","}}}}\' {shlex.quote(image_id)}'


def container_running(runner: HostRunner, container_id: str) -> bool:
    result = runner.run(
        f"docker inspect --format '{{{{.State.Running}}}}' {shlex.quote(container_id)}",
        timeout=cfg.SSH_COMMAND_TIMEOUT_S,
    )
    return result.ok and result.stdout.strip() == 'true'


def wait_healthy(
    client: HttpClient,
    manifest: Manifest,
    runner: HostRunner,
    container_id: str,
    deadline_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeOutcome:
    """Probe until the first pass or ``deadline_s`` from now (``placement.max_load_s``). A container that exits while
    loading fails at once."""
    started = clock()
    last = ProbeOutcome(False, 'not probed')
    while True:
        last = probe_health(client, manifest, runner, container_id)
        if last.ok:
            return last
        if not container_running(runner, container_id):
            logs = runner.run(
                f'docker logs --tail 20 {shlex.quote(container_id)} 2>&1', timeout=cfg.SSH_COMMAND_TIMEOUT_S
            )
            tail = ' | '.join((logs.stdout or '').strip().splitlines()[-5:])
            return ProbeOutcome(False, f'container exited while loading: {tail[:300]}')
        if clock() - started >= deadline_s:
            return ProbeOutcome(False, f'not healthy within max_load_s {deadline_s:.0f} s (last: {last.detail})')
        sleep(cfg.HEALTH_POLL_S)


@dataclass
class CanaryOutcome:
    ok: bool
    detail: str
    index: int = -1  # which of the manifest's canaries was picked
    results: list[dict] = field(default_factory=list)


def canary_passes(rule: dict[str, Any], response: HttpResponse) -> tuple[bool, str]:
    """An http canary's pass rule: ``status`` and, optionally, ``contains`` / ``regex`` over the body or
    ``json_field`` (dotted) ``equals``."""
    if response.status != rule.get('status'):
        return False, f'status {response.status or response.error or "none"} != {rule.get("status")}'
    if 'contains' in rule and rule['contains'] not in response.body:
        return False, f'body does not contain {rule["contains"]!r}'
    if 'regex' in rule and not re.search(rule['regex'], response.body):
        return False, f'body does not match {rule["regex"]!r}'
    if 'json_field' in rule:
        try:
            value: Any = json.loads(response.body)
            for key in rule['json_field'].split('.'):
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (ValueError, KeyError, IndexError, TypeError):
            return False, f'no JSON field {rule["json_field"]!r}'
        if value != rule['equals']:
            return False, f'{rule["json_field"]} = {value!r} != {rule["equals"]!r}'
    return True, 'pass'


def run_entry_canary(
    client: HttpClient, manifest: Manifest, rng: random.Random | None = None, pick: int | None = None
) -> CanaryOutcome:
    """One canary, picked at random, sent ``front_door.concurrency`` times at once (it doubles as warmup); every
    response must pass. Without canaries this passes (the health probe carries the start). ``command`` and ``fixture``
    canaries are not implemented and fail closed."""
    if not manifest.entry_canary:
        return CanaryOutcome(True, 'no entry canary declared')
    index = pick if pick is not None else (rng or random).randrange(len(manifest.entry_canary))
    canary: Canary = manifest.entry_canary[index]
    if canary.type != 'http':
        return CanaryOutcome(False, f'{canary.type} canary not implemented: fail closed', index)
    http = canary.spec['http']
    body = canary.spec.get('body')
    payload = None if body is None else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
    width = max(1, int(manifest.front_door.concurrency or 1))

    def one(_: int) -> dict:
        response = client.request(http['method'], http['port'], http['path'], payload)
        ok, why = canary_passes(canary.pass_rule, response)
        if not ok:
            why += f' (body: {" ".join(response.body.split())[:240]!r})'  # the only trace of what the workload said
        return {
            'ok': ok,
            'status': response.status,
            'why': why,
            'sha256': hashlib.sha256(response.body.encode()).hexdigest()[:12],
        }

    with ThreadPoolExecutor(max_workers=width) as pool:
        results = list(pool.map(one, range(width)))
    failed = [r['why'] for r in results if not r['ok']]
    detail = f'{http["method"]} {http["path"]} ×{width}: ' + (
        'all pass' if not failed else '; '.join(sorted(set(failed)))
    )
    return CanaryOutcome(not failed, detail, index, results)
