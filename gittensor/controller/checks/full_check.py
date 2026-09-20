# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``run_full_check``: point the controller at a box, get a verdict with evidence (``24`` §3 WS-C).

Scrape once over the runner, judge identity and resources (``checks.identity_checks``), and only if all of that
passed run the GPU proof — a box that already failed identity does not get ~30 GB of its VRAM filled for nothing.
The verdict is ADMIT when every check passed, otherwise BENCH with the failing checks named. Nothing here changes
box state; ``state.apply_verdict`` does that.
"""

from dataclasses import dataclass, field
from typing import Collection, Iterable, List, Mapping, Optional, Sequence, Tuple

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.checks import GPU_PROOF, check_gpu_proof, identity_checks
from gittensor.controller.checks.nvml_allowlist import NvmlAllowlist
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import HostScrape, scrape_host
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.proof.slot import GpuProof, UnconfiguredProof, image_ref


@dataclass(frozen=True)
class FullCheckConfig:
    """The served inputs of a full check, in one place so a test (or a future served config) can vary them."""

    spec: cfg.CardSpec = cfg.RTX_5090
    agent_image_digests: Tuple[str, ...] = ()  # sha256:... digests of the agent images we published
    agent_image_ids: Tuple[str, ...] = ()  # dev only: sha256:... image IDs of local builds, which carry no repo digest
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
    box_id: str = '',
    fleet_uuids: Optional[Mapping[str, Iterable[str]]] = None,
    ours: Collection[str] = (),
) -> CheckVerdict:
    """``pinned_uuids`` is what ADMIT pinned (None for a box at ADMIT). ``proof`` is the provider in the slot; the
    default admits nobody. ``fleet_uuids`` (every other box's UUIDs) adds the fleet-wide uniqueness check; ``ours``
    (our instances' container IDs on this box) is what ``check_card_free`` judges its device holders against."""
    scrape = scrape_box(runner, config)
    checks = judge_identity(scrape, allowlist, pinned_uuids, config, box_id, fleet_uuids, ours)
    if identity_passed(checks):
        checks.append(check_gpu_proof(runner, scrape.gpus, proof, config.proof_image, config.proof_timeout_s))
    else:
        checks.append(proof_skipped(checks))
    return finish_verdict(checks, scrape, now)


# The halves of a full check, for a caller that must see every box's scrape before judging any (the fleet round:
# UUID uniqueness across boxes, then stage everywhere, then fire everywhere).


def scrape_box(runner: HostRunner, config: FullCheckConfig) -> HostScrape:
    return scrape_host(
        runner,
        agent_container=config.agent_container,
        disk_path=config.disk_path,
        network_targets=config.network_targets,
        timeout=config.ssh_timeout_s,
    )


def judge_identity(
    scrape: HostScrape,
    allowlist: NvmlAllowlist,
    pinned_uuids: Optional[Sequence[str]],
    config: FullCheckConfig,
    box_id: str = '',
    fleet_uuids: Optional[Mapping[str, Iterable[str]]] = None,
    ours: Collection[str] = (),
) -> List[CheckResult]:
    """Every check except the GPU proof, from one scrape."""
    return identity_checks(
        scrape,
        config.spec,
        pinned_uuids,
        allowlist,
        config.agent_image_digests,
        config.power_min_ratio,
        config.disk_min_free_gb,
        config.disk_path,
        config.network_targets,
        config.agent_image_ids,
        box_id,
        fleet_uuids,
        ours,
    )


def identity_passed(checks: Sequence[CheckResult]) -> bool:
    """Nothing failed. A check that could not be carried out (``not_run``: ``card_free`` when the host procfs is not
    where we look) named no failure, so it does not skip the proof either — the box is otherwise fine and the verdict
    resolves to NOT_RUN, a strike (9/19), rather than a bench with nothing named."""
    return all(c.passed or c.not_run for c in checks)


def proof_skipped(checks: Sequence[CheckResult]) -> CheckResult:
    failed = [c.name for c in checks if not c.passed and not c.not_run]
    return CheckResult(GPU_PROOF, False, {'reason': f'skipped: {", ".join(failed)} failed'}, True)


def finish_verdict(checks: List[CheckResult], scrape: HostScrape, now: Optional[float] = None) -> CheckVerdict:
    card_name = scrape.gpus[0].name if scrape.gpus else ''
    return CheckVerdict.from_checks(checks, scrape.uuids, card_name, scrape.driver, now)
