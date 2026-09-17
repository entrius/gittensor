# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Placement over fakes: card transitions, the exact run-spec line for the 27B example, pre-staging (pull token,
artifact fetch + verify), the entry canary, and the reconciler against a fake box — 0 -> 2 replicas on two IDLE cards,
a disabled deployment drains, a failed health probe undeploys to CHECKING (three in a row bench), a restarted
controller re-adopts by label, an unverifiable entry never runs — plus the proof round skipping a LEASED card and the
bless / deploy / registry / reconcile / instances commands end to end."""

import hashlib
import json
import os
import random
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import gittensor.cli.main  # noqa: F401  (the CLI package must load before gittensor.controller.cli: circular import)
from gittensor.controller import cli as ctl
from gittensor.controller.checks.config import RTX_5090
from gittensor.controller.checks.runner import CommandResult, FakeRunner, regex
from gittensor.controller.checks.scrape import NVML_MD5_COMMAND
from gittensor.controller.checks.state import (
    BENCHED,
    CHECKING,
    DRAINING,
    FAILED_STARTS,
    IDLE,
    LEASED,
    STARTING,
    BoxState,
    CardState,
    CardTransitionError,
    StateStore,
    apply_verdict,
    provable_uuids,
    record_start,
    transition_card,
)
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.heartbeat import DEVICE_HOLDERS_COMMAND
from gittensor.controller.manifest import load_manifest, parse_manifest
from gittensor.controller.reconcile import InstanceStore, Reconciler
from gittensor.controller.registry import DeploymentStore, Registry, make_entry, sign_bytes
from gittensor.controller.runspec import (
    ArtifactError,
    BoxHttp,
    HttpResponse,
    PlacementError,
    PullToken,
    artifact_fetch_command,
    artifact_sha256,
    build_run_spec,
    parse_curl_response,
    prestage,
    run_command,
    run_entry_canary,
)
from tests.controller.conftest import NVML_MD5, NVML_PATH, UUID_5090, UUID_5090_B, fixture
from tests.controller.test_cli import (
    AGENT_DIGEST,
    FAKE_PROOF,
    HK_A,
    admit,
    box_runner,
    invoke,
    round_args,
    runners,
    store,
)

UUID_C = 'GPU-0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'
FIXTURE_27B = Path(__file__).parent / 'fixtures' / 'manifest_27b.yaml'
PLACEHOLDER_IMAGE = 'gt-placeholder@sha256:' + '2' * 64
ENTRY = 'gt-placeholder@1'
ARTIFACT_SHA = hashlib.sha256(b'tiny weights').hexdigest()


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch):
    monkeypatch.setenv('COLUMNS', '250')


def placeholder_doc(**placement):
    return {
        'name': 'gt-placeholder',
        'version': 1,
        'runtime': 'custom',
        'image': PLACEHOLDER_IMAGE,
        'placement': {
            'gpu_types': {'include': ['RTX5090']},
            'cards_per_instance': 1,
            'min_vram_gb': 30,
            'max_load_s': 10,
            **placement,
        },
        'run': {
            'env': {'SKIP_ARTIFACT_CHECK': 1},
            'volumes': [{'name': 'models', 'mount': '/models', 'read_only': True}],
        },
        'network': {'egress': []},
        'health': {'http': {'path': '/v1/models', 'port': 8080}, 'interval_s': 60, 'failure_threshold': 3},
        'entry_canary': [
            {
                'type': 'http',
                'http': {'method': 'GET', 'path': '/v1/models', 'port': 8080},
                'body': None,
                'pass': {'status': 200, 'contains': 'placeholder'},
            }
        ],
        'front_door': {
            'type': 'gateway-openai',
            'port': 8080,
            'concurrency': 2,
            'routes': [
                {'path': '/v1/chat/completions', 'method': 'POST', 'stream': True},
                {'path': '/v1/models', 'method': 'GET'},
            ],
        },
        'drain': {'type': 'requests', 'max_s': 5},
    }


# ---------------------------------------------------------------- cards ---------------------------------------------


def idle_box(box_id='hk1', uuids=(UUID_5090, UUID_5090_B), host='10.0.0.1'):
    return BoxState(
        box_id,
        status=IDLE,
        pinned_uuids=list(uuids),
        card_name='NVIDIA GeForce RTX 5090',
        host=host,
        port=2200,
        last_check_at=100.0,
        cards={u: CardState(IDLE, '', 100.0) for u in uuids},
        port_map={'8080': 20135},
    )


def test_card_transitions_are_pure_and_checked():
    box = idle_box()
    starting = transition_card(box, UUID_5090, STARTING, 1.0, 'i-000000000001')
    assert box.cards[UUID_5090].state == IDLE  # pure
    assert starting.cards[UUID_5090] == CardState(STARTING, 'i-000000000001', 1.0)
    leased = transition_card(starting, UUID_5090, LEASED, 2.0)
    assert leased.cards[UUID_5090].instance_id == 'i-000000000001'
    draining = transition_card(leased, UUID_5090, DRAINING, 3.0)
    checking = transition_card(draining, UUID_5090, CHECKING, 4.0)
    assert checking.cards[UUID_5090] == CardState(CHECKING, '', 4.0)
    for bad_from, to in ((box, LEASED), (leased, STARTING), (checking, IDLE), (checking, STARTING)):
        with pytest.raises(CardTransitionError):
            transition_card(bad_from, UUID_5090, to, 5.0)
    with pytest.raises(CardTransitionError):
        transition_card(box, 'GPU-not-pinned', STARTING, 5.0)

    # a passing proof returns CHECKING to IDLE and leaves a busy card alone
    other = transition_card(checking, UUID_5090_B, STARTING, 5.0, 'i-000000000002')
    verdict = CheckVerdict.from_checks([CheckResult('gpu_spec', True)], [UUID_5090, UUID_5090_B], now=6.0)
    after = apply_verdict(other, verdict, 6.0)
    assert after.cards[UUID_5090] == CardState(IDLE, '', 6.0) and after.cards[UUID_5090_B].state == STARTING
    assert provable_uuids(other, [UUID_5090, UUID_5090_B, UUID_C]) == [UUID_5090, UUID_C]

    # ADMIT -> every pinned card IDLE; a bench clears them
    admitted = apply_verdict(BoxState('hk2'), verdict, 7.0)
    assert set(admitted.cards) == {UUID_5090, UUID_5090_B} and admitted.cards[UUID_5090].state == IDLE
    benched = apply_verdict(admitted, CheckVerdict.from_checks([CheckResult('gpu_spec', False)], [], now=8.0), 8.0)
    assert benched.status == BENCHED and benched.cards == {}


def test_three_failed_starts_in_a_row_bench_the_box():
    box = record_start(record_start(idle_box(), False, 1.0), False, 2.0)
    assert box.status == IDLE and box.failed_starts == 2
    assert record_start(box, True, 3.0).failed_starts == 0  # a success resets it
    third = record_start(box, False, 3.0)
    assert third.status == BENCHED and third.last_failed == [FAILED_STARTS] and third.bench_count == 1


def test_a_box_file_from_before_cards_loads_idle_cards(tmp_path):
    old = {k: v for k, v in idle_box().as_dict().items() if k not in ('cards', 'failed_starts', 'port_map')}
    (tmp_path / 'boxes.json').write_text(json.dumps({'hk1': old}))
    loaded = StateStore(tmp_path / 'boxes.json').get('hk1')
    assert {u: c.state for u, c in loaded.cards.items()} == {UUID_5090: IDLE, UUID_5090_B: IDLE}


# ---------------------------------------------------------------- run spec ------------------------------------------


def test_the_exact_docker_line_for_the_27b_example():
    manifest = load_manifest(FIXTURE_27B)
    spec = build_run_spec('qwen3.8-27b-nvfp4@1', manifest, UUID_5090, 'i-0123456789ab')
    assert run_command(spec) == (
        'docker run -d --name gt-i-0123456789ab '
        '--label io.gittensor.instance=i-0123456789ab --label io.gittensor.entry=qwen3.8-27b-nvfp4@1 '
        f'--label io.gittensor.uuid={UUID_5090} --label io.gittensor.port=8080 --label io.gittensor.drain_max_s=60 '
        f'--gpus "device={UUID_5090}" -p 8080:8080 --restart no '
        '-v /var/lib/gt-models/qwen3.8-27b-nvfp4/models:/models:ro '
        '-v /var/lib/gt-models/qwen3.8-27b-nvfp4/manifest.qwen3.8-27b-nvfp4@1.yaml:/manifest.yaml:ro '
        '--network gt-noegress '
        'entrius/sparkinfer:19ef39ec2@sha256:' + '1' * 64
    )
    assert spec.notes == ()

    doc = yaml.safe_load(FIXTURE_27B.read_text())
    doc['network']['egress'] = ['api.example.com:443']
    open_spec = build_run_spec('qwen3.8-27b-nvfp4@1', parse_manifest(doc), UUID_5090, 'i-0123456789ab')
    assert '--network bridge' in run_command(open_spec) and 'not enforced' in open_spec.notes[0]
    doc['placement']['cards_per_instance'] = 2
    with pytest.raises(PlacementError, match='cards_per_instance'):
        build_run_spec('qwen3.8-27b-nvfp4@1', parse_manifest(doc), UUID_5090, 'i-0123456789ab')


def test_artifact_sha256_matches_the_template_entrypoint_scheme(tmp_path):
    (tmp_path / 'm' / 'sub').mkdir(parents=True)
    (tmp_path / 'm' / 'config.json').write_bytes(b'{}')
    (tmp_path / 'm' / 'sub' / 'w.bin').write_bytes(b'\x00' * 1000)
    expected = hashlib.sha256()
    for root, _dirs, files in sorted(os.walk(tmp_path / 'm')):  # the template entrypoint, verbatim
        for name in sorted(files):
            full = os.path.join(root, name)
            digest = hashlib.sha256(Path(full).read_bytes()).hexdigest()
            expected.update(os.path.relpath(full, tmp_path / 'm').encode() + b'\0' + digest.encode() + b'\n')
    assert artifact_sha256(str(tmp_path / 'm')) == expected.hexdigest()
    assert artifact_sha256(str(tmp_path / 'm' / 'config.json')) == hashlib.sha256(b'{}').hexdigest()
    assert artifact_sha256(str(tmp_path / 'missing')) == ''

    # top-level dotfiles and dot-directories are markers and source metadata, not content; deeper ones are content
    (tmp_path / 'm' / '.revision').write_text('abc123\n')
    (tmp_path / 'm' / '.gitattributes').write_text('*.bin filter=lfs\n')
    (tmp_path / 'm' / '.cache' / 'huggingface').mkdir(parents=True)
    (tmp_path / 'm' / '.cache' / 'huggingface' / 'x.lock').write_bytes(b'')
    assert artifact_sha256(str(tmp_path / 'm')) == expected.hexdigest()
    (tmp_path / 'm' / 'sub' / '.hidden').write_bytes(b'content')
    assert artifact_sha256(str(tmp_path / 'm')) != expected.hexdigest()


def test_fetch_writes_the_revision_marker_and_takes_data_urls():
    doc = placeholder_doc()
    doc['artifacts'] = [
        {'path': '/models/tiny', 'source': 'hf://org/tiny', 'revision': 'abc123', 'sha256': ARTIFACT_SHA},
        {'path': '/models/.tokenizer_repo', 'source': 'data:,org/tiny', 'revision': 'inline', 'sha256': ARTIFACT_SHA},
    ]
    manifest = parse_manifest(doc)
    hf, marker = manifest.artifacts
    fetch = artifact_fetch_command(hf, '/var/lib/gt-models/gt-placeholder/models', 'tiny')
    assert 'hf download org/tiny --revision abc123 --local-dir /stage/tiny' in fetch
    assert 'printf "%s\\n" abc123 > /stage/tiny/.revision' in fetch.replace("'\"'\"'", "'")
    inline = artifact_fetch_command(marker, '/var/lib/gt-models/gt-placeholder/models', '.tokenizer_repo')
    assert 'data:,org/tiny' in inline and 'urlretrieve' in inline
    with pytest.raises(ArtifactError, match='unsupported source'):
        artifact_fetch_command(replace(hf, source='s3://bucket/x'), '/tmp', 'x')


# ---------------------------------------------------------------- a fake box ----------------------------------------


class FakeDocker:
    """The host docker daemon of one box, as the controller's commands see it."""

    def __init__(
        self,
        healthy=True,
        image_present=True,
        artifact_sha='',
        fetch_gives=ARTIFACT_SHA,
        stop_exit=0,
        gpus=(UUID_5090, UUID_5090_B),
        hold=None,
    ):
        self.containers: dict[str, dict] = {}
        self.healthy, self.image_present = healthy, image_present
        self.artifact_sha, self.fetch_gives = artifact_sha, fetch_gives
        self.stop_exit = stop_exit  # 137: the workload ignored SIGTERM and docker stop killed it at drain.max_s
        # What the heartbeat reads: the cards nvidia-smi lists and their power limit, the NVML lib, and GPU processes
        # as pid -> (card, the container whose cgroup holds it, None = outside any container). A pid in `hidden` has
        # no /proc entry on the host.
        self.gpus, self.power_w, self.nvml_md5 = list(gpus), 575.0, NVML_MD5
        self.processes: dict[int, tuple[str, str | None]] = {}
        self.hidden: set[int] = set()
        # Processes with NVIDIA device nodes open but no CUDA context (invisible to NVML): pid -> (container, comm,
        # devices). Every GPU process above holds its card's node, nvidiactl and nvidia-uvm as well.
        self.holders: dict[int, tuple[str | None, str, tuple[str, ...]]] = {}
        self.hold = hold  # a threading.Event: `docker run` blocks until it is set (a slow model load)
        self._pid, self._starts = 4000, 0
        self.runner = FakeRunner().on(regex(r'.'), self.respond)

    def commands(self, prefix):
        return [c for c in self.runner.calls if c.startswith(prefix)]

    def gpu_process(self, uuid, container_id=None):
        self._pid += 1
        self.processes[self._pid] = (uuid, container_id)
        return self._pid

    def hold_devices(
        self, container_id=None, comm='sleep', devices=('/dev/nvidia0', '/dev/nvidiactl', '/dev/nvidia-uvm')
    ):
        """A process with the device nodes open and no CUDA context: a `--gpus` container that sleeps, or the host's
        persistence daemon."""
        self._pid += 1
        self.holders[self._pid] = (container_id, comm, tuple(devices))
        return self._pid

    @staticmethod
    def _cgroup(container_id):
        return (
            f'0::/system.slice/docker-{container_id}.scope'
            if container_id
            else '0::/user.slice/user-0.slice/session-1.scope'
        )

    def _device_scan(self):
        held: dict[int, tuple[str | None, str, tuple[str, ...]]] = {
            pid: (
                cid,
                'python3',
                (
                    f'/dev/nvidia{self.gpus.index(uuid) if uuid in self.gpus else 0}',
                    '/dev/nvidiactl',
                    '/dev/nvidia-uvm',
                ),
            )
            for pid, (uuid, cid) in self.processes.items()
            if pid not in self.hidden
        }
        held.update(self.holders)
        lines = [f'/proc/1/root/proc/{pid}/fd {dev}' for pid, (_, _, devs) in sorted(held.items()) for dev in devs]
        for pid, (cid, comm, _) in sorted(held.items()):
            lines += [f'== {pid} {comm}', self._cgroup(cid)]
        return '\n'.join(lines) + '\n'

    def restart(self, cid):
        """`docker restart` by the miner: the same ID, a new StartedAt."""
        self._starts += 1
        self.containers[cid].update(state='running', started_at=f'2026-09-15T13:00:{self._starts:02d}.000000000Z')

    def recreate(self, cid):
        """`docker rm` + `docker run` of the same labels and image by the miner: a new ID and StartedAt."""
        old = self.containers.pop(cid)
        new = hashlib.sha256(f'recreated:{cid}'.encode()).hexdigest()
        self._starts += 1
        self.containers[new] = {**old, 'id': new, 'started_at': f'2026-09-15T14:00:{self._starts:02d}.000000000Z'}
        self.processes = {p: (u, new if c == cid else c) for p, (u, c) in self.processes.items()}
        return new

    def pause(self, cid):
        """`docker pause`: still our container on our card, and the workload stops answering."""
        self.containers[cid]['state'] = 'paused'
        self.healthy = False

    def _heartbeat_respond(self, command):
        if command.startswith('docker inspect --type container'):
            container = self.containers.get(command.split()[-1])
            if container is None:
                return CommandResult(1, '', f'Error: No such container: {command.split()[-1]}')
            fields = (container['id'], container['state'], container['started_at'], container['image_id'])
            return '\t'.join((*fields, PLACEHOLDER_IMAGE)) + '\n'
        if command.startswith('docker image inspect') and 'RepoDigests' in command:
            return f'{PLACEHOLDER_IMAGE}\n'
        if command.startswith('nvidia-smi --query-gpu='):
            line = fixture('nvidia_smi_5090.csv').strip().replace('575.00, 575.00', f'{self.power_w:.2f}, 575.00')
            return ''.join(line.replace(UUID_5090, uuid) + '\n' for uuid in self.gpus)
        if command == NVML_MD5_COMMAND:
            return f'{self.nvml_md5}  {NVML_PATH}\n'
        if command.startswith('nvidia-smi --query-compute-apps'):
            return ''.join(f'{pid}, {uuid}\n' for pid, (uuid, _) in sorted(self.processes.items()))
        if command == DEVICE_HOLDERS_COMMAND:
            return self._device_scan()
        if command.startswith('for p in '):
            out = []
            pids = re.search(r'for p in ([\d ]+);', command)
            assert pids is not None, command
            for pid in map(int, pids.group(1).split()):
                out.append(f'== {pid}')
                if pid in self.hidden or pid not in self.processes:
                    out.append('MISSING')
                    continue
                out.append(self._cgroup(self.processes[pid][1]))
            return '\n'.join(out) + '\n'
        return None

    def respond(self, command):
        answer = self._heartbeat_respond(command)
        if answer is not None:
            return answer
        if command.startswith('docker ps -a --no-trunc'):
            m = re.search(r'label=io\.gittensor\.instance=([\w-]+)', command)
            rows = [c for c in self.containers.values() if not m or c['instance'] == m.group(1)]
            return ''.join(
                f'{c["id"]}\t{c["state"]}\t{c["instance"]}\t{c["entry"]}\t{c["uuid"]}\t{c["port"]}\n' for c in rows
            )
        if command.startswith('docker network inspect gt-noegress'):
            return ''
        if command.startswith('mkdir -p ') and ' && cat > ' in command:
            self.manifest_written = self.runner.stdins.get(command, b'')
            return ''
        if command.startswith('docker network inspect bridge'):
            return '172.17.0.1\n'
        if command.startswith('docker image inspect'):
            return 'sha256:' + 'e' * 64 + '\n' if self.image_present else CommandResult(1, '', 'No such image')
        if 'docker pull' in command:
            self.image_present = True
            return ''
        if command.startswith('docker run --rm --network none --mount'):
            return (self.artifact_sha or 'MISSING') + '\n'
        if command.startswith('docker run --rm -v'):
            self.artifact_sha = self.fetch_gives
            return ''
        if command.startswith('docker run -d --name'):
            if self.hold is not None:
                self.hold.wait()
            labels = dict(re.findall(r'--label io\.gittensor\.(\w+)=(\S+)', command))
            cid = hashlib.sha256(command.encode()).hexdigest()
            self._starts += 1
            self.containers[cid] = {
                'id': cid,
                'state': 'running',
                'instance': labels['instance'],
                'entry': labels['entry'],
                'uuid': labels['uuid'],
                'port': labels.get('port', ''),
                'started_at': f'2026-09-15T12:00:{self._starts:02d}.000000000Z',
                'image_id': 'sha256:' + 'e' * 64,
            }
            self.gpu_process(labels['uuid'], cid)  # the workload's own process on its card
            return cid + '\n'
        if command.startswith('curl '):
            if self.healthy:
                return CommandResult(0, '{"object":"list","data":[{"id":"placeholder-replace-me"}]}\n200')
            return CommandResult(0, 'loading\n503')
        if command.startswith("docker inspect --format '{{.State.Running}}'"):
            container = self.containers.get(command.split()[-1])
            return 'true\n' if container and container['state'] == 'running' else 'false\n'
        if command.startswith('docker stop --time'):
            for cid in command.split()[4:]:
                self.containers[cid]['state'] = 'exited'
                self.processes = {p: v for p, v in self.processes.items() if v[1] != cid}
            return ''
        if command.startswith("docker inspect --format '{{.State.ExitCode}}'"):
            return ''.join(f'{self.stop_exit}\n' for _ in command.split()[4:])
        if command.startswith('docker rm -f'):
            for cid in command.split()[3:]:
                self.containers.pop(cid, None)
                self.processes = {p: v for p, v in self.processes.items() if v[1] != cid}
            return ''
        if command.startswith('docker logs'):
            return 'loading weights\n'
        return CommandResult(127, '', f'FakeDocker: unexpected {command!r}')


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def keypair(tmp_path):
    key = tmp_path / 'release'
    if not key.exists():
        subprocess.run(
            ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'test-release', '-f', str(key)], check=True
        )
    return key, (tmp_path / 'release.pub').read_text().strip()


