# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The sub-checks of the full check: each judges one slice of the scrape against the pinned spec and yields a
``CheckResult`` with its evidence. ``check_gpu_proof`` is the one that runs something on the box — the two-phase
GPU proof on every card at once, through whatever provider fills the slot (``gittensor.controller.proof``).

``check_card_free`` asks the heartbeat's exclusivity question of an *idle* card, judging the same device-handle scan
with the same judge (``foreign_holders``, shared with ``heartbeat._device_holders``). Idle pay buys exclusivity, so
the round has to be able to test it without a workload on the card.

Every check that can fail writes two things about the failure: ``evidence['reason']``, the full detail, which goes to
``controller.jsonl`` and nowhere else; and ``evidence[why.PUBLIC]``, a code from ``why``'s closed vocabulary plus a
few integers, which is what the miner is shown on the public fleet page. The second is a classification, never a
summary of the first — see ``checks/why.py`` for why the distinction is the whole design."""

import re
import time
from typing import Callable, Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks import why as w
from gittensor.controller.checks.amd_scrape import AmdCard, AmdStack, version_tuple
from gittensor.controller.checks.catalog import spec_for_name, spec_for_pci_id
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import (
    PERSISTENCED_COMM,
    DeviceHolder,
    GpuInfo,
    HostScrape,
    gpu_device_pattern,
    parse_device_holders,
)
from gittensor.controller.checks.vendor import AMD, BOTH, NVIDIA, vendor_or_default
from gittensor.controller.checks.verdict import CheckResult
from gittensor.controller.proof.slot import GpuProof, ProbeResult, clip, probe_box

VENDOR = 'vendor'
GPU_SPEC = 'gpu_spec'
GPU_UUID_PIN = 'gpu_uuid_pin'
FLEET_UUID_UNIQUE = 'fleet_uuid_unique'
NVML_DIGEST = 'nvml_digest'
AMD_STACK = 'amd_stack'
POWER_LIMIT = 'power_limit'
AGENT_IMAGE = 'agent_image'
DISK_FREE = 'disk_free'
NETWORK = 'network'
CARD_FREE = 'card_free'
GPU_PROOF = 'gpu_proof'

_UUID = re.compile(r'^GPU-[0-9a-fA-F-]{20,}$')
_SPEC_CODES = (w.SPEC_MODEL, w.SPEC_COMPUTE_CAP, w.SPEC_VRAM, w.SPEC_BAD_UUID, w.SPEC_ID_MISSING, w.SPEC_PARTITIONED)


def check_vendor(detected: str) -> CheckResult:
    """One GPU vendor per box (30 §3; no mixed boxes, as no mixed types): a box with both the NVIDIA and the AMD
    kernel module loaded is refused by name, not left to read as a broken nvidia-smi. No module at all passes here
    and fails closed in ``gpu_spec`` as it always has."""
    evidence = {'detected': detected, 'vendor': vendor_or_default(detected)}
    if detected == BOTH:
        return CheckResult(
            VENDOR,
            False,
            {**evidence, 'reason': 'both nvidia and amdgpu are loaded', w.PUBLIC: {'code': w.VENDOR_MIXED}},
        )
    return CheckResult(VENDOR, True, evidence)


def check_gpu_spec(gpus: Sequence[GpuInfo], spec: Optional[cfg.CardSpec] = None, scrape_error: str = '') -> CheckResult:
    """A box size the type admits and every card the one model: a name of the type, compute capability, VRAM in
    range, a well-formed UUID. With no ``spec`` given it is the catalog's entry for the first card's name: the box
    says which type it claims, and every card is then held to our numbers for that type. A name the catalog does not
    know, or a type that is listed but not qualified, is not admitted."""
    cards = [g.as_dict() for g in gpus]
    if spec is None and gpus and not scrape_error:
        claimed = gpus[0].name.strip()
        spec = spec_for_name(claimed)
        if spec is None or not spec.qualified:
            why = 'is not in the GPU catalog' if spec is None else f'({spec.gpu_type}) is listed but not qualified yet'
            return CheckResult(
                GPU_SPEC,
                False,
                {'reason': f'model {claimed!r} {why}', 'gpus': cards, w.PUBLIC: {'code': w.SPEC_MODEL, 'n': len(gpus)}},
            )
    spec = spec or cfg.RTX_5090  # nothing scraped: the count and unreadable-scrape answers do not depend on the type
    if scrape_error:
        return CheckResult(
            GPU_SPEC,
            False,
            {
                'reason': f'nvidia-smi: {scrape_error}',
                'gpus': cards,
                w.PUBLIC: {'code': w.SPEC_UNREADABLE},
            },  # fmt: skip
        )
    if len(gpus) not in spec.counts:
        sizes = ', '.join(map(str, spec.counts))
        return CheckResult(
            GPU_SPEC,
            False,
            {
                'reason': f'{len(gpus)} GPUs: a {spec.gpu_type} box is {sizes} cards',
                'gpus': cards,
                # The count is the box's and says nothing it did not advertise; the sizes are ours (29 §1 #3).
                w.PUBLIC: {'code': w.SPEC_CARD_COUNT, 'n': len(gpus)},
            },
        )
    offending: List[str] = []
    wrong: Dict[str, int] = {}  # why code -> cards in it; the public phrase names the first kind we found
    for g in gpus:
        if g.name.strip() not in spec.names:
            offending.append(f'{g.uuid}: model {g.name!r} is not a {spec.gpu_type} ({", ".join(spec.names)})')
            wrong[w.SPEC_MODEL] = wrong.get(w.SPEC_MODEL, 0) + 1
        if g.compute_cap.strip() != spec.compute_cap:
            offending.append(f'{g.uuid}: compute_cap {g.compute_cap!r} != {spec.compute_cap!r}')
            wrong[w.SPEC_COMPUTE_CAP] = wrong.get(w.SPEC_COMPUTE_CAP, 0) + 1
        if g.memory_total_mib is None or not spec.vram_total_mib_min <= g.memory_total_mib <= spec.vram_total_mib_max:
            offending.append(
                f'{g.uuid}: VRAM {g.memory_total_mib} MiB outside {spec.vram_total_mib_min}-{spec.vram_total_mib_max}'
            )
            wrong[w.SPEC_VRAM] = wrong.get(w.SPEC_VRAM, 0) + 1
        if not _UUID.match(g.uuid):
            offending.append(f'malformed UUID {g.uuid!r}')
            wrong[w.SPEC_BAD_UUID] = wrong.get(w.SPEC_BAD_UUID, 0) + 1
    if offending:
        code = next(c for c in (w.SPEC_MODEL, w.SPEC_COMPUTE_CAP, w.SPEC_VRAM, w.SPEC_BAD_UUID) if c in wrong)
        return CheckResult(
            GPU_SPEC,
            False,
            {'reason': '; '.join(offending)[:500], 'gpus': cards, w.PUBLIC: {'code': code, 'n': wrong[code]}},
        )
    return CheckResult(
        GPU_SPEC, True, {'count': len(gpus), 'model': gpus[0].name.strip(), 'gpu_type': spec.gpu_type, 'gpus': cards}
    )


def check_amd_spec(scrape: HostScrape, spec: Optional[cfg.CardSpec] = None) -> CheckResult:
    """The AMD sibling of ``check_gpu_spec`` (30 §3, §4): the type is the catalog row whose PCI ids list the first
    card's device id (never a marketing name), and every card is then held to our numbers for it: the same device id,
    the gfx target KFD reports, VRAM in the window, a usable serial (``AMD-<16 hex>``, non-zero, the one KFD agrees
    with) and a whole card (SPX / NPS1 on an MI300-class part; a card with no partition modes is whole). The display
    name is recorded, not matched. A listed-not-qualified type is refused, as on NVIDIA."""
    gpus = scrape.gpus
    cards = [g.as_dict() for g in gpus]
    error = scrape.errors.get('amd_sysfs', '')
    if error:
        return CheckResult(
            GPU_SPEC,
            False,
            {'reason': f'amd sysfs: {error}', 'gpus': cards, w.PUBLIC: {'code': w.SPEC_SYSFS_UNREADABLE}},
        )
    amd = scrape.amd_cards
    if spec is None and amd:
        spec = spec_for_pci_id(amd[0].device_id)
        if spec is None or not spec.qualified:
            why = 'is not in the GPU catalog' if spec is None else f'({spec.gpu_type}) is listed but not qualified yet'
            claimed = amd[0].device_id + (f' ({amd[0].product_name})' if amd[0].product_name else '')
            return CheckResult(
                GPU_SPEC,
                False,
                {'reason': f'model {claimed} {why}', 'gpus': cards, w.PUBLIC: {'code': w.SPEC_MODEL, 'n': len(amd)}},
            )
    if spec is None or spec.vendor != AMD:
        counts = cfg.RTX_5090.counts if spec is None else spec.counts
        if amd and len(amd) in counts and spec is not None:
            reason = f'{spec.gpu_type} is not an AMD type'
        else:
            reason = (
                f'{len(amd)} AMD cards: a box is {", ".join(map(str, counts))} cards' if amd else 'no AMD card in sysfs'
            )
        return CheckResult(
            GPU_SPEC, False, {'reason': reason, 'gpus': cards, w.PUBLIC: {'code': w.SPEC_CARD_COUNT, 'n': len(amd)}}
        )
    if len(amd) not in spec.counts:
        sizes = ', '.join(map(str, spec.counts))
        return CheckResult(
            GPU_SPEC,
            False,
            {
                'reason': f'{len(amd)} GPUs: a {spec.gpu_type} box is {sizes} cards',
                'gpus': cards,
                w.PUBLIC: {'code': w.SPEC_CARD_COUNT, 'n': len(amd)},
            },
        )
    offending: List[str] = []
    wrong: Dict[str, int] = {}

    def flag(code: str, text: str) -> None:
        offending.append(text)
        wrong[code] = wrong.get(code, 0) + 1

    for c in amd:
        if c.device_id not in spec.pci_ids:
            flag(w.SPEC_MODEL, f'{c.uuid}: device {c.device_id!r} is not a {spec.gpu_type} ({", ".join(spec.pci_ids)})')
        if c.gfx_target != spec.gfx_target:
            flag(w.SPEC_COMPUTE_CAP, f'{c.uuid}: gfx target {c.gfx_target!r} != {spec.gfx_target!r}')
        mib = c.memory_total_mib
        if mib is None or not spec.vram_total_mib_min <= mib <= spec.vram_total_mib_max:
            flag(w.SPEC_VRAM, f'{c.uuid}: VRAM {mib} MiB outside {spec.vram_total_mib_min}-{spec.vram_total_mib_max}')
        if not c.id_ok:
            kfd = f', KFD says {c.kfd_unique_id!r}' if c.kfd_unique_id and c.kfd_unique_id != c.unique_id else ''
            flag(w.SPEC_ID_MISSING, f'{c.render_node}: no usable serial ({c.unique_id!r}{kfd})')
        if not c.whole:
            flag(w.SPEC_PARTITIONED, f'{c.uuid}: partitioned {c.partition}, the pool admits SPX/NPS1 only')
    if offending:
        code = next(code for code in _SPEC_CODES if code in wrong)
        return CheckResult(
            GPU_SPEC,
            False,
            {'reason': '; '.join(offending)[:500], 'gpus': cards, w.PUBLIC: {'code': code, 'n': wrong[code]}},
        )
    return CheckResult(
        GPU_SPEC,
        True,
        {
            'count': len(amd),
            'model': spec.name,
            'gpu_type': spec.gpu_type,
            'gfx_target': spec.gfx_target,
            'gpus': cards,
            'partition': {c.uuid: c.partition for c in amd},
            'product_names': sorted({c.product_name for c in amd if c.product_name}),
        },
    )


def check_amd_stack(
    stack: Optional[AmdStack],
    cards: Sequence[AmdCard],
    scrape_error: str = '',
    kernel_min: Tuple[int, int] = cfg.AMD_KERNEL_MIN,
    dkms_min: Tuple[int, int] = cfg.AMD_DKMS_MIN,
) -> CheckResult:
    """The AMD sibling of the NVML allowlist (30 §14 #2): the stack is recorded (kernel release, the DKMS amdgpu
    version when there is one, the VBIOS per card) for the support and offer pages, and the only failure is the
    version floor. There is no allowlist: the driver is the kernel's, and an allowlist of kernel releases would fail
    closed on every distro update for nothing the proof does not already cover."""
    record = {**(stack.as_dict() if stack else {}), 'vbios': {c.uuid: c.vbios for c in cards}}
    evidence: Dict[str, object] = {'record': record, 'kernel_min': list(kernel_min), 'dkms_min': list(dkms_min)}
    if scrape_error or stack is None or not stack.kernel:
        return CheckResult(
            AMD_STACK,
            False,
            {
                **evidence,
                'reason': scrape_error or 'no kernel release in the scrape',
                w.PUBLIC: {'code': w.STACK_UNREADABLE},
            },  # fmt: skip
        )
    kernel_ok = version_tuple(stack.kernel) >= kernel_min
    dkms_ok = bool(stack.amdgpu) and version_tuple(stack.amdgpu) >= dkms_min
    if not kernel_ok and not dkms_ok:
        return CheckResult(
            AMD_STACK,
            False,
            {
                **evidence,
                # the versions are the box's and stay in the log; the floor is ours
                'reason': f'kernel {stack.kernel} below {kernel_min[0]}.{kernel_min[1]} and no DKMS amdgpu at or above {dkms_min[0]}.{dkms_min[1]}',  # noqa: E501
                w.PUBLIC: {
                    'code': w.STACK_BELOW_FLOOR,
                    'kmaj': kernel_min[0],
                    'kmin': kernel_min[1],
                    'dmaj': dkms_min[0],
                    'dmin': dkms_min[1],
                },
            },
        )
    return CheckResult(AMD_STACK, True, {**evidence, 'passed_on': 'kernel' if kernel_ok else 'dkms'})


def check_uuid_pin(gpus: Sequence[GpuInfo], pinned_uuids: Optional[Sequence[str]]) -> CheckResult:
    """The set of UUIDs now must equal the set pinned at ADMIT — a swapped, missing or extra card all fail (Lium's
    ``gpu_fingerprint`` + ``spec_change``). With no pin yet (a box at ADMIT) the check passes and the caller pins
    what it saw. Duplicate UUIDs on one box always fail."""
    observed = [g.uuid for g in gpus]
    evidence: Dict[str, object] = {'observed': observed, 'pinned': list(pinned_uuids or [])}
    if len(set(observed)) != len(observed):
        return CheckResult(
            GPU_UUID_PIN, False, {**evidence, 'reason': 'duplicate GPU UUIDs', w.PUBLIC: {'code': w.UUID_DUPLICATE}}
        )
    if not pinned_uuids:
        return CheckResult(GPU_UUID_PIN, True, {**evidence, 'reason': 'no pin yet: pinning at ADMIT'})
    missing = sorted(set(pinned_uuids) - set(observed))
    extra = sorted(set(observed) - set(pinned_uuids))
    if missing or extra:
        return CheckResult(
            GPU_UUID_PIN,
            False,
            {
                **evidence,
                'reason': 'UUIDs changed since ADMIT',
                'missing': missing,
                'extra': extra,
                w.PUBLIC: {'code': w.UUID_CHANGED, 'missing': len(missing), 'extra': len(extra)},
            },
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
        return CheckResult(POWER_LIMIT, False, {**evidence, 'reason': 'no GPUs', w.PUBLIC: {'code': w.POWER_NO_GPUS}})
    if low:
        return CheckResult(
            POWER_LIMIT,
            False,
            {
                **evidence,
                'reason': 'power limit below floor: ' + '; '.join(low),
                # The floor is ours; the watts are theirs and stay in the log.
                w.PUBLIC: {'code': w.POWER_BELOW_FLOOR, 'n': len(low), 'pct': int(round(min_ratio * 100))},
            },
        )
    if incomplete:
        return CheckResult(
            POWER_LIMIT,
            False,
            {
                **evidence,
                'reason': 'power limit not reported',
                'incomplete': incomplete,
                w.PUBLIC: {'code': w.POWER_UNREPORTED, 'n': len(incomplete)},
            },
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
        return CheckResult(
            AGENT_IMAGE,
            False,
            {
                **evidence,
                'reason': 'no agent image digest pinned in config',
                w.PUBLIC: {'code': w.AGENT_IMAGE_UNPINNED},
            },  # fmt: skip
        )
    if set(observed_digests) & set(allowed_digests):
        return CheckResult(AGENT_IMAGE, True, evidence)
    if observed_id and observed_id in set(allowed_ids):
        return CheckResult(AGENT_IMAGE, True, {**evidence, 'matched': 'image_id (dev)'})
    if scrape_error:
        return CheckResult(
            AGENT_IMAGE,
            False,
            {**evidence, 'reason': f'docker inspect: {scrape_error}', w.PUBLIC: {'code': w.AGENT_IMAGE_UNREADABLE}},
        )
    if allowed_ids:
        return CheckResult(
            AGENT_IMAGE,
            False,
            {
                **evidence,
                'reason': 'agent image matches neither a published digest nor a pinned ID',
                w.PUBLIC: {'code': w.AGENT_IMAGE_MISMATCH},
            },
        )
    if not observed_digests:
        return CheckResult(
            AGENT_IMAGE,
            False,
            {
                **evidence,
                'reason': 'agent container has no repo digest (local build?)',
                w.PUBLIC: {'code': w.AGENT_IMAGE_LOCAL_BUILD},
            },
        )
    return CheckResult(
        AGENT_IMAGE,
        False,
        {
            **evidence,
            'reason': 'agent image digest is not one we published',
            w.PUBLIC: {'code': w.AGENT_IMAGE_MISMATCH},
        },  # fmt: skip
    )


def check_fleet_uuid_unique(
    box_id: str, observed_uuids: Sequence[str], fleet_uuids: Mapping[str, Iterable[str]]
) -> CheckResult:
    """No card this box reports may be claimed by another box in the fleet — pinned there, or reported there in the
    same probe round. A duplicate GPU UUID anywhere is BENCH, enforced (``23`` §3b; Lium's default only observes)."""
    others = {b: set(u) for b, u in fleet_uuids.items() if b != box_id}
    clashes = {b: sorted(set(observed_uuids) & u) for b, u in sorted(others.items()) if set(observed_uuids) & u}
    evidence: Dict[str, object] = {'observed': list(observed_uuids), 'boxes_compared': len(others)}
    if clashes:
        claimed = len({u for us in clashes.values() for u in us})
        return CheckResult(
            FLEET_UUID_UNIQUE,
            False,
            {
                **evidence,
                'reason': 'GPU UUID also claimed by ' + ', '.join(clashes),
                'clashes': clashes,
                # The other box's hotkey is not this miner's business, and is not ours to publish: the count is.
                w.PUBLIC: {'code': w.UUID_CLAIMED_ELSEWHERE, 'n': claimed},
            },
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
        return CheckResult(DISK_FREE, False, {**evidence, 'reason': 'df unreadable', w.PUBLIC: {'code': w.DISK_UNREADABLE}})  # fmt: skip
    if free_gb < min_gb:
        return CheckResult(
            DISK_FREE,
            False,
            {
                # Our floor, never their reading: how much room a box has left is its operator's business.
                **evidence,
                'reason': f'{free_gb:.0f} GB free < {min_gb:.0f} GB',
                w.PUBLIC: {'code': w.DISK_BELOW_FLOOR, 'floor_gb': min_gb},
            },
        )
    return CheckResult(DISK_FREE, True, evidence)


def check_network(results: Dict[str, Tuple[int, float]], targets: Sequence[str]) -> CheckResult:
    """Evidence only, never a failure (Kimbo 9/19): which targets answered with an HTTP status (any 2xx/3xx/4xx counts
    as reachable; 0 = no connection) and how fast. One missed Hugging Face request benched a healthy serving box and
    took the fleet to zero for 4 h (mainnet 9/19). Reaching the registry and the weights is proved where it matters:
    a box that cannot pull fails its start (``state.record_start``)."""
    evidence = {url: {'http_code': code, 'bytes_per_s': speed} for url, (code, speed) in results.items()}
    unreachable = [url for url in targets if not (200 <= results.get(url, (0, 0.0))[0] < 500)]
    return CheckResult(NETWORK, True, {'targets': evidence, 'unreachable': unreachable})


def foreign_holders(
    holders: Mapping[int, DeviceHolder], ours: Collection[str]
) -> Tuple[List[str], List[int], List[str]]:
    """The holders of an NVIDIA device node that are not ours, the PIDs that exited between the fd scan and their
    cgroup read, and one ``why`` bucket per foreign holder. A holder passes when its cgroup names one of ``ours``
    (our instances' container IDs on this box), or when it is the driver's own ``nvidia-persistenced`` on the host and
    in no container (Kimbo 9/15). Shared by the heartbeat (``heartbeat._device_holders``, on a leased card) and the
    round (``check_card_free``, on any card).

    The reason strings carry the PID, the comm and the container ID: they are the operator's log and go no further.
    The buckets carry no part of them — ``why.holder_category`` reads the comm only to decide which of our own
    constants it is nearest — and are what the miner is shown."""
    mine = set(ours)
    foreign: List[str] = []
    exited: List[int] = []
    buckets: List[str] = []
    for holder in sorted(holders.values(), key=lambda h: h.pid):
        devices = ', '.join(holder.devices)
        if not holder.read:
            foreign.append(f'pid {holder.pid} holds {devices}: its cgroup was not read')
            buckets.append(w.holder_category('', None, read=False))
        elif holder.containers is None:
            exited.append(holder.pid)  # gone between the fd scan and its cgroup read: it holds nothing now
        elif holder.containers & mine or (not holder.containers and holder.comm == PERSISTENCED_COMM):
            continue
        else:
            where = ', '.join(sorted(i[:12] for i in holder.containers)) or 'no container'
            foreign.append(f'pid {holder.pid} ({holder.comm or "?"}) in {where} holds {devices}')
            buckets.append(w.holder_category(holder.comm, holder.containers, read=True))
    return foreign, exited, buckets


def check_card_free(holders: str, ours: Collection[str], scrape_error: str = '', vendor: str = NVIDIA) -> CheckResult:
    """Exclusivity, judged in the round instead of only while a workload is leased: nothing outside our own instances
    may hold an NVIDIA device node. ``holders`` is ``scrape.device_holders`` (``DEVICE_HOLDERS_COMMAND``'s raw
    stdout), ``ours`` the container IDs of our instances on the box (empty on a fully idle box).

    Idle pay buys exclusivity, and until this check the fleet only ever tested it with a workload on the card
    (``heartbeat``): a box whose GPU another session holds (a desktop, mainnet 9/19) passed the round, earned standby
    pay, and was caught only when a rotation happened to place work on it — and rotation prefers higher standing, so
    spare capacity tested a known-bad card *less* often. A foreign holder benches the box on the ladder like any
    other failed check; a scan that could not run is no answer to judge, so it is a strike, never a bench (9/19)."""
    parsed = parse_device_holders(holders, gpu_device_pattern(vendor))
    foreign, exited, buckets = foreign_holders(parsed, ours)
    evidence: Dict[str, object] = {
        'ours': sorted(c[:12] for c in ours),
        'holders': sorted(parsed),
        'exited_mid_scan': exited,
    }
    if scrape_error:
        return CheckResult(
            CARD_FREE,
            False,
            {
                **evidence,
                'reason': f'cannot scan device handles: {scrape_error}',
                w.PUBLIC: {'code': w.CARD_FREE_UNSCANNABLE},
            },  # fmt: skip
            not_run=True,
        )
    if foreign:
        reason = 'foreign device holder(s): ' + '; '.join(foreign)[:400]
        return CheckResult(
            CARD_FREE,
            False,
            {
                **evidence,
                'reason': reason,
                'foreign': foreign,
                'buckets': buckets,
                w.PUBLIC: w.card_free_public(buckets),
            },  # fmt: skip
        )
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


# What a docker error names when it was the NVIDIA container runtime that refused, not our proof. Matched to pick one
# of our own phrases (``why.PROOF_RUNTIME_NVIDIA``); no part of the error text is published. This is the failure that
# cost a miner a 16 h bench and us a log dive on 9/18 — the toolkit on their box, nothing they could see from the page.
_NVIDIA_RUNTIME_ERRORS = ('nvidia-container-cli', 'prestart hook', 'nvidia-container-runtime')
# The AMD sibling: no container runtime is involved (30 §1 #5), so what refuses is the device itself: the node is
# missing or busy after a reset (30 §14 #5), or HSA could not open it.
_AMD_RUNTIME_ERRORS = ('/dev/kfd', '/dev/dri/renderD', 'hsa_init', 'HSA_STATUS_ERROR')


def _proof_public(cards: list) -> dict:
    """The failing cards as one classification. Structural wherever it can be — which card answered, whether the
    container ever started — and a small signature table only for the one error class worth naming."""
    failed = [c for c in cards if not c.get('passed')]
    if not failed:
        return {'code': w.PROOF_BAD_ANSWER, 'n': 0}
    never_started = [c for c in failed if c.get('not_run')]
    if never_started:
        text = ' '.join(str(c.get('reason', '')) for c in never_started)
        if any(e in text for e in _NVIDIA_RUNTIME_ERRORS):
            code = w.PROOF_RUNTIME_NVIDIA
        elif any(e in text for e in _AMD_RUNTIME_ERRORS):
            code = w.PROOF_RUNTIME_AMD
        else:
            code = w.PROOF_CONTAINER
        return {'code': code, 'n': len(never_started)}
    wrong = [c for c in failed if c.get('answered_uuid') and c.get('answered_uuid') != c.get('uuid')]
    if wrong:
        return {'code': w.PROOF_WRONG_CARD, 'n': len(wrong)}
    return {'code': w.PROOF_BAD_ANSWER, 'n': len(failed)}


def proof_result(probe: ProbeResult) -> CheckResult:
    """A probe's cards as the ``gpu_proof`` check: every card must pass. A proof that could not be carried out (staging
    failed, no container started) is ``not_run``, not a failure: no answer was judged. The fleet round (stage
    everywhere, then fire everywhere) builds its ``ProbeResult`` itself and judges it here too."""
    evidence = {'provider': probe.provider, 'cards': probe.cards}
    if probe.error:
        return CheckResult(
            GPU_PROOF, False, {**evidence, 'reason': probe.error, w.PUBLIC: {'code': w.PROOF_STAGING}}, not_run=True
        )
    if not probe.cards:
        return CheckResult(GPU_PROOF, False, {**evidence, 'reason': 'no card answered', w.PUBLIC: {'code': w.PROOF_NO_ANSWER}})  # fmt: skip
    if probe.failures:
        reason = clip('; '.join(probe.failures))
        return CheckResult(
            GPU_PROOF,
            False,
            {**evidence, 'reason': reason, w.PUBLIC: _proof_public(probe.cards)},
            not_run=probe.not_run,  # fmt: skip
        )
    return CheckResult(GPU_PROOF, True, evidence)


def identity_checks(
    scrape: HostScrape,
    spec: Optional[cfg.CardSpec],
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
) -> List[CheckResult]:
    """Everything except the GPU proof, from one scrape. ``fleet_uuids`` (``{box_id: uuids}`` of every other box)
    adds the fleet-wide uniqueness check; without it the box is judged alone. ``ours`` (our instances' container IDs
    on this box) is what ``check_card_free`` judges the box's device holders against."""
    amd = scrape.vendor == AMD  # the vendor switch (30 §3): the spec and the stack have an AMD sibling each
    checks = [
        check_vendor(scrape.vendor_detected),
        check_amd_spec(scrape, spec) if amd else check_gpu_spec(scrape.gpus, spec, scrape.errors.get('nvidia_smi', '')),
        check_uuid_pin(scrape.gpus, pinned_uuids),
    ]
    if fleet_uuids is not None:
        checks.append(check_fleet_uuid_unique(box_id, scrape.uuids, fleet_uuids))
    if amd:
        stack = check_amd_stack(scrape.amd_stack, scrape.amd_cards, scrape.errors.get('amd_sysfs', ''))
    else:
        stack = allowlist.judge(scrape.driver, scrape.nvml_md5, scrape.kernel_driver)
    return [
        *checks,
        stack,
        check_power_limit(scrape.gpus, power_min_ratio),
        check_agent_image(
            scrape.agent_image_digests,
            agent_image_digests,
            scrape.errors.get('agent_image', ''),
            scrape.agent_image_id,
            agent_image_ids,
        ),
        check_disk_free(scrape.disk_free_gb, disk_min_free_gb, disk_path),
        check_card_free(scrape.device_holders, ours, scrape.errors.get('device_holders', ''), scrape.vendor),
        check_network(scrape.network, network_targets),
    ]
