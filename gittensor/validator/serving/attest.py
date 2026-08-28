# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Hardware attestation of serving miners (sub-subnet B beta).

Each round a random half of the READY miners (plus every miner that has never passed and every miner that failed
last round) is sent one fresh ``AttestSynapse`` — same seed, same instant. The miner's runtime image answers with
``gt_attest`` (docker/attest): it fills the card's free VRAM with a seeded stream and runs a deterministic fp32
GEMM chain, returning the GPU's UUID, bytes filled, a SHA-256 digest and wall time. The validator asks its own
reference runtime (same image) for the same seed first, so the expected digest and reference wall time come from
an honest 5090 — no GPU code runs in the validator process.

PASS = digest matches, wall within ``SERVING_ATTEST_BUDGET_RATIO`` x the reference's, and most of the free VRAM was
filled. Overlap is the mechanism: two hotkeys fronting one card cannot both fill its free VRAM at the same instant
and their chains run ~2x slower, so a sharing pair is caught within a few rounds (P = fraction^2 per round). Two
hotkeys reporting the same GPU UUID within ``SERVING_ATTEST_UUID_MEMORY_ROUNDS`` rounds both fail. A failure is
never a strike: capacity 0 for the round, re-challenged next round. Miners not in the cohort keep their last
verdict; a miner with no verdict yet is not READY (admission).
"""

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, cast

import bittensor as bt
import requests

from gittensor.constants import (
    SERVING_ATTEST_BUDGET_RATIO,
    SERVING_ATTEST_COHORT_FRACTION,
    SERVING_ATTEST_ITERS,
    SERVING_ATTEST_MIN_FILL_RATIO,
    SERVING_ATTEST_TIMEOUT,
    SERVING_ATTEST_UUID_MEMORY_ROUNDS,
    SERVING_VRAM_MODEL_RESERVED_BYTES,
)
from gittensor.serving.loadout import ServingRelease
from gittensor.serving.state import ServingState
from gittensor.synapses import AttestSynapse


@dataclass
class AttestVerdict:
    passed: bool
    reason: str
    uuid: str = ''
    wall_ms: Optional[float] = None
    filled_bytes: int = 0

    def as_status(self, now: float, round_no: int) -> dict:
        return {
            'passed': self.passed,
            'reason': self.reason,
            'uuid': self.uuid,
            'wall_ms': self.wall_ms,
            'filled_bytes': self.filled_bytes,
            'ts': now,
            'round': round_no,
        }


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
    """(expected digest, reference wall ms) from the validator's own reference runtime for ``seed``."""
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


def judge(
    response: Optional[AttestSynapse],
    expected_digest: str,
    ref_wall_ms: float,
    release: ServingRelease,
    budget_ratio: float = SERVING_ATTEST_BUDGET_RATIO,
    min_fill_ratio: float = SERVING_ATTEST_MIN_FILL_RATIO,
) -> AttestVerdict:
    if response is None or response.devices is None:
        detail = getattr(response, 'error', None) or getattr(
            getattr(response, 'dendrite', None), 'status_message', None
        )
        return AttestVerdict(False, f'no attestation ({detail or "timeout"})')
    if not response.devices:
        return AttestVerdict(False, 'no devices')
    dev = response.devices[0]
    if dev.get('error'):
        return AttestVerdict(False, f'challenge error: {dev["error"]}')
    uuid = str(dev.get('uuid') or '')
    wall = float(dev.get('wall_ms') or 0.0) + float(response.queued_ms or 0.0)
    filled = int(dev.get('filled_bytes') or 0)
    if str(dev.get('digest')) != expected_digest:
        return AttestVerdict(False, 'digest mismatch', uuid, wall, filled)
    budget = budget_ratio * ref_wall_ms if ref_wall_ms > 0 else float('inf')
    if wall > budget:
        return AttestVerdict(False, f'too slow: {wall:.0f} ms > {budget:.0f} ms budget', uuid, wall, filled)
    reserved = release.vram_model_reserved_bytes or SERVING_VRAM_MODEL_RESERVED_BYTES
    expected_free = max(0.0, float(dev.get('vram_total') or 0.0) - reserved)
    if expected_free > 0 and filled < min_fill_ratio * expected_free:
        return AttestVerdict(
            False, f'under-filled: {filled / 1e9:.1f} GB of {expected_free / 1e9:.1f} GB free', uuid, wall, filled
        )
    return AttestVerdict(True, 'ok', uuid, wall, filled)


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