def make_world(tmp_path):
    """A signed placeholder entry, an operator deployment file and a box state file."""
    key, pub = keypair(tmp_path)
    registry = Registry(tmp_path / 'registry', pub)
    verified = make_entry(placeholder_doc(), now=1.0)
    registry.write(verified, sign_bytes(verified.entry.canonical_bytes(), key))
    return tmp_path, registry


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def reconciler(root, registry, boxes, clock=None, **kw):
    clock = clock or Clock()
    return Reconciler(
        StateStore(root / 'boxes.json'),
        InstanceStore(root / 'instances.json'),
        DeploymentStore(root / 'deployments.json'),
        registry,
        make_runner=lambda box: boxes[box.box_id].runner,
        clock=clock,
        wall=clock,
        sleep=clock.sleep,
        rng=random.Random(0),
        **kw,
    )


def seed(root, *boxes, replicas=1, enabled=True):
    s = StateStore(root / 'boxes.json')
    for box in boxes:
        s.put(box)
    DeploymentStore(root / 'deployments.json').set(ENTRY, enabled, replicas)


# ---------------------------------------------------------------- pre-staging + canary ------------------------------


def test_prestage_pulls_with_a_token_for_that_pull_only_and_verifies_artifacts():
    doc = placeholder_doc()
    doc['artifacts'] = [
        {'path': '/models/tiny', 'source': 'hf://org/tiny', 'revision': 'abc123', 'sha256': ARTIFACT_SHA}
    ]
    manifest = parse_manifest(doc)
    spec = build_run_spec(ENTRY, manifest, UUID_5090, 'i-0123456789ab')
    box = FakeDocker(image_present=False)
    report = prestage(box.runner, spec, manifest, PullToken.parse('bot:dckr_pat_x\n'), Clock())
    pull = next(c for c in box.runner.calls if 'docker pull' in c)
    assert 'docker login -u bot --password-stdin' in pull and 'docker logout' in pull and 'rm -rf' in pull
    assert box.runner.stdins[pull] == b'dckr_pat_x' and report.pulled
    fetch = box.commands('docker run --rm -v')[0]
    assert b'name: gt-placeholder' in box.manifest_written  # the blessed manifest lands on the box first
    write = next(c for c in box.runner.calls if ' && cat > ' in c)
    # on the HOST, where docker resolves `-v` sources, not in the agent container's own filesystem (real run, 9/15)
    assert (
        f'cat > /proc/1/root{spec.manifest_host_path}.tmp' in write
        and f'rm -rf /proc/1/root{spec.manifest_host_path}' in write
    )
    assert (
        '/var/lib/gt-models/gt-placeholder/models:/stage' in fetch and 'hf download org/tiny --revision abc123' in fetch
    )
    assert report.artifacts == [{'path': '/models/tiny', 'fetched': True, 'sha256': ARTIFACT_SHA}]

    again = FakeDocker(artifact_sha=ARTIFACT_SHA)  # already staged: no pull, no fetch
    assert not prestage(again.runner, spec, manifest).artifacts[0]['fetched'] and not again.commands(
        'docker run --rm -v'
    )

    wrong = FakeDocker(fetch_gives='f' * 64)
    with pytest.raises(ArtifactError, match='sha256'):
        prestage(wrong.runner, spec, manifest)
    with pytest.raises(ValueError):
        PullToken.parse('no-colon')


