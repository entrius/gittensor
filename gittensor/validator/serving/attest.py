# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Hardware attestation of serving miners (sub-subnet B beta).

Each round a random half of the READY miners (plus every miner that has never passed and every miner that failed
last round) is sent one fresh ``AttestSynapse`` — same seed, same instant. The miner's attest sidecar (docker/attest,
image ``entrius/gt-attest``) answers with ``gt_attest`` on every GPU it can see: it fills the card's free VRAM with a
seeded stream and runs a deterministic fp32 GEMM chain, returning the GPU's UUID, bytes filled, a SHA-256 digest and
wall time. The validator asks its own reference sidecar (same image) for the same seed first, so the expected digest
and reference wall time come from an honest 5090 — no GPU code runs in the validator process.

A card PASSES when its digest matches, its wall is within ``SERVING_ATTEST_BUDGET_RATIO`` x the reference's, most of
its free VRAM was filled, and the model was resident before the fill (free VRAM at most total minus
``SERVING_ATTEST_MODEL_RESIDENT_RATIO`` x the reservation). A hotkey's ``capacity`` is its number of passing cards —
one hotkey, N cards, N card-hours — and it is attested while at least one passes. Overlap is the mechanism: two
hotkeys fronting one card cannot both fill its free VRAM at the same instant and their chains run ~2x slower, so a
sharing pair is caught within a few rounds (P = fraction^2 per round). Two hotkeys reporting the same GPU UUID within
``SERVING_ATTEST_UUID_MEMORY_ROUNDS`` rounds both fail. A failure is never a strike: capacity 0 for the round,
re-challenged next round. Miners not in the cohort keep their last verdict; a miner with no verdict yet is not READY
(admission).
"""

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, cast

import bittensor as bt
import requests

from gittensor.constants import (
    SERVING_ATTEST_BUDGET_RATIO,
    SERVING_ATTEST_COHORT_FRACTION,
    SERVING_ATTEST_ITERS,
    SERVING_ATTEST_MIN_FILL_RATIO,
    SERVING_ATTEST_MODEL_RESIDENT_RATIO,
    SERVING_ATTEST_TIMEOUT,
    SERVING_ATTEST_UUID_MEMORY_ROUNDS,
    SERVING_VRAM_MODEL_RESERVED_BYTES,
)
from gittensor.serving.loadout import ServingRelease
from gittensor.serving.state import ServingState
from gittensor.synapses import AttestSynapse


@dataclass
class CardVerdict:
    passed: bool
    reason: str
    uuid: str = ''
    wall_ms: Optional[float] = None
    filled_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            'passed': self.passed,
            'reason': self.reason,
            'uuid': self.uuid,
            'wall_ms': self.wall_ms,
            'filled_bytes': self.filled_bytes,
        }


@dataclass
class AttestVerdict:
    passed: bool
    reason: str
    uuid: str = ''  # first passing card (telemetry)
    wall_ms: Optional[float] = None
    filled_bytes: int = 0
    cards: List[CardVerdict] = field(default_factory=list)

    @property
    def capacity(self) -> int:
        return sum(1 for c in self.cards if c.passed)

    @property
    def uuids(self) -> List[str]:
        return [c.uuid for c in self.cards if c.passed and c.uuid]

    def as_status(self, now: float, round_no: int) -> dict:
        return {
            'passed': self.passed,
            'reason': self.reason,
            'uuid': self.uuid,
            'uuids': self.uuids,
            'capacity': self.capacity,
            'cards': [c.as_dict() for c in self.cards],
            'wall_ms': self.wall_ms,
            'filled_bytes': self.filled_bytes,
            'ts': now,
            'round': round_no,
        }


def status_capacity(status: Optional[dict]) -> int:
    """Cards a stored attest status pays for (statuses persisted before ``capacity`` existed count one card)."""
    if not status or not status.get('passed'):
        return 0
    return int(status.get('capacity', 1))


def choose_cohort(
    hotkeys: Sequence[str], status: Dict[str, dict], fraction: float = SERVING_ATTEST_COHORT_FRACTION, rng=None
) -> List[str]:
    """``fraction`` of ``hotkeys`` per round: the least recently challenged first (ties broken at random, so the
    membership is still unpredictable), plus every never-attested hotkey and every last-round failure.

    Pure random sampling let one hotkey go unchallenged for many rounds (soak 6: the other of two was drawn three
    rounds running); with recency first, every hotkey is challenged at least every ``1/fraction`` rounds while a
    sharing pair still lands together within a few rounds.
    """
    rng = rng or secrets.SystemRandom()
    pool = list(hotkeys)
    n = min(len(pool), max(1, -(-len(pool) * fraction // 1).__int__())) if pool else 0
    order = sorted(pool, key=lambda hk: (int((status.get(hk) or {}).get('round', -1)), rng.random()))
    chosen = set(order[:n])
    for hk in pool:
        st = status.get(hk)
        if st is None or not st.get('passed'):
            chosen.add(hk)
    return sorted(chosen)


def reference_challenge(release: ServingRelease, seed: int, iters: int, timeout: float) -> Tuple[str, float]:
    """(expected digest, reference wall ms) from the validator's own reference sidecar for ``seed``."""
    if not release.attest_reference_url:
        raise RuntimeError('no attest_reference_url on the release')
    r = requests.post(
        f'{release.attest_reference_url.rstrip("/")}/v1/attest',
        json={'seed': seed, 'iters': iters, 'fill': False},
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    devices = payload.get('devices') or [payload]
    first = devices[0]
    if 'digest' not in first:
        raise RuntimeError(f'reference attest returned no digest: {payload}')
    return str(first['digest']), float(first.get('wall_ms') or 0.0)


def judge_card(
    dev: dict,
    queued_ms: float,
    expected_digest: str,
    ref_wall_ms: float,
    release: ServingRelease,
    budget_ratio: float,
    min_fill_ratio: float,
    resident_ratio: float,
) -> CardVerdict:
    if dev.get('error'):
        return CardVerdict(False, f'challenge error: {dev["error"]}')
    uuid = str(dev.get('uuid') or '')
    wall = float(dev.get('wall_ms') or 0.0) + queued_ms
    filled = int(dev.get('filled_bytes') or 0)
    if str(dev.get('digest')) != expected_digest:
        return CardVerdict(False, 'digest mismatch', uuid, wall, filled)
    budget = budget_ratio * ref_wall_ms if ref_wall_ms > 0 else float('inf')
    if wall > budget:
        return CardVerdict(False, f'too slow: {wall:.0f} ms > {budget:.0f} ms budget', uuid, wall, filled)
    reserved = release.vram_model_reserved_bytes or SERVING_VRAM_MODEL_RESERVED_BYTES
    total = float(dev.get('vram_total') or 0.0)
    expected_free = max(0.0, total - reserved)
    if expected_free > 0 and filled < min_fill_ratio * expected_free:
        return CardVerdict(
            False, f'under-filled: {filled / 1e9:.1f} GB of {expected_free / 1e9:.1f} GB free', uuid, wall, filled
        )
    free_before = dev.get('vram_free_before')
    if total > 0 and free_before is not None and float(free_before) > total - resident_ratio * reserved:
        return CardVerdict(
            False,
            f'model not resident: {float(free_before) / 1e9:.1f} GB free of {total / 1e9:.1f}',
            uuid,
            wall,
            filled,
        )
    return CardVerdict(True, 'ok', uuid, wall, filled)


def judge(
    response: Optional[AttestSynapse],
    expected_digest: str,
    ref_wall_ms: float,
    release: ServingRelease,
    budget_ratio: float = SERVING_ATTEST_BUDGET_RATIO,
    min_fill_ratio: float = SERVING_ATTEST_MIN_FILL_RATIO,
    resident_ratio: float = SERVING_ATTEST_MODEL_RESIDENT_RATIO,
) -> AttestVerdict:
    """Every card the sidecar reported is judged on its own; the hotkey passes with any passing card and its
    capacity is the count. The reason names the first failing card when one fails."""
    if response is None or response.devices is None:
        detail = getattr(response, 'error', None) or getattr(
            getattr(response, 'dendrite', None), 'status_message', None
        )
        return AttestVerdict(False, f'no attestation ({detail or "timeout"})')
    if not response.devices:
        return AttestVerdict(False, 'no devices')
    queued = float(response.queued_ms or 0.0)
    cards = [
        judge_card(dev, queued, expected_digest, ref_wall_ms, release, budget_ratio, min_fill_ratio, resident_ratio)
        for dev in response.devices
    ]
    passing = [c for c in cards if c.passed]
    failing = [c for c in cards if not c.passed]
    lead = passing[0] if passing else cards[0]
    if not passing:
        reason = failing[0].reason
    elif failing:
        reason = f'{len(passing)}/{len(cards)} cards ok ({failing[0].reason})'
    else:
        reason = 'ok' if len(cards) == 1 else f'ok ({len(cards)} cards)'
    return AttestVerdict(bool(passing), reason, lead.uuid, lead.wall_ms, lead.filled_bytes, cards)


async def send_challenges(
    dendrite: bt.Dendrite,
    targets: Sequence[Tuple[int, str, bt.AxonInfo]],
    seed: int,
    iters: int,
    timeout: float = SERVING_ATTEST_TIMEOUT,
) -> Dict[str, Optional[AttestSynapse]]:
    """Fire the same challenge at every target at once; None where the call failed."""

    async def one(axon: bt.AxonInfo) -> Optional[AttestSynapse]:
        try:
            result = await dendrite.call(
                target_axon=axon,
                synapse=AttestSynapse(seed=seed, iters=iters, fill=True),
                timeout=timeout,
                deserialize=False,
            )
            return cast(AttestSynapse, result)
        except Exception as e:  # network / axon fault: judged as no attestation
            bt.logging.debug(f'Serving: attest call failed: {e!r}')
            return None

    results = await asyncio.gather(*(one(axon) for _, _, axon in targets))
    return {hotkey: resp for (_, hotkey, _), resp in zip(targets, results)}


def _status_uuids(st: dict) -> List[str]:
    uuids = st.get('uuids')
    if uuids is None:
        uuids = [st['uuid']] if st.get('uuid') else []
    return [u for u in uuids if u]


def dedupe_uuids(
    state: ServingState, round_no: int, memory_rounds: int = SERVING_ATTEST_UUID_MEMORY_ROUNDS
) -> Dict[str, str]:
    """hotkey -> shared GPU UUID, for every passing hotkey whose card another hotkey also reported within
    ``memory_rounds``."""
    by_uuid: Dict[str, List[str]] = {}
    for hk, st in state.attest_status.items():
        if st.get('passed') and round_no - int(st.get('round', 0)) <= memory_rounds:
            for uuid in _status_uuids(st):
                by_uuid.setdefault(uuid, []).append(hk)
    shared: Dict[str, str] = {}
    for uuid, hks in sorted(by_uuid.items()):
        if len(hks) > 1:
            for hk in hks:
                shared.setdefault(hk, uuid)
    return shared


async def attest_round(
    state: ServingState,
    dendrite: bt.Dendrite,
    candidates: Sequence[Tuple[int, str, bt.AxonInfo]],
    release: ServingRelease,
    rng=None,
    timeout: float = SERVING_ATTEST_TIMEOUT,
) -> Dict[str, int]:
    """Challenge this round's cohort, update ``state.attest_status``; return hotkey -> attested cards (0 = not
    attested) for every candidate.

    Without an ``attest_reference_url`` (localnet / echo) attestation is off and every candidate counts as one card.
    If the reference itself fails, nothing changes this round (neutral).
    """
    round_no = state.attest_round = getattr(state, 'attest_round', 0) + 1
    if not release.attest_reference_url:
        return {hk: 1 for _, hk, _ in candidates}
    hotkeys = [hk for _, hk, _ in candidates]
    cohort = set(choose_cohort(hotkeys, state.attest_status, rng=rng))
    seed = secrets.randbits(63)
    iters = release.attest_iters or SERVING_ATTEST_ITERS
    try:
        expected, ref_wall = await asyncio.to_thread(reference_challenge, release, seed, iters, timeout)
    except Exception as e:
        bt.logging.error(f'Serving: reference attestation failed, attest neutral this round: {e!r}')
        return {hk: status_capacity(state.attest_status.get(hk)) for hk in hotkeys}
    targets = [(uid, hk, axon) for uid, hk, axon in candidates if hk in cohort]
    responses = await send_challenges(dendrite, targets, seed, iters, timeout)
    now = time.time()
    for uid, hk, _ in targets:
        verdict = judge(responses.get(hk), expected, ref_wall, release)
        state.attest_status[hk] = verdict.as_status(now, round_no)
        bt.logging.info(
            f'Serving: UID {uid} attest {"PASS" if verdict.passed else "FAIL"} '
            f'{verdict.wall_ms or 0:.0f} ms (ref {ref_wall:.0f}) fill {verdict.filled_bytes / 1e9:.1f} GB '
            f'cards {verdict.capacity}/{len(verdict.cards)} uuid {verdict.uuid or "-"}'
            f'{"" if verdict.reason.startswith("ok") else " — " + verdict.reason}'
        )
    for hk, uuid in dedupe_uuids(state, round_no).items():
        st = state.attest_status.get(hk, {})
        uid = next((u for u, h, _ in candidates if h == hk), '?')
        bt.logging.warning(f'Serving: UID {uid} attest FAIL — duplicate GPU {uuid} across hotkeys')
        state.attest_status[hk] = {**st, 'passed': False, 'capacity': 0, 'reason': f'duplicate GPU {uuid}'}
    return {hk: status_capacity(state.attest_status.get(hk)) for hk in hotkeys}
