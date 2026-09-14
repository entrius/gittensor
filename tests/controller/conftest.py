# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Fixtures for the controller full check: a recorded 5090 box (nvidia-smi / df / docker output), a fake GPU-proof
provider in the slot, and a ``FakeRunner`` that answers every command the check issues the way a real, honest box
would."""

import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Dict, Optional

import pytest

from gittensor.controller.checks.full_check import FullCheckConfig
from gittensor.controller.checks.nvml_allowlist import NvmlAllowlist
from gittensor.controller.checks.runner import CommandResult, FakeRunner, HostRunner, regex
from gittensor.controller.checks.scrape import (
    KERNEL_DRIVER_COMMAND,
    NVML_MD5_COMMAND,
    agent_image_command,
    agent_image_id_command,
    disk_free_command,
    network_command,
    nvidia_smi_command,
)
from gittensor.controller.proof.slot import (
    BoxIdentity,
    ProofUnavailable,
    ProofVerdict,
    StagedProof,
    create_command,
    remove_command,
    start_command,
)

FIXTURES = Path(__file__).parent / 'fixtures'
UUID_5090 = 'GPU-4f2a6b8c-1d3e-4a5b-9c7d-0e1f2a3b4c5d'
UUID_5090_B = 'GPU-9b8c7d6e-5f40-4132-a2b3-c4d5e6f70819'
DRIVER = '580.65.06'
NVML_MD5 = '3c9d0f1e2b4a5968778695a4b3c2d1e0'
NVML_PATH = '/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.580.65.06'
AGENT_DIGEST = 'sha256:' + 'a' * 64
AGENT_IMAGE_OUT = f'entrius/gt-agent@{AGENT_DIGEST}\n'
AGENT_IMAGE_ID = 'sha256:' + 'b' * 64
VRAM_TOTAL_BYTES = 32607 * 1024 * 1024
FILL_RATIO = 0.9
FILLED_BYTES = int(FILL_RATIO * VRAM_TOTAL_BYTES)
GOOD_WALL_MS = 1500.0
NETWORK_TARGETS = ('https://registry.example/v2/', 'https://hub.example/api')
PROOF_IMAGE = 'entrius/gt-proof:test'
CONFIG = FullCheckConfig(agent_image_digests=(AGENT_DIGEST,), network_targets=NETWORK_TARGETS, proof_image=PROOF_IMAGE)
FAKE_BINARY = b'\x7fELF-fake-sealed-proof'


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def container_for(uuid: str) -> str:
    """The container id the fake box hands back for a card's `docker create`: deterministic, 64 hex like docker's."""
    return hashlib.sha256(f'ctr:{uuid}'.encode()).hexdigest()


def challenge_for(uuid: str, version: str) -> str:
    return hashlib.sha256(f'challenge:{version}:{uuid}'.encode()).hexdigest()[:32]


class FakeProof:
    """A provider that behaves like the sealed binary's controller side would, without any crypto: it stages one
    container per card with a per-card challenge token copied in, and judges a result by the token echoing back
    from the right UUID, a full-enough fill, and both clocks inside a band. Counts what it staged so tests can see
    that identity failures skip it."""

    def __init__(
        self, version: str = 'fake-1', wall_budget_ms: float = 1.6 * GOOD_WALL_MS, outer_budget_ms: float = 6_000.0
    ):
        self.version = version
        self.wall_budget_ms = wall_budget_ms
        self.outer_budget_ms = outer_budget_ms
        self.staged: list = []

    def stage(self, runner: HostRunner, identity: BoxIdentity, image: str, timeout: float) -> StagedProof:
        containers: Dict[str, str] = {}
        challenges: Dict[str, str] = {}
        for i, uuid in enumerate(identity.uuids):
            challenge = challenge_for(uuid, self.version)
            result = runner.run(
                create_command(image, uuid, f'gt-proof-{i}', ['--', '--challenge', challenge]), timeout=timeout
            )
            if not result.ok:
                raise ProofUnavailable(f'docker create failed: {(result.stderr or result.stdout).strip()[:200]}')
            cid = result.stdout.strip()
            runner.run(f'docker cp - {cid}:/opt/gt-proof/bin', timeout=timeout, stdin=FAKE_BINARY)
            containers[uuid], challenges[uuid] = cid, challenge
        staged = StagedProof(self.version, containers, challenges)
        self.staged.append(staged)
        return staged

    def start_command(self, staged: StagedProof, uuid: str) -> str:
        return start_command(staged.containers[uuid])

    def judge(self, staged, uuid, stdout, elapsed_ms, vram_total_bytes) -> ProofVerdict:
        try:
            answer = json.loads(stdout)
        except ValueError:
            return ProofVerdict(False, f'no JSON: {stdout[:80]!r}', uuid, elapsed_ms=elapsed_ms)
        got_uuid = str(answer.get('uuid', ''))
        wall = float(answer.get('wall_ms') or 0)
        filled = int(answer.get('filled_bytes') or 0)
        verdict = ProofVerdict(
            True,
            'ok',
            got_uuid,
            filled,
            wall,
            answer.get('speed'),
            elapsed_ms,
            {'job_version': answer.get('job_version')},
        )
        if answer.get('challenge') != staged.challenges.get(uuid):
            return _fail(verdict, 'challenge did not echo: sealed for another box or version')
        if got_uuid != uuid:
            return _fail(verdict, f'answered from {got_uuid or "?"}, not {uuid}')
        want = FILL_RATIO * float(vram_total_bytes or 0)
        if want and filled < 0.6 * want:
            return _fail(verdict, f'under-filled: {filled / 1e9:.1f} GB of {want / 1e9:.1f} GB')
        if wall > self.wall_budget_ms:
            return _fail(verdict, f'too slow: {wall:.0f} ms > {self.wall_budget_ms:.0f} ms')
        if elapsed_ms > self.outer_budget_ms:
            return _fail(verdict, f'too slow: {elapsed_ms:.0f} ms round trip > {self.outer_budget_ms:.0f} ms')
        return verdict

    def cleanup_command(self, staged: StagedProof) -> Optional[str]:
        return remove_command(list(staged.containers.values())) if staged.containers else None


def _fail(v: ProofVerdict, reason: str) -> ProofVerdict:
    v.passed, v.reason = False, reason
    return v


@pytest.fixture
def proof() -> FakeProof:
    return FakeProof()


@pytest.fixture
def allowlist() -> NvmlAllowlist:
    return NvmlAllowlist.from_file(FIXTURES / 'nvml_allowlist.json')


_CREATE_DEVICE = re.compile(r'^docker create --gpus="device=([^"]+)"')
_CREATE_CHALLENGE = re.compile(r'--challenge (\w+)')
_START = re.compile(r'^docker start -a (\w+)$')


def job_responder(
    *,
    uuid: Optional[str] = None,
    wall_ms: Optional[float] = None,
    filled_bytes: Optional[int] = None,
    challenge: Optional[str] = None,
    version: str = 'fake-1',
) -> Callable[[str], str]:
    """Answers the proof's docker lines the way an honest box would: `docker create` returns a container id and
    remembers the challenge it was given; `docker cp` says nothing; `docker start -a` prints the sealed result for
    that container's card, echoing its challenge. Any override makes it a lying (slow, wrong, relayed) card."""
    challenges: Dict[str, str] = {}
    cards: Dict[str, str] = {}

    def respond(command: str) -> str:
        m = _CREATE_DEVICE.match(command)
        if m:
            card = m.group(1)
            cid = container_for(card)
            cards[cid] = card
            challenges[cid] = _CREATE_CHALLENGE.search(command).group(1)
            return cid + '\n'
        if command.startswith('docker cp -') or command.startswith('docker rm -f'):
            return ''
        m = _START.match(command)
        if m:
            cid = m.group(1)
            card = cards.get(cid, '?')
            return json.dumps(
                {
                    'uuid': uuid if uuid is not None else card,
                    'name': 'NVIDIA GeForce RTX 5090',
                    'driver': DRIVER,
                    'filled_bytes': filled_bytes if filled_bytes is not None else FILLED_BYTES,
                    'wall_ms': wall_ms if wall_ms is not None else GOOD_WALL_MS,
                    'speed': 41.2,
                    'challenge': challenge if challenge is not None else challenges.get(cid, ''),
                    'job_version': version,
                    'run_ms': 2400.0,
                }
            )
        return CommandResult(127, '', f'job_responder: unexpected {command!r}')

    return respond


def passing_runner(
    nvidia_smi: str = fixture('nvidia_smi_5090.csv'),
    kernel_driver: str = fixture('proc_driver_version.txt'),
    nvml_md5: str = f'{NVML_MD5}  {NVML_PATH}\n',
    agent_image: str = AGENT_IMAGE_OUT,
    agent_image_id: str = AGENT_IMAGE_ID + '\n',
    df: str = fixture('df_docker.txt'),
    network_targets=NETWORK_TARGETS,
    job=None,
) -> FakeRunner:
    """A box that passes everything. Tests override one command with ``runner.on(...)`` to make one check fail."""
    runner = FakeRunner(
        {
            nvidia_smi_command(): nvidia_smi,
            NVML_MD5_COMMAND: nvml_md5,
            KERNEL_DRIVER_COMMAND: kernel_driver,
            agent_image_command(): agent_image,
            agent_image_id_command(): agent_image_id,
            disk_free_command(): df,
        }
    )
    for url in network_targets:
        runner.on(network_command(url), '200 4812345.000\n')
    runner.on(regex(r'^docker (create|cp|start|rm) '), job or job_responder())
    return runner


def failing(stderr: str, code: int = 1) -> CommandResult:
    return CommandResult(code, '', stderr)