class Responses:
    def __init__(self, *responses: HttpResponse):
        self.responses, self.calls = list(responses), []

    def request(
        self, method: str, port: int, path: str, body: bytes | None = None, timeout: float = 10
    ) -> HttpResponse:
        self.calls.append((method, port, path, body))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def test_entry_canary_rules_and_fail_closed_types():
    manifest = parse_manifest(placeholder_doc())
    ok = run_entry_canary(Responses(parse_curl_response(CommandResult(0, 'placeholder-replace-me\n200'))), manifest)
    assert ok.ok and len(ok.results) == 2  # sent at front_door.concurrency
    bad = run_entry_canary(Responses(parse_curl_response(CommandResult(0, 'other\n200'))), manifest)
    assert not bad.ok and 'does not contain' in bad.detail

    doc = yaml.safe_load(FIXTURE_27B.read_text())
    # sparkinfer's real body (9/15): "arguments" before "name", so a canary regex must not assume key order
    tool_call = (
        '{"choices":[{"finish_reason":"tool_calls","index":0,"message":{"content":null,"role":"assistant",'
        '"tool_calls":[{"function":{"arguments":"{\\"a\\":17,\\"b\\":23}","name":"multiply"},"type":"function"}]}}]}'
    )
    client = Responses(parse_curl_response(CommandResult(0, tool_call + '\n200')))
    assert run_entry_canary(client, parse_manifest(doc), pick=0).ok and client.calls[0][0] == 'POST'
    assert json.loads(client.calls[0][3])['tools'][0]['function']['name'] == 'multiply'

    doc['entry_canary'] = [{'type': 'command', 'command': ['true'], 'pass': {'exit_code': 0}}]
    assert not run_entry_canary(Responses(), parse_manifest(doc)).ok


