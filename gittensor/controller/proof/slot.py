# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The pluggable GPU proof: interface, the two-phase probe, and the fail-closed default.

**What a provider does** (``23`` §3a): derive a key from the box's *claimed* identity, encrypt a random challenge
under it, put its binary and the challenge on the box, and later unseal ``{UUID, VRAM filled, measured speed,
nonce}`` and judge it (UUID pin, fill from our 5090 spec table, 5090 speed band). All of that is the provider's;
this module only fixes the shape so the controller can drive any version of it.

**Two phases** (``23`` §3b). Phase 1, ``stage_box``: connect and leave everything ready on the box — one
``docker create`` (not started) of ``entrius/gt-proof`` per card, pinned to that card, with the binary and its
challenge copied in over the same SSH session (``docker cp -`` from stdin: the binary never touches a registry).
Phase 2, ``fire_box``: ``docker start -a`` every prepared container at the same instant and time each one on
*our* clock. A scheduler that wants the whole fleet to start together runs phase 1 on every box first, then phase
2 everywhere; ``probe_box`` does both for one box, which is what a full check needs.
"""

from __future__ import annotations

import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import GpuInfo


class ProofUnavailable(RuntimeError):
    """No proof can be run: no provider, or the provider could not stage. Nothing is admitted; the check is ``not_run``
    (no answer was judged), not a failed proof."""


@dataclass(frozen=True)
class BoxIdentity:
    """The box's CLAIMED identity, from the scrape. The provider keys its challenge to this; a box whose real cards
    differ cannot open it (``23`` §3a step 2)."""

    uuids: Tuple[str, ...]
    card_name: str
    driver: str = ''


@dataclass
class StagedProof:
    """What phase 1 left on a box: one prepared container per card, and whatever the provider needs to judge."""

    version: str
    containers: Dict[str, str]  # gpu uuid -> container id (created, not started)
    challenges: Dict[str, str] = field(default_factory=dict)  # gpu uuid -> the provider's opaque challenge
    extra: dict = field(default_factory=dict)


@dataclass
class ProofVerdict:
    passed: bool
    reason: str
    uuid: str = ''
    filled_bytes: int = 0
    wall_ms: Optional[float] = None  # the job's own clock, as sealed
    speed: Optional[float] = None  # the provider's measured-speed figure (its unit)
    elapsed_ms: Optional[float] = None  # our clock around `docker start -a`
    extra: dict = field(default_factory=dict)
    not_run: bool = False  # the container never started: there is no answer to judge (``container_never_started``)

    def as_dict(self) -> dict:
        return asdict(self)


class GpuProof(Protocol):
    """A proof provider. ``version`` is what the controller records in evidence and what rotation bumps."""

    version: str

    def stage(self, runner: HostRunner, identity: BoxIdentity, image: str, timeout: float) -> StagedProof:
        """Phase 1 on one box. Create (do not start) one container per card and copy in whatever the proof needs.
        Raise ``ProofUnavailable`` if it cannot be done; the check then fails closed."""
        ...

    def start_command(self, staged: StagedProof, uuid: str) -> str:
        """The command phase 2 fires for one card; its stdout is the sealed result."""
        ...

    def judge(
        self, staged: StagedProof, uuid: str, stdout: str, elapsed_ms: float, vram_total_bytes: Optional[int]
    ) -> ProofVerdict:
        """Unseal and judge one card's result. Never raises."""
        ...

    def cleanup_command(self, staged: StagedProof) -> Optional[str]:
        """Remove what phase 1 created (containers, files). None if nothing to do."""
        ...


class UnconfiguredProof:
    """The default: no provider wired in. No box passes its GPU proof, with a reason that says why."""

    version = 'unconfigured'
    REASON = (
        'no GPU proof provider configured: the sealed binary is its own track (vault 23 §3a); '
        'nothing is admitted until one is plugged into the slot'
    )

    def stage(self, runner: HostRunner, identity: BoxIdentity, image: str, timeout: float) -> StagedProof:
        raise ProofUnavailable(self.REASON)

    def start_command(self, staged: StagedProof, uuid: str) -> str:
        raise ProofUnavailable(self.REASON)

    def judge(self, staged, uuid, stdout, elapsed_ms, vram_total_bytes) -> ProofVerdict:
        return ProofVerdict(False, self.REASON, uuid)

    def cleanup_command(self, staged: StagedProof) -> Optional[str]:
        return None


