# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The sub-checks of the full check: each judges one slice of the scrape against the pinned spec and yields a
``CheckResult`` with its evidence. ``check_gpu_proof`` is the one that runs something on the box — the one-shot
challenge job, per pinned card, judged against the bank."""

import json
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from gittensor.controller.challenge.bank import (
    BankConsumer,
    BankDepleted,
    ChallengeParams,
    job_command_str,
    judge_answer,
)
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import GpuInfo, HostScrape
from gittensor.controller.checks.verdict import CheckResult

GPU_SPEC = 'gpu_spec'
GPU_UUID_PIN = 'gpu_uuid_pin'
NVML_DIGEST = 'nvml_digest'
POWER_LIMIT = 'power_limit'
AGENT_IMAGE = 'agent_image'
DISK_FREE = 'disk_free'
NETWORK = 'network'
GPU_PROOF = 'gpu_proof'

_UUID = re.compile(r'^GPU-[0-9a-fA-F-]{20,}$')


def check_gpu_spec(gpus: Sequence[GpuInfo], spec: cfg.CardSpec, scrape_error: str = '') -> CheckResult:
    """Count within the spec's range and every card the pinned model: exact name, compute capability, VRAM in range,
    a well-formed UUID."""
    cards = [g.as_dict() for g in gpus]
    if scrape_error:
        return CheckResult(GPU_SPEC, False, {'reason': f'nvidia-smi: {scrape_error}', 'gpus': cards})
    if not spec.count_min <= len(gpus) <= spec.count_max:
        return CheckResult(
            GPU_SPEC,
            False,
            {'reason': f'{len(gpus)} GPUs, spec allows {spec.count_min}-{spec.count_max}', 'gpus': cards},
        )
    offending: List[str] = []
    for g in gpus:
        if g.name.strip() != spec.name:
            offending.append(f'{g.uuid}: model {g.name!r} != {spec.name!r}')
        if g.compute_cap.strip() != spec.compute_cap:
            offending.append(f'{g.uuid}: compute_cap {g.compute_cap!r} != {spec.compute_cap!r}')
        if g.memory_total_mib is None or not spec.vram_total_mib_min <= g.memory_total_mib <= spec.vram_total_mib_max:
            offending.append(
                f'{g.uuid}: VRAM {g.memory_total_mib} MiB outside {spec.vram_total_mib_min}-{spec.vram_total_mib_max}'
            )
        if not _UUID.match(g.uuid):
            offending.append(f'malformed UUID {g.uuid!r}')
    if offending:
        return CheckResult(GPU_SPEC, False, {'reason': '; '.join(offending)[:500], 'gpus': cards})
    return CheckResult(GPU_SPEC, True, {'count': len(gpus), 'model': spec.name, 'gpus': cards})


def check_uuid_pin(gpus: Sequence[GpuInfo], pinned_uuids: Optional[Sequence[str]]) -> CheckResult:
    """The set of UUIDs now must equal the set pinned at ADMIT — a swapped, missing or extra card all fail (Lium's
    ``gpu_fingerprint`` + ``spec_change``). With no pin yet (a box at ADMIT) the check passes and the caller pins
    what it saw. Duplicate UUIDs on one box always fail."""
    observed = [g.uuid for g in gpus]
    evidence: Dict[str, object] = {'observed': observed, 'pinned': list(pinned_uuids or [])}
    if len(set(observed)) != len(observed):
        return CheckResult(GPU_UUID_PIN, False, {**evidence, 'reason': 'duplicate GPU UUIDs'})
    if not pinned_uuids:
        return CheckResult(GPU_UUID_PIN, True, {**evidence, 'reason': 'no pin yet: pinning at ADMIT'})
    missing = sorted(set(pinned_uuids) - set(observed))
    extra = sorted(set(observed) - set(pinned_uuids))
    if missing or extra:
        return CheckResult(
            GPU_UUID_PIN, False, {**evidence, 'reason': 'UUIDs changed since ADMIT', 'missing': missing, 'extra': extra}
        )
    return CheckResult(GPU_UUID_PIN, True, evidence)


def check_power_limit(gpus: Sequence[GpuInfo], min_ratio: float = cfg.POWER_LIMIT_MIN_RATIO) -> CheckResult:
    """Every card's current power limit at least ``min_ratio`` of its default. A card that does not report both
    numbers fails closed (Lium counts those as "incomplete" and still passes; we do not)."""
    readings = []
    low: List[str] = []
    incomplete: List[str] = []
    for g in gpus:
        if g.power_limit_w is None or not g.power_default_limit_w:
            incomplete.append(g.uuid)
            readings.append({'uuid': g.uuid, 'limit_w': g.power_limit_w, 'default_w': g.power_default_limit_w})
            continue
        ratio = g.power_limit_w / g.power_default_limit_w
        readings.append(
            {'uuid': g.uuid, 'limit_w': g.power_limit_w, 'default_w': g.power_default_limit_w, 'ratio': round(ratio, 4)}
        )
        if ratio < min_ratio:
            low.append(f'{g.uuid}: {g.power_limit_w:.0f} W / {g.power_default_limit_w:.0f} W = {ratio:.2f}')
    evidence = {'min_ratio': min_ratio, 'readings': readings}
    if not gpus:
        return CheckResult(POWER_LIMIT, False, {**evidence, 'reason': 'no GPUs'})
    if low:
        return CheckResult(POWER_LIMIT, False, {**evidence, 'reason': 'power limit below floor: ' + '; '.join(low)})
    if incomplete:
        return CheckResult(
            POWER_LIMIT, False, {**evidence, 'reason': 'power limit not reported', 'incomplete': incomplete}
        )
    return CheckResult(POWER_LIMIT, True, evidence)


def check_agent_image(
    observed_digests: Sequence[str], allowed_digests: Sequence[str], scrape_error: str = ''
) -> CheckResult:
    """The running agent container's image must carry one of the digests we published. No pinned digest configured
    means nothing can be admitted (fail closed) — pin one before pointing the controller at a fleet."""
    evidence = {'observed': list(observed_digests), 'allowed': list(allowed_digests)}
    if not allowed_digests:
        return CheckResult(AGENT_IMAGE, False, {**evidence, 'reason': 'no agent image digest pinned in config'})
    if scrape_error:
        return CheckResult(AGENT_IMAGE, False, {**evidence, 'reason': f'docker inspect: {scrape_error}'})
    if not observed_digests:
        return CheckResult(
            AGENT_IMAGE, False, {**evidence, 'reason': 'agent container has no repo digest (local build?)'}
        )
    if not set(observed_digests) & set(allowed_digests):
        return CheckResult(AGENT_IMAGE, False, {**evidence, 'reason': 'agent image digest is not one we published'})
    return CheckResult(AGENT_IMAGE, True, evidence)


def check_disk_free(
    free_gb: Optional[float], min_gb: float = cfg.DISK_MIN_FREE_GB, path: str = cfg.DISK_PATH
) -> CheckResult:
    evidence = {'path': path, 'free_gb': None if free_gb is None else round(free_gb, 1), 'min_gb': min_gb}
    if free_gb is None:
        return CheckResult(DISK_FREE, False, {**evidence, 'reason': 'df unreadable'})
    if free_gb < min_gb:
        return CheckResult(DISK_FREE, False, {**evidence, 'reason': f'{free_gb:.0f} GB free < {min_gb:.0f} GB'})
    return CheckResult(DISK_FREE, True, evidence)


def check_network(results: Dict[str, Tuple[int, float]], targets: Sequence[str]) -> CheckResult:
    """Every target answered with an HTTP status (any 2xx/3xx/4xx counts as reachable; 0 = no connection). The
    download speed is recorded as evidence, not judged yet — the weights-download floor is a `needs a card` number."""
    evidence = {url: {'http_code': code, 'bytes_per_s': speed} for url, (code, speed) in results.items()}
    unreachable = [url for url in targets if not (200 <= results.get(url, (0, 0.0))[0] < 500)]
    if unreachable:
        return CheckResult(NETWORK, False, {'reason': 'unreachable: ' + ', '.join(unreachable), 'targets': evidence})
    return CheckResult(NETWORK, True, {'targets': evidence})


def check_gpu_proof(
    runner: HostRunner,
    gpus: Sequence[GpuInfo],
    bank: BankConsumer,
    params: ChallengeParams,
    image: str,
    budget_ratio: float = cfg.CHALLENGE_BUDGET_RATIO,
    min_fill_ratio: float = cfg.CHALLENGE_MIN_FILL_RATIO,
    timeout: float = cfg.CHALLENGE_JOB_TIMEOUT_S,
    clock=time.monotonic,
    rtt_slack_ms: float = cfg.CHALLENGE_RTT_SLACK_MS,
) -> CheckResult:
    """One bank seed per card: run the challenge image on that card alone (``--gpus device=<uuid>``), parse the JSON
    it prints, judge it against the entry. Every card must pass. Our own clock around the whole ``docker run`` is
    judged against the bank's outer clock plus slack (a relay to a card elsewhere pays the round trip). Bank depletion is a failed check that says so."""
    cards = []
    failures: List[str] = []
    for g in gpus:
        try:
            entry = bank.checkout()
        except BankDepleted as e:
            return CheckResult(
                GPU_PROOF, False, {'reason': f'challenge bank depleted: {e}', 'cards': cards, 'bank': bank.status()}
            )
        command = job_command_str(entry.seed, params, image, g.uuid)
        started = clock()
        try:
            result = runner.run(command, timeout=timeout)
            elapsed_ms = (clock() - started) * 1000.0
            answer = (
                json.loads(result.stdout) if result.ok else {'error': (result.stderr or result.stdout).strip()[:300]}
            )
        except Exception as e:  # transport died or the job printed no JSON
            elapsed_ms = (clock() - started) * 1000.0
            answer = {'error': f'{type(e).__name__}: {e}'[:300]}
        verdict = judge_answer(
            entry,
            answer,
            params,
            budget_ratio=budget_ratio,
            min_fill_ratio=min_fill_ratio,
            expected_uuid=g.uuid,
            vram_total_bytes=g.memory_total_bytes,
            elapsed_ms=elapsed_ms,
            rtt_slack_ms=rtt_slack_ms,
        )
        cards.append(
            {
                'uuid': g.uuid,
                'seed': entry.seed,
                'bank_wall_ms': entry.wall_ms,
                'bank_run_ms': entry.run_ms,
                **verdict.as_dict(),
            }
        )
        if not verdict.passed:
            failures.append(f'{g.uuid}: {verdict.reason}')
    evidence = {'cards': cards, 'bank': bank.status()}
    if not gpus:
        return CheckResult(GPU_PROOF, False, {**evidence, 'reason': 'no GPUs to challenge'})
    if failures:
        return CheckResult(GPU_PROOF, False, {**evidence, 'reason': '; '.join(failures)[:500]})
    return CheckResult(GPU_PROOF, True, evidence)


def identity_checks(
    scrape: HostScrape,
    spec: cfg.CardSpec,
    pinned_uuids: Optional[Sequence[str]],
    allowlist,
    agent_image_digests: Sequence[str],
    power_min_ratio: float,
    disk_min_free_gb: float,
    disk_path: str,
    network_targets: Sequence[str],
) -> List[CheckResult]:
    """Everything except the GPU proof, from one scrape."""
    return [
        check_gpu_spec(scrape.gpus, spec, scrape.errors.get('nvidia_smi', '')),
        check_uuid_pin(scrape.gpus, pinned_uuids),
        allowlist.judge(scrape.driver, scrape.nvml_md5, scrape.kernel_driver),
        check_power_limit(scrape.gpus, power_min_ratio),
        check_agent_image(scrape.agent_image_digests, agent_image_digests, scrape.errors.get('agent_image', '')),
        check_disk_free(scrape.disk_free_gb, disk_min_free_gb, disk_path),
        check_network(scrape.network, network_targets),
    ]