def test_box_http_goes_through_the_bridge_gateway():
    box = FakeDocker()
    response = BoxHttp(box.runner).request('GET', 8080, '/v1/models')
    assert response.status == 200 and 'placeholder' in response.body
    assert box.commands('curl ')[0].endswith('http://172.17.0.1:8080/v1/models')


# ---------------------------------------------------------------- reconcile -----------------------------------------


def test_zero_to_two_replicas_starts_two_instances_on_two_idle_cards(world):
    root, registry = world
    seed(root, idle_box(), replicas=2)
    box = FakeDocker()
    report = reconciler(root, registry, {'hk1': box}).run_pass()
    assert report.ok, report
    starts = [a for a in report.actions if a.kind == 'start']
    assert len(starts) == 2 and all(a.states == [IDLE, STARTING, LEASED] for a in starts)
    assert {a.uuid for a in starts} == {UUID_5090, UUID_5090_B}
    runs = box.commands('docker run -d')
    assert len(runs) == 2 and all('--network gt-noegress' in r for r in runs)
    assert {re.findall(r'device=([^"]+)', r)[0] for r in runs} == {UUID_5090, UUID_5090_B}
    records = InstanceStore(root / 'instances.json').instances
    assert {r.container_id for r in records.values()} == set(box.containers)
    assert all((r.host, r.healthy, r.draining) == ('10.0.0.1', True, False) for r in records.values())
    # two instances of one image on one box: two host ports from the workload range, each mapped to the manifest's 8080
    assert sorted((r.host_port or 0, r.port or 0) for r in records.values()) == [(20000, 20000), (20001, 20001)]
    assert sorted(re.findall(r' -p (\d+:\d+) ', r)[0] for r in runs) == ['20000:8080', '20001:8080']
    assert {c['port'] for c in box.containers.values()} == {'20000', '20001'}  # the port label is the host port
    cards = StateStore(root / 'boxes.json').get('hk1').cards
    assert {c.state for c in cards.values()} == {LEASED} and {c.instance_id for c in cards.values()} == set(records)
    assert report.running == {ENTRY: 2} and {'prestage', 'docker_run', 'load', 'canary', 'first_probe'} <= set(
        starts[0].timings_ms
    )

    steady = reconciler(root, registry, {'hk1': box}).run_pass()
    assert steady.ok and steady.actions == [] and len(box.commands('docker run -d')) == 2