# ---------------------------------------------------------------- docker lines --------------------------------------


def image_ref(
    repo: str = cfg.PROOF_IMAGE_REPO, tag: str = cfg.PROOF_IMAGE_TAG, digest: str = cfg.PROOF_IMAGE_DIGEST
) -> str:
    """``repo@sha256:...`` when a digest is pinned, else ``repo:tag`` (dev only; the agent refuses unsigned images)."""
    return f'{repo}@{digest}' if digest else f'{repo}:{tag}'


def create_command(image: str, uuid: str, name: str, args: Sequence[str] = ()) -> str:
    """``docker create`` one proof container pinned to one card. Inside it that card is device 0. The container
    is labelled so a restarted controller can find and remove strays."""
    parts = [
        'docker',
        'create',
        f'--gpus="device={uuid}"',
        '--name',
        name,
        '--label',
        f'io.gittensor.proof.uuid={uuid}',
        image,
        *args,
    ]
    return ' '.join(p if p.startswith('--gpus=') else shlex.quote(p) for p in parts)


def start_command(container_id: str) -> str:
    """Start a prepared container and stream its output: the sealed result on stdout."""
    return f'docker start -a {shlex.quote(container_id)}'


def remove_command(container_ids: Sequence[str]) -> str:
    return 'docker rm -f ' + ' '.join(shlex.quote(c) for c in container_ids)


# ---------------------------------------------------------------- the probe -----------------------------------------


@dataclass
class ProbeResult:
    provider: str
    cards: List[dict] = field(default_factory=list)  # per card: uuid, command, the verdict's fields
    error: str = ''  # staging failed: nothing ran

    @property
    def passed(self) -> bool:
        return not self.error and bool(self.cards) and all(c['passed'] for c in self.cards)

    @property
    def failures(self) -> List[str]:
        return [f'{c["uuid"]}: {c["reason"]}' for c in self.cards if not c['passed']]

    @property
    def not_run(self) -> bool:
        """The proof could not be carried out and no card gave a wrong answer: staging failed, or every card that did
        not pass never started its container. One judged failure among the cards makes it a failed proof."""
        if self.error:
            return True
        failed = [c for c in self.cards if not c['passed']]
        return bool(failed) and all(c.get('not_run') for c in failed)


def proof_image_ready(runner: HostRunner, image: str = '', timeout: float = cfg.PROOF_IMAGE_PROBE_TIMEOUT_S) -> bool:
    """Whether the proof image is on the box; when it is not, its pull is started there, detached, and this returns
    False at once. ``docker create`` pulls a missing image by itself, inside the proof's own 60 s command timeout: a
    2 GB image does not arrive in that on most links, and the timeout was judged as a failed GPU proof (mainnet 9/18,
    the first outside miner, benched 16 h for it). The pull is setup, not proof: a box without the image gets no
    verdict, stays where it is (ADMIT: unpaid), and is proved on the first round that finds the image."""
    ref = shlex.quote(image or image_ref())
    result = runner.run(
        f'if docker image inspect {ref} >/dev/null 2>&1; then echo ready; '
        f'else (nohup docker pull -q {ref} >/dev/null 2>&1 </dev/null &) ; echo pulling; fi',
        timeout=timeout,
    )
    return result.ok and result.stdout.strip().endswith('ready')


def clip(text: str, keep: int = cfg.ERROR_CLIP) -> str:
    """The last ``keep`` characters: a docker error says why at its end."""
    text = text.strip()
    return text if len(text) <= keep else '…' + text[-keep:]


# What `docker start` prints, and exits non-zero on, when the runtime could not create or start the container (the
# OCI runtime, NVIDIA's prestart hook, a device that is not there). Our binary never ran. A box could print this
# itself, and gains nothing: a proof that did not run pays nothing and counts towards COULD_NOT_RUN_BENCH_AFTER.
_DAEMON_ERROR = 'Error response from daemon'


