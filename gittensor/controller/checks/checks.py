# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The sub-checks of the full check: each judges one slice of the scrape against the pinned spec and yields a
``CheckResult`` with its evidence. ``check_gpu_proof`` is the one that runs something on the box — the two-phase
GPU proof on every card at once, through whatever provider fills the slot (``gittensor.controller.proof``).

``check_card_free`` asks the heartbeat's exclusivity question of an *idle* card, judging the same device-handle scan
with the same judge (``foreign_holders``, shared with ``heartbeat._device_holders``). Idle pay buys exclusivity, so
the round has to be able to test it without a workload on the card."""

import re
import time
from typing import Callable, Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import (
    PERSISTENCED_COMM,
    DeviceHolder,
    GpuInfo,
    HostScrape,
    parse_device_holders,
)
from gittensor.controller.checks.verdict import CheckResult
from gittensor.controller.proof.slot import GpuProof, ProbeResult, clip, probe_box

GPU_SPEC = 'gpu_spec'
GPU_UUID_PIN = 'gpu_uuid_pin'
FLEET_UUID_UNIQUE = 'fleet_uuid_unique'
NVML_DIGEST = 'nvml_digest'
POWER_LIMIT = 'power_limit'
AGENT_IMAGE = 'agent_image'
DISK_FREE = 'disk_free'
NETWORK = 'network'
CARD_FREE = 'card_free'
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
    observed_digests: Sequence[str],
    allowed_digests: Sequence[str],
    scrape_error: str = '',
    observed_id: str = '',
    allowed_ids: Sequence[str] = (),
) -> CheckResult:
    """The running agent container's image must carry one of the digests we published (prod). A dev box runs a local
    build, which has no repo digest; its exact image ID pinned in config (``allowed_ids``) satisfies the check
    instead. Nothing pinned at all means nothing can be admitted (fail closed) — pin one before pointing the
    controller at a fleet."""
    evidence: Dict[str, object] = {'observed': list(observed_digests), 'allowed': list(allowed_digests)}
    if allowed_ids:
        evidence.update(observed_id=observed_id, allowed_ids=list(allowed_ids))
    if not allowed_digests and not allowed_ids:
        return CheckResult(AGENT_IMAGE, False, {**evidence, 'reason': 'no agent image digest pinned in config'})
    if set(observed_digests) & set(allowed_digests):
        return CheckResult(AGENT_IMAGE, True, evidence)
    if observed_id and observed_id in set(allowed_ids):
        return CheckResult(AGENT_IMAGE, True, {**evidence, 'matched': 'image_id (dev)'})
    if scrape_error:
        return CheckResult(AGENT_IMAGE, False, {**evidence, 'reason': f'docker inspect: {scrape_error}'})
    if allowed_ids:
        return CheckResult(
            AGENT_IMAGE, False, {**evidence, 'reason': 'agent image matches neither a published digest nor a pinned ID'}
        )
    if not observed_digests:
        return CheckResult(
            AGENT_IMAGE, False, {**evidence, 'reason': 'agent container has no repo digest (local build?)'}
        )
    return CheckResult(AGENT_IMAGE, False, {**evidence, 'reason': 'agent image digest is not one we published'})


def check_fleet_uuid_unique(
    box_id: str, observed_uuids: Sequence[str], fleet_uuids: Mapping[str, Iterable[str]]
) -> CheckResult:
    """No card this box reports may be claimed by another box in the fleet — pinned there, or reported there in the
    same probe round. A duplicate GPU UUID anywhere is BENCH, enforced (``23`` §3b; Lium's default only observes)."""
    others = {b: set(u) for b, u in fleet_uuids.items() if b != box_id}
    clashes = {b: sorted(set(observed_uuids) & u) for b, u in sorted(others.items()) if set(observed_uuids) & u}
    evidence: Dict[str, object] = {'observed': list(observed_uuids), 'boxes_compared': len(others)}
    if clashes:
        return CheckResult(
            FLEET_UUID_UNIQUE,
            False,
            {**evidence, 'reason': 'GPU UUID also claimed by ' + ', '.join(clashes), 'clashes': clashes},
        )
    return CheckResult(FLEET_UUID_UNIQUE, True, evidence)


def check_disk_free(
    free_gb: Optional[float], min_gb: float = cfg.DISK_MIN_FREE_GB, path: str = cfg.DISK_PATH
) -> CheckResult:
    evidence = {
        'path': path or 'docker root dir',
        'free_gb': None if free_gb is None else round(free_gb, 1),
        'min_gb': min_gb,
    }
    if free_gb is None:
        return CheckResult(DISK_FREE, False, {**evidence, 'reason': 'df unreadable'})
    if free_gb < min_gb:
        return CheckResult(DISK_FREE, False, {**evidence, 'reason': f'{free_gb:.0f} GB free < {min_gb:.0f} GB'})
    return CheckResult(DISK_FREE, True, evidence)


def check_network(results: Dict[str, Tuple[int, float]], targets: Sequence[str]) -> CheckResult:
    """Evidence only, never a failure (Kimbo 9/19): which targets answered with an HTTP status (any 2xx/3xx/4xx counts
    as reachable; 0 = no connection) and how fast. One missed Hugging Face request benched a healthy serving box and
    took the fleet to zero for 4 h (mainnet 9/19). Reaching the registry and the weights is proved where it matters:
    a box that cannot pull fails its start (``state.record_start``)."""
    evidence = {url: {'http_code': code, 'bytes_per_s': speed} for url, (code, speed) in results.items()}
    unreachable = [url for url in targets if not (200 <= results.get(url, (0, 0.0))[0] < 500)]
    return CheckResult(NETWORK, True, {'targets': evidence, 'unreachable': unreachable})


def foreign_holders(holders: Mapping[int, DeviceHolder], ours: Collection[str]) -> Tuple[List[str], List[int]]:
    """The holders of an NVIDIA device node that are not ours, and the PIDs that exited between the fd scan and their
    cgroup read. A holder passes when its cgroup names one of ``ours`` (our instances' container IDs on this box), or
    when it is the driver's own ``nvidia-persistenced`` on the host and in no container (Kimbo 9/15). Shared by the
    heartbeat (``heartbeat._device_holders``, on a leased card) and the round (``check_card_free``, on any card)."""
    mine = set(ours)
    foreign: List[str] = []
    exited: List[int] = []
    for holder in sorted(holders.values(), key=lambda h: h.pid):
        devices = ', '.join(holder.devices)
        if not holder.read:
            foreign.append(f'pid {holder.pid} holds {devices}: its cgroup was not read')
        elif holder.containers is None:
            exited.append(holder.pid)  # gone between the fd scan and its cgroup read: it holds nothing now
        elif holder.containers & mine or (not holder.containers and holder.comm == PERSISTENCED_COMM):
            continue
        else:
            where = ', '.join(sorted(i[:12] for i in holder.containers)) or 'no container'
            foreign.append(f'pid {holder.pid} ({holder.comm or "?"}) in {where} holds {devices}')
    return foreign, exited


def check_card_free(
    holders: str, ours: Collection[str], scrape_error: str = '', enforcing: bool = cfg.CARD_FREE_ENFORCING
) -> CheckResult:
    """Exclusivity, judged in the round instead of only while a workload is leased: nothing outside our own instances
    may hold an NVIDIA device node. ``holders`` is ``scrape.device_holders`` (``DEVICE_HOLDERS_COMMAND``'s raw
    stdout), ``ours`` the container IDs of our instances on the box (empty on a fully idle box).

    Idle pay buys exclusivity, and until this check the fleet only ever tested it with a workload on the card
    (``heartbeat``): a box whose GPU another session holds (a desktop, mainnet 9/19) passed the round, earned standby
    pay, and was caught only when a rotation happened to place work on it — and rotation prefers higher standing, so
    spare capacity tested a known-bad card *less* often. Evidence only on first release
    (``cfg.CARD_FREE_ENFORCING``, as the network check is): the verdict is logged and nothing is benched until the
    flag is flipped. A scan that could not run is no answer to judge, never a bench (9/19)."""
    parsed = parse_device_holders(holders)
    foreign, exited = foreign_holders(parsed, ours)
    evidence: Dict[str, object] = {
        'ours': sorted(c[:12] for c in ours),
        'holders': sorted(parsed),
        'exited_mid_scan': exited,
        'enforcing': enforcing,
    }
    if scrape_error:
        return CheckResult(
            CARD_FREE,
            not enforcing,
            {**evidence, 'reason': f'cannot scan device handles: {scrape_error}'},
            not_run=enforcing,
        )
    if foreign:
        reason = 'foreign device holder(s): ' + '; '.join(foreign)[:400]
        return CheckResult(CARD_FREE, not enforcing, {**evidence, 'reason': reason, 'foreign': foreign})
    return CheckResult(CARD_FREE, True, {**evidence, 'reason': f'{len(parsed)} device holder(s), none foreign'})


def check_gpu_proof(
    runner: HostRunner,
    gpus: Sequence[GpuInfo],
    proof: GpuProof,
    image: str = '',
    timeout: float = cfg.PROOF_JOB_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Stage the provider's proof on the box, fire it on every card at the same instant, judge each card. Every card
    must pass. Nothing passes without an answer: a proof that could not be carried out (no provider, a staging failure,
    a container that never started) is ``not_run``; a dead transport mid-proof or an empty box fails, reason named."""
    return proof_result(probe_box(runner, gpus, proof, image, timeout, clock))


def proof_result(probe: ProbeResult) -> CheckResult:
    """A probe's cards as the ``gpu_proof`` check: every card must pass. A proof that could not be carried out (staging
    failed, no container started) is ``not_run``, not a failure: no answer was judged. The fleet round (stage
    everywhere, then fire everywhere) builds its ``ProbeResult`` itself and judges it here too."""
    evidence = {'provider': probe.provider, 'cards': probe.cards}
    if probe.error:
        return CheckResult(GPU_PROOF, False, {**evidence, 'reason': probe.error}, not_run=True)
    if not probe.cards:
        return CheckResult(GPU_PROOF, False, {**evidence, 'reason': 'no card answered'})
    if probe.failures:
        reason = clip('; '.join(probe.failures))
        return CheckResult(GPU_PROOF, False, {**evidence, 'reason': reason}, not_run=probe.not_run)
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
    agent_image_ids: Sequence[str] = (),
    box_id: str = '',
    fleet_uuids: Optional[Mapping[str, Iterable[str]]] = None,
    ours: Collection[str] = (),
    card_free_enforcing: bool = cfg.CARD_FREE_ENFORCING,
) -> List[CheckResult]:
    """Everything except the GPU proof, from one scrape. ``fleet_uuids`` (``{box_id: uuids}`` of every other box)
    adds the fleet-wide uniqueness check; without it the box is judged alone. ``ours`` (our instances' container IDs
    on this box) is what ``check_card_free`` judges the box's device holders against."""
    checks = [
        check_gpu_spec(scrape.gpus, spec, scrape.errors.get('nvidia_smi', '')),
        check_uuid_pin(scrape.gpus, pinned_uuids),
    ]
    if fleet_uuids is not None:
        checks.append(check_fleet_uuid_unique(box_id, scrape.uuids, fleet_uuids))
    return [
        *checks,
        allowlist.judge(scrape.driver, scrape.nvml_md5, scrape.kernel_driver),
        check_power_limit(scrape.gpus, power_min_ratio),
        check_agent_image(
            scrape.agent_image_digests,
            agent_image_digests,
            scrape.errors.get('agent_image', ''),
            scrape.agent_image_id,
            agent_image_ids,
        ),
        check_disk_free(scrape.disk_free_gb, disk_min_free_gb, disk_path),
        check_card_free(scrape.device_holders, ours, scrape.errors.get('device_holders', ''), card_free_enforcing),
        check_network(scrape.network, network_targets),
    ]