def test_a_disabled_deployment_drains_to_checking(world):
    root, registry = world
    seed(root, idle_box(), replicas=2)
    box = FakeDocker()
    reconciler(root, registry, {'hk1': box}).run_pass()
    DeploymentStore(root / 'deployments.json').set(ENTRY, enabled=False)
    report = reconciler(root, registry, {'hk1': box}).run_pass()
    drains = [a for a in report.actions if a.kind == 'drain']
    assert report.ok and len(drains) == 2 and all(a.states == [LEASED, DRAINING, CHECKING] for a in drains)
    assert len(box.commands('docker stop --time 5 ')) == 2 and box.containers == {}
    assert InstanceStore(root / 'instances.json').instances == {} and report.running == {}
    after = StateStore(root / 'boxes.json').get('hk1')
    assert {c.state for c in after.cards.values()} == {CHECKING}
    verdict = CheckVerdict.from_checks([CheckResult('gpu_proof', True)], after.pinned_uuids, now=2.0)
    assert {c.state for c in apply_verdict(after, verdict, 2.0).cards.values()} == {IDLE}  # the next round


def test_a_workload_that_ignores_sigterm_is_a_failed_drain_but_still_reaches_checking(world):
    root, registry = world
    seed(root, idle_box(uuids=(UUID_5090,)), replicas=1)
    box = FakeDocker(stop_exit=137)
    reconciler(root, registry, {'hk1': box}).run_pass()
    DeploymentStore(root / 'deployments.json').set(ENTRY, enabled=False)
    (drain,) = reconciler(root, registry, {'hk1': box}).run_pass().actions
    assert not drain.ok and 'failed drain' in drain.detail and drain.states == [LEASED, DRAINING, CHECKING]
    assert box.containers == {} and StateStore(root / 'boxes.json').get('hk1').cards[UUID_5090].state == CHECKING


def test_a_failed_health_probe_undeploys_to_checking_and_three_bench(world):
    root, registry = world
    seed(root, idle_box(uuids=(UUID_5090, UUID_5090_B, UUID_C)), replicas=1)
    box = FakeDocker(healthy=False)
    report = reconciler(root, registry, {'hk1': box}).run_pass()
    (start,) = report.actions
    assert not start.ok and 'not healthy within max_load_s 10' in start.detail
    assert start.states == [IDLE, STARTING, CHECKING] and box.containers == {} and box.commands('docker rm -f')
    after = StateStore(root / 'boxes.json').get('hk1')
    assert after.status == IDLE and after.failed_starts == 1 and InstanceStore(root / 'instances.json').instances == {}
    reconciler(root, registry, {'hk1': box}).run_pass()
    third = reconciler(root, registry, {'hk1': box}).run_pass()
    assert third.actions[0].states[-1] == BENCHED
    benched = StateStore(root / 'boxes.json').get('hk1')
    assert benched.status == BENCHED and benched.last_failed == [FAILED_STARTS] and benched.cards == {}