def container_never_started(exit_code: int, stderr: str) -> bool:
    return exit_code != 0 and _DAEMON_ERROR in (stderr or '')


PROOF_IMAGE_PULLING = 'proof image not on the box yet: its pull was started, proved on a later round'


def stage_box(
    runner: HostRunner,
    gpus: Sequence[GpuInfo],
    proof: GpuProof,
    image: str = '',
    timeout: float = cfg.PROOF_JOB_TIMEOUT_S,
) -> StagedProof:
    """Phase 1. Raises ``ProofUnavailable`` (provider) or whatever the transport raises."""
    identity = BoxIdentity(tuple(g.uuid for g in gpus), gpus[0].name if gpus else '', gpus[0].driver if gpus else '')
    return proof.stage(runner, identity, image or image_ref(), timeout)


def fire_box(
    runner: HostRunner,
    gpus: Sequence[GpuInfo],
    proof: GpuProof,
    staged: StagedProof,
    timeout: float = cfg.PROOF_JOB_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
    executor: Optional[ThreadPoolExecutor] = None,
) -> List[dict]:
    """Phase 2: start every card's prepared container at once (one thread per card), time each on our clock, judge.
    A card whose container was never staged fails with that reason."""

    def one(gpu: GpuInfo) -> dict:
        if gpu.uuid not in staged.containers:
            return _card(gpu.uuid, '', ProofVerdict(False, 'not staged', gpu.uuid))
        command = proof.start_command(staged, gpu.uuid)
        started = clock()
        never_started = False
        try:
            result = runner.run(command, timeout=timeout)
            elapsed_ms = (clock() - started) * 1000.0
            stdout = result.stdout if result.ok else ''
            error = '' if result.ok else f'exit {result.exit_code}: {clip(result.stderr or result.stdout)}'
            never_started = container_never_started(result.exit_code, result.stderr)
        except Exception as e:  # transport died mid-proof: the challenge was out, so this stays a failed proof
            elapsed_ms = (clock() - started) * 1000.0
            stdout, error = '', clip(f'{type(e).__name__}: {e}')
        if error:
            reason = f'container never started: {error}' if never_started else f'job error: {error}'
            verdict = ProofVerdict(False, reason, gpu.uuid, elapsed_ms=elapsed_ms, not_run=never_started)
        else:
            verdict = proof.judge(staged, gpu.uuid, stdout, elapsed_ms, gpu.memory_total_bytes)
        return _card(gpu.uuid, command, verdict)

    if not gpus:
        return []
    if executor is not None:
        return list(executor.map(one, gpus))
    with ThreadPoolExecutor(max_workers=max(1, len(gpus))) as pool:
        return list(pool.map(one, gpus))


def _card(uuid: str, command: str, verdict: ProofVerdict) -> dict:
    """One card's row of evidence: ``uuid`` is the card we challenged, ``answered_uuid`` what the result claims."""
    return {**verdict.as_dict(), 'uuid': uuid, 'answered_uuid': verdict.uuid, 'command': command}


def probe_box(
    runner: HostRunner,
    gpus: Sequence[GpuInfo],
    proof: GpuProof,
    image: str = '',
    timeout: float = cfg.PROOF_JOB_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeResult:
    """Both phases on one box, then cleanup. Never raises: a staging failure is a ``ProbeResult`` with ``error``."""
    result = ProbeResult(provider=getattr(proof, 'version', '?'))
    if not gpus:
        result.error = 'no GPUs to prove'
        return result
    try:
        staged = stage_box(runner, gpus, proof, image, timeout)
    except ProofUnavailable as e:
        result.error = clip(str(e))
        return result
    except Exception as e:  # transport
        result.error = clip(f'staging failed: {type(e).__name__}: {e}')
        return result
    try:
        result.cards = fire_box(runner, gpus, proof, staged, timeout, clock)
    finally:
        cleanup = proof.cleanup_command(staged)
        if cleanup:
            try:
                runner.run(cleanup, timeout=cfg.SSH_COMMAND_TIMEOUT_S)
            except Exception:  # best effort; the reconciler sweeps labelled strays
                pass
    return result