def dedupe_uuids(
    state: ServingState, round_no: int, memory_rounds: int = SERVING_ATTEST_UUID_MEMORY_ROUNDS
) -> List[str]:
    """Hotkeys whose passing attestation reports a GPU UUID another hotkey reported within ``memory_rounds``."""
    by_uuid: Dict[str, List[str]] = {}
    for hk, st in state.attest_status.items():
        if st.get('uuid') and st.get('passed') and round_no - int(st.get('round', 0)) <= memory_rounds:
            by_uuid.setdefault(st['uuid'], []).append(hk)
    return sorted(hk for hks in by_uuid.values() if len(hks) > 1 for hk in hks)


async def attest_round(
    state: ServingState,
    dendrite: bt.Dendrite,
    candidates: Sequence[Tuple[int, str, bt.AxonInfo]],
    release: ServingRelease,
    rng=None,
    timeout: float = SERVING_ATTEST_TIMEOUT,
) -> Dict[str, bool]:
    """Challenge this round's cohort, update ``state.attest_status``; return hotkey -> attested for every candidate.

    Without an ``attest_reference_url`` (localnet / echo) attestation is off and every candidate counts as attested.
    If the reference itself fails, nothing changes this round (neutral).
    """
    round_no = state.attest_round = getattr(state, 'attest_round', 0) + 1
    if not release.attest_reference_url:
        return {hk: True for _, hk, _ in candidates}
    hotkeys = [hk for _, hk, _ in candidates]
    cohort = set(choose_cohort(hotkeys, state.attest_status, rng=rng))
    seed = secrets.randbits(63)
    iters = release.attest_iters or SERVING_ATTEST_ITERS
    try:
        expected, ref_wall = await asyncio.to_thread(reference_challenge, release, seed, iters, timeout)
    except Exception as e:
        bt.logging.error(f'Serving: reference attestation failed, attest neutral this round: {e!r}')
        return {hk: bool(state.attest_status.get(hk, {}).get('passed')) for hk in hotkeys}
    targets = [(uid, hk, axon) for uid, hk, axon in candidates if hk in cohort]
    responses = await send_challenges(dendrite, targets, seed, iters, timeout)
    now = time.time()
    for uid, hk, _ in targets:
        verdict = judge(responses.get(hk), expected, ref_wall, release)
        state.attest_status[hk] = verdict.as_status(now, round_no)
        bt.logging.info(
            f'Serving: UID {uid} attest {"PASS" if verdict.passed else "FAIL"} '
            f'{verdict.wall_ms or 0:.0f} ms (ref {ref_wall:.0f}) fill {verdict.filled_bytes / 1e9:.1f} GB '
            f'uuid {verdict.uuid or "-"}{"" if verdict.passed else " — " + verdict.reason}'
        )
    for hk in dedupe_uuids(state, round_no):
        st = state.attest_status.get(hk, {})
        if st.get('passed'):
            uid = next((u for u, h, _ in candidates if h == hk), '?')
            bt.logging.warning(f'Serving: UID {uid} attest FAIL — duplicate GPU {st.get("uuid")} across hotkeys')
            state.attest_status[hk] = {**st, 'passed': False, 'reason': f'duplicate GPU {st.get("uuid")}'}
    return {hk: bool(state.attest_status.get(hk, {}).get('passed')) for hk in hotkeys}