def test_an_artifact_mismatch_never_starts_the_instance(world):
    root, registry = world
    key, pub = keypair(root)
    doc = placeholder_doc()
    doc['version'] = 2
    doc['artifacts'] = [{'path': '/models/tiny', 'source': 'hf://org/tiny', 'revision': 'abc', 'sha256': ARTIFACT_SHA}]
    verified = make_entry(doc)
    registry.write(verified, sign_bytes(verified.entry.canonical_bytes(), key))
    StateStore(root / 'boxes.json').put(idle_box())
    DeploymentStore(root / 'deployments.json').set('gt-placeholder@2', True, 1)
    box = FakeDocker(fetch_gives='f' * 64)
    report = reconciler(root, registry, {'hk1': box}).run_pass()
    assert not report.actions[0].ok and 'sha256' in report.actions[0].detail
    assert box.commands('docker run -d') == [] and report.actions[0].states == [IDLE, STARTING, CHECKING]


def test_a_restarted_controller_re_adopts_running_containers_by_label(world):
    root, registry = world
    seed(root, idle_box(), replicas=2)
    box = FakeDocker()
    reconciler(root, registry, {'hk1': box}).run_pass()
    before = InstanceStore(root / 'instances.json').instances
    (root / 'instances.json').unlink()  # the controller lost its instance view
    report = reconciler(root, registry, {'hk1': box}, visit_all=True).run_pass()
    assert report.ok and sorted(a.kind for a in report.actions) == ['adopt', 'adopt']
    after = InstanceStore(root / 'instances.json').instances
    assert {k: (r.container_id, r.uuid, r.port, r.healthy) for k, r in after.items()} == {
        k: (r.container_id, r.uuid, r.port, r.healthy) for k, r in before.items()
    }
    assert len(box.commands('docker run -d')) == 2  # nothing restarted

    # a container that vanished under a LEASED card is a heartbeat failure, not a restart (Kimbo 9/15): the box is
    # benched with its pay withheld, and every instance on it is undeployed in the same pass
    victim = next(iter(after.values()))
    box.containers.pop(victim.container_id)
    report = reconciler(root, registry, {'hk1': box}).run_pass()
    lost = next(a for a in report.actions if a.kind == 'lost')
    assert lost.instance == victim.id and lost.states == [LEASED, BENCHED] and 'heartbeat failure' in lost.detail
    benched = StateStore(root / 'boxes.json').get('hk1')
    assert benched.status == BENCHED and benched.last_failed == ['heartbeat:our_container'] and benched.withheld_from
    assert box.containers == {} and InstanceStore(root / 'instances.json').instances == {}


@pytest.mark.parametrize('how', ['vanished', 'exited'])
def test_the_reconciler_reads_a_container_gone_after_a_missed_heartbeat_as_a_stop(world, how):
    # 9/16: the reconcile pass, not the heartbeat, found the container gone once the agent was back: `lost`, 4 h bench
    root, registry = world
    seed(root, idle_box(), replicas=1)
    box = FakeDocker()
    clock = Clock()
    rec = reconciler(root, registry, {'hk1': box}, clock=clock)
    assert rec.run_pass().ok
    (record,) = rec.instances.instances.values()
    record.last_heartbeat_at, record.heartbeat_ok, record.heartbeat_misses = clock.t, None, 1  # a missed heartbeat
    rec.instances.put(record)
    good_at = clock.t
    clock.t += 90
    if how == 'vanished':
        box.containers.pop(record.container_id)
    else:
        box.containers[record.container_id]['state'] = 'exited'
    report = rec.run_pass()
    stopped = next(a for a in report.actions if a.kind == 'stopped')
    assert stopped.instance == record.id and stopped.states == [LEASED, CHECKING] and not stopped.ok
    assert 'gone after 1 missed heartbeat(s): instance stopped, not a cheat' in stopped.detail
    assert 'BENCHED' not in stopped.detail
    after = StateStore(root / 'boxes.json').get('hk1')
    assert after.status == IDLE and after.withheld_from is None and after.bench_count == 0
    event = after.standing_events[-1]
    assert event['kind'] == 'instance_stopped' and event['lease_ended_at'] == good_at and event['via'] == 'reconcile'
    assert after.cards[UUID_5090].state == CHECKING or after.cards[UUID_5090_B].state == CHECKING
    assert record.container_id not in box.containers  # a stopped container left behind is removed in the same pass
    drained = next(a for a in report.actions if a.kind == 'drain')
    assert drained.instance == record.id and drained.states[0] == CHECKING  # no clean_lease / drain_failed event
    assert [e['kind'] for e in after.standing_events] == ['instance_stopped']
    assert record.id not in InstanceStore(root / 'instances.json').instances
    assert rec.run_pass().ok and len(box.commands('docker run -d')) == 2  # re-placed on the free card


def test_a_leftover_mid_start_container_is_undeployed(world):
    root, registry = world
    box_state = transition_card(idle_box(), UUID_5090, STARTING, 1.0, 'i-00000000dead')
    seed(root, box_state, replicas=0)
    box = FakeDocker()
    cid = 'd' * 64
    box.containers[cid] = {
        'id': cid,
        'state': 'running',
        'instance': 'i-00000000dead',
        'entry': ENTRY,
        'uuid': UUID_5090,
        'port': '8080',
    }
    report = reconciler(root, registry, {'hk1': box}, visit_all=True).run_pass()
    assert [a.kind for a in report.actions] == ['orphan', 'drain'] and box.containers == {}
    assert StateStore(root / 'boxes.json').get('hk1').cards[UUID_5090].state == CHECKING


def test_an_unverifiable_entry_is_never_run(world):
    root, registry = world
    seed(root, idle_box(), replicas=1)
    path = root / 'registry' / f'{ENTRY}.json'
    path.write_bytes(path.read_bytes().replace(b'"max_load_s":10', b'"max_load_s":11'))
    box = FakeDocker()
    report = reconciler(root, registry, {'hk1': box}).run_pass()
    assert not report.ok and 'not run' in report.errors[0] and 'does not verify' in report.errors[0]
    assert box.commands('docker run') == [] and report.desired == {ENTRY: 0}


