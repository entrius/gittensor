# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Fixtures for the controller full check: a recorded 5090 box (nvidia-smi / df / docker output), a small challenge
bank, and a ``FakeRunner`` that answers every command the check issues the way a real, honest box would."""

import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Optional

import pytest

from gittensor.controller.challenge.bank import BankConsumer, BankEntry, ChallengeBank, ChallengeParams
from gittensor.controller.checks.full_check import FullCheckConfig
from gittensor.controller.checks.nvml_allowlist import NvmlAllowlist
from gittensor.controller.checks.runner import CommandResult, FakeRunner, regex
from gittensor.controller.checks.scrape import (
    KERNEL_DRIVER_COMMAND,
    NVML_MD5_COMMAND,
    agent_image_command,
    disk_free_command,
    network_command,
    nvidia_smi_command,
)

FIXTURES = Path(__file__).parent / 'fixtures'
UUID_5090 = 'GPU-4f2a6b8c-1d3e-4a5b-9c7d-0e1f2a3b4c5d'
UUID_5090_B = 'GPU-9b8c7d6e-5f40-4132-a2b3-c4d5e6f70819'
DRIVER = '580.65.06'
NVML_MD5 = '3c9d0f1e2b4a5968778695a4b3c2d1e0'
NVML_PATH = '/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.580.65.06'
AGENT_DIGEST = 'sha256:' + 'a' * 64
AGENT_IMAGE_OUT = f'ghcr.io/entrius/gt-agent@{AGENT_DIGEST}\n'
VRAM_TOTAL_BYTES = 32607 * 1024 * 1024
BANK_WALL_MS = 1500.0
PARAMS = ChallengeParams()
FILLED_BYTES = int(PARAMS.fill_ratio * VRAM_TOTAL_BYTES)
NETWORK_TARGETS = ('https://registry.example/v2/', 'https://hub.example/api')
CONFIG = FullCheckConfig(agent_image_digests=(AGENT_DIGEST,), network_targets=NETWORK_TARGETS)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def digest_for(seed: int) -> str:
    """A stand-in for the GEMM chain's digest: any deterministic function of the seed will do for the consumer."""
    return hashlib.sha256(f'gt-challenge:{seed}'.encode()).hexdigest()


def make_bank(n: int = 5, params: ChallengeParams = PARAMS, wall_ms: float = BANK_WALL_MS) -> ChallengeBank:
    entries = [
        BankEntry(
            seed=1000 + i,
            digest=digest_for(1000 + i),
            wall_ms=wall_ms,
            filled_bytes=FILLED_BYTES,
            uuid='GPU-bank-card',
            card_name='NVIDIA GeForce RTX 5090',
            driver=DRIVER,
            image_digest='sha256:' + 'c' * 64,
            generated_at=1_700_000_000.0,
            run_ms=wall_ms + 900.0,
        )
        for i in range(n)
    ]
    return ChallengeBank(params, entries, 'NVIDIA GeForce RTX 5090', DRIVER, 'sha256:' + 'c' * 64, 1_700_000_000.0)


@pytest.fixture
def bank(tmp_path) -> BankConsumer:
    return BankConsumer(make_bank(), tmp_path / 'bank.used.json')


@pytest.fixture
def allowlist() -> NvmlAllowlist:
    return NvmlAllowlist.from_file(FIXTURES / 'nvml_allowlist.json')


_SEED = re.compile(r'--seed (\d+)')
_DEVICE = re.compile(r'--gpus="device=([^"]+)"')


def job_responder(
    bank: ChallengeBank,
    *,
    digest: Optional[str] = None,
    wall_ms: Optional[float] = None,
    filled_bytes: Optional[int] = None,
    uuid: Optional[str] = None,
    params: ChallengeParams = PARAMS,
) -> Callable[[str], str]:
    """Answers a ``docker run ... --seed N`` command the way the challenge job on an honest card would: the bank's
    digest for that seed, the bank's wall, the requested fill, the UUID the run was pinned to. Any override makes it
    a lying (or slow, or wrong) card."""

    def respond(command: str) -> str:
        seed = int(_SEED.search(command).group(1))
        entry = bank.entry(seed)
        card = _DEVICE.search(command).group(1) if _DEVICE.search(command) else UUID_5090
        return json.dumps(
            {
                'seed': seed,
                'device': 0,
                'uuid': uuid if uuid is not None else card,
                'name': 'NVIDIA GeForce RTX 5090',
                'driver': DRIVER,
                'sm_count': 170,
                'vram_total': VRAM_TOTAL_BYTES,
                'vram_free_before': VRAM_TOTAL_BYTES - 600 * 1024 * 1024,
                'filled_bytes': filled_bytes if filled_bytes is not None else FILLED_BYTES,
                'fill_ratio': params.fill_ratio,
                'dim': params.dim,
                'matrices': params.matrices,
                'iters': params.iters,
                'digest': digest if digest is not None else (entry.digest if entry else 'no-such-seed'),
                'wall_ms': wall_ms if wall_ms is not None else (entry.wall_ms if entry else 0.0),
                'job_version': 'test',
                'run_ms': 2400.0,
            }
        )

    return respond


def passing_runner(
    bank: BankConsumer,
    nvidia_smi: str = fixture('nvidia_smi_5090.csv'),
    kernel_driver: str = fixture('proc_driver_version.txt'),
    nvml_md5: str = f'{NVML_MD5}  {NVML_PATH}\n',
    agent_image: str = AGENT_IMAGE_OUT,
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
            disk_free_command(): df,
        }
    )
    for url in network_targets:
        runner.on(network_command(url), '200 4812345.000\n')
    runner.on(regex(r'^docker run '), job or job_responder(bank.bank))
    return runner


def failing(stderr: str, code: int = 1) -> CommandResult:
    return CommandResult(code, '', stderr)
