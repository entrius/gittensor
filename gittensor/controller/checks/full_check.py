# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``run_full_check``: point the controller at a box, get a verdict with evidence (``24`` §3 WS-C).

Scrape once over the runner, judge identity and resources (``checks.identity_checks``), and only if all of that
passed run the GPU proof — a box that already failed identity does not get ~30 GB of its VRAM filled for nothing.
The verdict is ADMIT when every check passed, otherwise BENCH with the failing checks named. Nothing here changes
box state; ``state.apply_verdict`` does that.
"""

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.checks import GPU_PROOF, check_gpu_proof, identity_checks
from gittensor.controller.checks.nvml_allowlist import NvmlAllowlist
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import scrape_host
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.proof.slot import GpuProof, UnconfiguredProof, image_ref


@dataclass(frozen=True)
class FullCheckConfig:
    """The served inputs of a full check, in one place so a test (or a future served config) can vary them."""

    spec: cfg.CardSpec = cfg.RTX_5090
    agent_image_digests: Tuple[str, ...] = ()  # sha256:... digests of the agent images we published
    agent_container: str = cfg.AGENT_CONTAINER_NAME
    power_min_ratio: float = cfg.POWER_LIMIT_MIN_RATIO
    disk_min_free_gb: float = cfg.DISK_MIN_FREE_GB
    disk_path: str = cfg.DISK_PATH
    network_targets: Tuple[str, ...] = tuple(cfg.NETWORK_TARGETS)
    proof_image: str = field(default_factory=image_ref)
    proof_timeout_s: float = cfg.PROOF_JOB_TIMEOUT_S
    ssh_timeout_s: float = cfg.SSH_COMMAND_TIMEOUT_S


def run_full_check(
    runner: HostRunner,
    allowlist: NvmlAllowlist,
    proof: GpuProof = UnconfiguredProof(),
    pinned_uuids: Optional[Sequence[str]] = None,
    config: FullCheckConfig = FullCheckConfig(),
    now: Optional[float] = None,
) -> CheckVerdict:
    """``pinned_uuids`` is what ADMIT pinned (None for a box at ADMIT). ``proof`` is the provider in the slot; the
    default admits nobody."""
    scrape = scrape_host(
        runner,
        agent_container=config.agent_container,
        disk_path=config.disk_path,
        network_targets=config.network_targets,
        timeout=config.ssh_timeout_s,
    )
    checks = identity_checks(
        scrape,
        config.spec,
        pinned_uuids,
        allowlist,
        config.agent_image_digests,
        config.power_min_ratio,
        config.disk_min_free_gb,
        config.disk_path,
        config.network_targets,
    )
    if all(c.passed for c in checks):
        checks.append(check_gpu_proof(runner, scrape.gpus, proof, config.proof_image, config.proof_timeout_s))
    else:
        failed = [c.name for c in checks if not c.passed]
        checks.append(CheckResult(GPU_PROOF, False, {'reason': f'skipped: {", ".join(failed)} failed'}, True))
    card_name = scrape.gpus[0].name if scrape.gpus else ''
    return CheckVerdict.from_checks(checks, scrape.uuids, card_name, scrape.driver, now)