def test_placement_skips_cards_that_do_not_fit(world):
    root, registry = world
    small = idle_box()
    small.card_name = 'NVIDIA GeForce RTX 4090'
    seed(root, small, replicas=1)
    report = reconciler(root, registry, {'hk1': FakeDocker()}).run_pass()
    assert report.actions == [] and 'no IDLE card fits' in report.errors[0]


# ---------------------------------------------------------------- the proof round skips busy cards ----------------------


@pytest.fixture
def state(tmp_path):
    root = tmp_path / 'state'
    root.mkdir()
    (root / 'gt_ca').write_text('placeholder: the runner is faked\n')
    (root / 'nvml_allowlist.json').write_text(fixture('nvml_allowlist.json'))
    return root


def two_card_box(state, states):
    admit(state)
    s = store(state)
    box = s.get(HK_A)
    box.status, box.pinned_uuids, box.card_name = IDLE, [UUID_5090, UUID_5090_B], 'NVIDIA GeForce RTX 5090'
    box.cards = {
        u: CardState(st, 'i-0000000000aa' if st == LEASED else '', 1.0) for u, st in zip(box.pinned_uuids, states)
    }
    s.put(box)
    one = fixture('nvidia_smi_5090.csv')
    return box_runner(nvidia_smi=one + one.replace(UUID_5090, UUID_5090_B))


def test_the_proof_round_stages_and_fires_only_idle_cards(state):
    runner = two_card_box(state, [CHECKING, LEASED])
    with runners({HK_A: runner}):
        result = invoke(*round_args(state, '--json'))
    assert result.exit_code == 0, result.output
    creates = [c for c in runner.calls if c.startswith('docker create')]
    starts = [c for c in runner.calls if c.startswith('docker start')]
    assert len(creates) == 1 and UUID_5090 in creates[0] and UUID_5090_B not in ''.join(creates) and len(starts) == 1
    box_row = json.loads(result.stdout)['boxes'][0]
    assert box_row['cards'] == {'proved': [UUID_5090], 'skipped': {UUID_5090_B: LEASED}}
    after = store(state).get(HK_A)
    assert after.cards[UUID_5090].state == IDLE and after.cards[UUID_5090_B] == CardState(LEASED, 'i-0000000000aa', 1.0)

    with runners({HK_A: runner}):
        table = invoke(*round_args(state))
    assert '1 skipped (LEASED)' in table.output


def test_a_box_with_every_card_busy_gets_no_proof_and_no_verdict(state):
    runner = two_card_box(state, [LEASED, LEASED])
    with runners({HK_A: runner}):
        result = invoke(*round_args(state, '--json'))
    assert result.exit_code == 0, result.output
    assert not any(c.startswith('docker create') for c in runner.calls)
    row = json.loads(result.stdout)['boxes'][0]
    assert row['verdict'] is None and row['status'] == {'before': IDLE, 'after': IDLE}
    with runners({HK_A: runner}):
        check = invoke('check', HK_A, '--state-dir', state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST,
                       '--network-target', 'https://registry.example/v2/', '--network-target', 'https://hub.example/api')  # fmt: skip
    assert check.exit_code == 2 and 'every card busy' in check.output
    assert {c.state for c in store(state).get(HK_A).cards.values()} == {LEASED}


# ---------------------------------------------------------------- the commands, end to end --------------------------


def test_bless_deploy_registry_reconcile_instances_commands(state, tmp_path):
    key, _ = keypair(tmp_path)
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text(yaml.safe_dump(placeholder_doc()))
    common = ['--release-pubkey', tmp_path / 'release.pub', '--state-dir', state]
    blessed = invoke('bless', manifest, '--image', PLACEHOLDER_IMAGE, '--sign-key', key, *common, '--json')
    assert blessed.exit_code == 0, blessed.output
    assert json.loads(blessed.stdout)['entry'] == ENTRY and not json.loads(blessed.stdout)['already']
    again = invoke('bless', manifest, '--image', PLACEHOLDER_IMAGE, '--sign-key', key, *common, '--json')
    assert json.loads(again.stdout)['already'] is True
    other = invoke('bless', manifest, '--image', 'gt-placeholder@sha256:' + '3' * 64, '--sign-key', key, *common)
    assert other.exit_code == 1 and 'bump version' in other.output
    untrusted = invoke(
        'bless', manifest, '--image', PLACEHOLDER_IMAGE, '--sign-key', key, '--state-dir', tmp_path / 'x'
    )
    assert untrusted.exit_code == 1 and 'does not verify' in untrusted.output  # the compiled release key is not ours

    assert invoke('deploy', ENTRY, '--enabled', '--replicas', 1, *common).exit_code == 0
    shown = json.loads(invoke('registry', 'show', *common, '--json').stdout)['entries']
    assert [(e['entry'], e['verified'], e['enabled'], e['replicas']) for e in shown] == [(ENTRY, True, True, 1)]

    runner = two_card_box(state, [IDLE, IDLE])  # admits HK_A with two IDLE cards
    box = FakeDocker()
    with patch.object(ctl, '_make_runner', side_effect=lambda st, b, ca, purpose: box.runner):
        result = invoke('reconcile', *common, '--json')
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [a['kind'] for a in payload['actions']] == ['start'] and payload['running'] == {ENTRY: 1}
    rows = json.loads(invoke('instances', '--state-dir', state, '--json').stdout)['instances']
    assert len(rows) == 1 and rows[0]['healthy'] and rows[0]['card_state'] == LEASED and runner is not None
    assert 'LEASED' in invoke('instances', '--state-dir', state).output

    assert invoke('deploy', ENTRY, '--disabled', *common).exit_code == 0
    with patch.object(ctl, '_make_runner', side_effect=lambda st, b, ca, purpose: box.runner):
        drained = invoke('reconcile', *common)
    assert drained.exit_code == 0, drained.output
    assert 'LEASED → DRAINING → CHECKING' in drained.output and box.containers == {}


def test_the_committed_27b_manifest_agrees_with_itself_and_with_the_release_container():
    """v6 (9/16): sparkinfer's own release container. Its entrypoint reads MODEL_DIR / DRAFT_DIR, so both must be
    pre-staged artifacts; refuse-never-queue means the runtime's queue depth equals our front door's concurrency; the
    drain window must cover the output cap; the canaries must not depend on the checkpoint's sampling defaults. The
    digest is a placeholder until upstream tags (bless pins it), so the placeholder is allowed here and nowhere else."""
    manifest = load_manifest(
        Path(__file__).parents[2] / 'docker' / 'controller' / 'manifests' / 'qwen3.8-27b-nvfp4.yaml',
        allow_placeholder_digest=True,
    )
    spec = build_run_spec('qwen3.8-27b-nvfp4@1', manifest, UUID_5090, 'i-0123456789ab')
    env = dict(spec.env)
    assert [a.path for a in manifest.artifacts] == [env['MODEL_DIR'], env['DRAFT_DIR']]
    assert env['SPARKINFER_NO_DOWNLOAD'] == '1' and env['SPARKINFER_MODE'] == 'serve-dspark'
    assert env['SPARKINFER_MAX_QUEUE_DEPTH'] == str(manifest.front_door.concurrency)
    assert env['SPARKINFER_ADMISSION_WAIT_S'] == '0' and env['SPARKINFER_DRAIN_GRACE_S'] == '0'
    assert 'SPARKINFER_MAX_OUTPUT_TOKENS' not in env  # the runtime's own cap (16384 in their container), never ours
    assert manifest.drain.max_s >= 16384 / 94  # the drain covers that cap at ~94 tok/s decode
    assert all(c.spec['body']['temperature'] == 0 for c in manifest.entry_canary)
    assert spec.volumes == (('/var/lib/gt-models/qwen3.8-27b-nvfp4/models', '/models', True),)
    # 9/17 soak: min_vram_gb 32 sat above our 5090 spec floor (31.25 GB) and the entry could never place
    assert manifest.placement.min_vram_gb <= RTX_5090.vram_total_mib_min / 1024
    assert spec.network == 'gt-noegress' and manifest.image.startswith(
        'ghcr.io/gittensor-ai-lab/sparkinfer-qwen38:0.5.9@sha256:afb4a553'
    )


# ---------------------------------------------------------------- workload ports ------------------------------------


def test_a_freed_host_port_is_given_out_again_and_an_exhausted_range_skips_the_box(world):
    root, registry = world
    box_state = idle_box(uuids=(UUID_5090, UUID_5090_B, UUID_C))
    box_state.workload_ports = [20000, 20001]
    seed(root, box_state, replicas=2)
    box = FakeDocker()
    assert reconciler(root, registry, {'hk1': box}).run_pass().ok
    records = InstanceStore(root / 'instances.json').instances
    assert sorted(r.host_port or 0 for r in records.values()) == [20000, 20001]

    # a third replica: an IDLE card is left, a port is not; the box is skipped and the reason named
    DeploymentStore(root / 'deployments.json').set(ENTRY, True, 3)
    short = reconciler(root, registry, {'hk1': box}).run_pass()
    assert short.actions == [] and len(box.commands('docker run -d')) == 2
    assert short.errors == [
        f'{ENTRY}: 1 replica(s) short: no IDLE card fits its placement; hk1: workload ports 20000-20001 all in use'
    ]

    # scale down: the drained instance's port is free again, and the next start gets it
    DeploymentStore(root / 'deployments.json').set(ENTRY, True, 1)
    (drain,) = reconciler(root, registry, {'hk1': box}).run_pass().actions
    freed = records[drain.instance].host_port
    DeploymentStore(root / 'deployments.json').set(ENTRY, True, 2)
    (start,) = reconciler(root, registry, {'hk1': box}).run_pass().actions
    assert start.ok and start.uuid == UUID_C
    assert InstanceStore(root / 'instances.json').instances[start.instance].host_port == freed
    assert f' -p {freed}:8080 ' in box.commands('docker run -d')[-1]


def test_health_probes_and_the_canary_go_to_the_host_port(world):
    root, registry = world
    seed(root, idle_box(uuids=(UUID_5090,)), replicas=1)
    box = FakeDocker()
    assert reconciler(root, registry, {'hk1': box}).run_pass().ok
    curls = box.commands('curl ')
    assert curls and all(c.endswith('http://172.17.0.1:20000/v1/models') for c in curls)


def test_a_dev_box_with_its_own_port_range_and_a_port_map(world, state):
    root, registry = world
    box_state = idle_box(uuids=(UUID_5090,))  # port_map {'8080': 20135}: a Lium pod exposing 8080 on 20135
    box_state.workload_ports = [8080, 8080]
    seed(root, box_state, replicas=1)
    box = FakeDocker()
    assert reconciler(root, registry, {'hk1': box}).run_pass().ok
    (record,) = InstanceStore(root / 'instances.json').instances.values()
    assert (record.host_port, record.port) == (8080, 20135) and ' -p 8080:8080 ' in box.commands('docker run -d')[0]

    result = admit(state, extra=['--workload-ports', '8080-8080', '--port-map', '8080=20135', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['workload_ports'] == [8080, 8080]
    assert store(state).get(HK_A).workload_port_range() == range(8080, 8081)
    assert admit(state, extra=['--workload-ports', '9-8']).exit_code == 2


def test_a_pinned_deployment_lands_only_on_its_box(world):
    """A canary run on our own card (Kimbo 9/15): the fresher, better box is skipped when the deployment is pinned."""
    root, registry = world
    ours = idle_box()
    other = idle_box('hk2', ('GPU-other-1',), '10.0.0.2')
    other.last_check_at = 200.0  # fresher than ours (100.0): the normal pick
    seed(root, ours, other, replicas=1)
    DeploymentStore(root / 'deployments.json').set(ENTRY, box='hk1')
    boxes = {'hk1': FakeDocker(), 'hk2': FakeDocker()}
    report = reconciler(root, registry, boxes).run_pass()
    starts = [a for a in report.actions if a.kind == 'start']
    assert len(starts) == 1 and starts[0].box == 'hk1', report
    assert boxes['hk2'].commands('docker run -d') == []
    # pinned to a box with nothing free: short, and the error names the pin
    DeploymentStore(root / 'deployments.json').set(ENTRY, replicas=3)
    report = reconciler(root, registry, boxes).run_pass()
    assert any('pinned box hk1' in e for e in report.errors), report.errors
