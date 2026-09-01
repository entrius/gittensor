# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Hardware attestation of serving miners (sub-subnet B beta): admission, pass/fail per hotkey.

Attestation answers one question — is there a real 5090 running the real model behind this hotkey? — and nothing
here counts cards: pay is per token served (forward.py), and a card cannot serve more tokens than its silicon
decodes. Each round the least recently challenged half of the READY miners (plus every miner that has never passed
and every miner that failed last round) is sent one fresh ``AttestSynapse``. The miner's attest sidecar
(docker/attest, image ``entrius/gt-attest``) answers with ``gt_attest`` on every GPU it can see, all at once: device
``i`` fills its free VRAM with a seeded stream and runs a deterministic fp32 GEMM chain on ``seed + i``, returning the
GPU's UUID, bytes filled, a SHA-256 digest and wall time. The validator asks its own reference sidecar (same image)
for index 0 first, and for each further index a miner claims, so every expected digest comes from an honest 5090 —
no GPU code runs in the validator process.

What the validator can measure itself is the digest and the round trip; every other field of a card's report is the
miner's own number. So a card PASSES when its digest matches its index's, its wall is within
``SERVING_ATTEST_BUDGET_RATIO`` x the reference's, most of its free VRAM was filled and the model was resident before
the fill — and the whole reply arrived within that budget plus ``SERVING_ATTEST_RTT_SLACK_MS``. A hotkey passes with
any passing card. A failure is never a strike: not READY for the round, re-challenged next round. Miners not in the
cohort keep their last verdict until it is older than ``SERVING_ATTEST_MEMORY_ROUNDS``; a miner with no verdict yet
is not READY. A fault in this module is neutral for the cohort, never fatal for the round.
"""

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union, cast

import bittensor as bt
import requests

from gittensor.constants import (
    SERVING_ATTEST_BUDGET_RATIO,
    SERVING_ATTEST_COHORT_FRACTION,
    SERVING_ATTEST_ITERS,
    SERVING_ATTEST_MAX_CARDS,
    SERVING_ATTEST_MEMORY_ROUNDS,
    SERVING_ATTEST_MIN_FILL_RATIO,
    SERVING_ATTEST_MODEL_RESIDENT_RATIO,
    SERVING_ATTEST_RTT_SLACK_MS,
    SERVING_ATTEST_TIMEOUT,
    SERVING_VRAM_MODEL_RESERVED_BYTES,
)
from gittensor.serving.loadout import ServingRelease
from gittensor.serving.state import ServingState
from gittensor.synapses import AttestSynapse

ExpectedDigest = Union[str, Callable[[int], Optional[str]]]  # one digest for every index, or a digest per index


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
    def uuids(self) -> List[str]:
        return [c.uuid for c in self.cards if c.passed and c.uuid]

    def as_status(self, now: float, round_no: int) -> dict:
        return {
            'passed': self.passed,
            'reason': self.reason,
            'uuid': self.uuid,
            'uuids': self.uuids,
            'cards': [c.as_dict() for c in self.cards],
            'wall_ms': self.wall_ms,
            'filled_bytes': self.filled_bytes,
            'ts': now,
            'round': round_no,
        }


def status_passed(
    status: Optional[dict], round_no: Optional[int] = None, memory_rounds: int = SERVING_ATTEST_MEMORY_ROUNDS
) -> bool:
    """Does a stored attest status still admit the hotkey? A verdict older than ``memory_rounds`` — a miner the
    reference could not re-challenge for that long — admits nothing until it is renewed."""
    if not status or not status.get('passed'):
        return False
    return round_no is None or round_no - int(status.get('round', round_no)) <= memory_rounds


def choose_cohort(
    hotkeys: Sequence[str], status: Dict[str, dict], fraction: float = SERVING_ATTEST_COHORT_FRACTION, rng=None
) -> List[str]:
    """``fraction`` of ``hotkeys`` per round: the least recently challenged first (ties broken at random, so the
    membership is still unpredictable), plus every never-attested hotkey and every last-round failure.

    Pure random sampling let one hotkey go unchallenged for many rounds (soak 6: the other of two was drawn three
    rounds running); with recency first, every hotkey is challenged at least every ``1/fraction`` rounds.
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
    headers = (
        {'Authorization': f'Bearer {release.attest_reference_api_key}'} if release.attest_reference_api_key else {}
    )
    r = requests.post(
        f'{release.attest_reference_url.rstrip("/")}/v1/attest',
        json={'seed': seed, 'iters': iters, 'fill': False},
        headers=headers,
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
    expected_digest: Optional[str],
    ref_wall_ms: float,
    release: ServingRelease,
    budget_ratio: float,
    min_fill_ratio: float,
    resident_ratio: float,
) -> CardVerdict:
    try:
        return _judge_card(
            dev, queued_ms, expected_digest, ref_wall_ms, release, budget_ratio, min_fill_ratio, resident_ratio
        )
    except (TypeError, ValueError, AttributeError) as e:  # a field the miner typed that is not a number
        return CardVerdict(False, f'malformed device report: {e!r}'[:200])


def _judge_card(
    dev: dict,
    queued_ms: float,
    expected_digest: Optional[str],
    ref_wall_ms: float,
    release: ServingRelease,
    budget_ratio: float,
    min_fill_ratio: float,
    resident_ratio: float,
) -> CardVerdict:
    if dev.get('error'):
        return CardVerdict(False, f'challenge error: {dev["error"]}'[:200])
    uuid = str(dev.get('uuid') or '')
    wall = float(dev.get('wall_ms') or 0.0) + queued_ms
    filled = int(dev.get('filled_bytes') or 0)
    if expected_digest is None:
        return CardVerdict(False, 'no reference digest for this device index', uuid, wall, filled)
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
    expected: ExpectedDigest,
    ref_wall_ms: float,
    release: ServingRelease,
    budget_ratio: float = SERVING_ATTEST_BUDGET_RATIO,
    min_fill_ratio: float = SERVING_ATTEST_MIN_FILL_RATIO,
    resident_ratio: float = SERVING_ATTEST_MODEL_RESIDENT_RATIO,
    elapsed_ms: Optional[float] = None,
    rtt_slack_ms: float = SERVING_ATTEST_RTT_SLACK_MS,
    max_cards: int = SERVING_ATTEST_MAX_CARDS,
) -> AttestVerdict:
    """Every card the sidecar reported is judged on its own against its index's expected digest; the hotkey passes
    with any passing card. ``elapsed_ms`` is the validator's own round trip for the whole reply: past one card's
    budget plus the slack, no card passes, however each card's own wall reads. The reason names the first failing
    card when one fails."""
    if response is None or response.devices is None:
        detail = getattr(response, 'error', None) or getattr(
            getattr(response, 'dendrite', None), 'status_message', None
        )
        return AttestVerdict(False, f'no attestation ({detail or "timeout"})')
    if not response.devices:
        return AttestVerdict(False, 'no devices')
    budget = budget_ratio * ref_wall_ms if ref_wall_ms > 0 else float('inf')
    if elapsed_ms is not None and elapsed_ms > budget + rtt_slack_ms:
        return AttestVerdict(False, f'too slow: {elapsed_ms:.0f} ms round trip > {budget + rtt_slack_ms:.0f} ms')
    try:
        queued = float(response.queued_ms or 0.0)
    except (TypeError, ValueError):
        queued = 0.0
    devices = [dev for dev in response.devices if isinstance(dev, dict)][:max_cards]
    if not devices:
        return AttestVerdict(False, 'malformed device report')
    cards = [
        judge_card(
            dev,
            queued,
            expected if isinstance(expected, str) else expected(i),
            ref_wall_ms,
            release,
            budget_ratio,
            min_fill_ratio,
            resident_ratio,
        )
        for i, dev in enumerate(devices)
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
) -> Dict[str, Tuple[Optional[AttestSynapse], float]]:
    """Fire the same challenge at every target at once; (response, round trip ms) per hotkey, None where the call
    failed. The round trip is the validator's own clock on the whole reply."""

    async def one(axon: bt.AxonInfo) -> Tuple[Optional[AttestSynapse], float]:
        started = time.monotonic()
        try:
            result = await dendrite.call(
                target_axon=axon,
                synapse=AttestSynapse(seed=seed, iters=iters, fill=True),
                timeout=timeout,
                deserialize=False,
            )
            return cast(AttestSynapse, result), (time.monotonic() - started) * 1000.0
        except Exception as e:  # network / axon fault: judged as no attestation
            bt.logging.debug(f'Serving: attest call failed: {e!r}')
            return None, (time.monotonic() - started) * 1000.0

    results = await asyncio.gather(*(one(axon) for _, _, axon in targets))
    return {hotkey: resp for (_, hotkey, _), resp in zip(targets, results)}


async def attest_round(
    state: ServingState,
    dendrite: bt.Dendrite,
    candidates: Sequence[Tuple[int, str, bt.AxonInfo]],
    release: ServingRelease,
    rng=None,
    timeout: float = SERVING_ATTEST_TIMEOUT,
) -> Dict[str, bool]:
    """Challenge this round's cohort, update ``state.attest_status``; return hotkey -> admitted for every candidate.

    Without an ``attest_reference_url`` (localnet / echo) attestation is off and every candidate is admitted.
    If the reference itself fails, or anything in here does, nothing changes this round (neutral).
    """
    round_no = state.attest_round = getattr(state, 'attest_round', 0) + 1
    if not release.attest_reference_url:
        return {hk: True for _, hk, _ in candidates}
    hotkeys = [hk for _, hk, _ in candidates]

    def carried() -> Dict[str, bool]:
        return {hk: status_passed(state.attest_status.get(hk), round_no) for hk in hotkeys}

    try:
        return await _attest_round(state, dendrite, candidates, release, round_no, rng, timeout)
    except Exception as e:  # a fault in judging must not take the round down; the cohort keeps its last verdict
        bt.logging.error(f'Serving: attestation failed this round, attest neutral: {e!r}')
        return carried()


async def _attest_round(
    state: ServingState,
    dendrite: bt.Dendrite,
    candidates: Sequence[Tuple[int, str, bt.AxonInfo]],
    release: ServingRelease,
    round_no: int,
    rng,
    timeout: float,
) -> Dict[str, bool]:
    hotkeys = [hk for _, hk, _ in candidates]
    cohort = set(choose_cohort(hotkeys, state.attest_status, rng=rng))
    seed = secrets.randbits(62)
    iters = release.attest_iters or SERVING_ATTEST_ITERS
    try:
        expected0, ref_wall = await asyncio.to_thread(reference_challenge, release, seed, iters, timeout)
    except Exception as e:
        bt.logging.error(f'Serving: reference attestation failed, attest neutral this round: {e!r}')
        return {hk: status_passed(state.attest_status.get(hk), round_no) for hk in hotkeys}
    targets = [(uid, hk, axon) for uid, hk, axon in candidates if hk in cohort]
    responses = await send_challenges(dendrite, targets, seed, iters, timeout)
    # One reference digest per device index any miner claimed (index i answers seed + i), at most MAX_CARDS.
    claimed = max((len(resp.devices) for resp, _ in responses.values() if resp is not None and resp.devices), default=1)
    digests: Dict[int, Optional[str]] = {0: expected0}
    for i in range(1, min(claimed, SERVING_ATTEST_MAX_CARDS)):
        try:
            digests[i] = (await asyncio.to_thread(reference_challenge, release, seed + i, iters, timeout))[0]
        except Exception as e:
            bt.logging.warning(f'Serving: reference attestation for device index {i} failed: {e!r}')
            digests[i] = None
    now = time.time()
    for uid, hk, _ in targets:
        resp, elapsed = responses.get(hk, (None, 0.0))
        verdict = judge(resp, lambda i: digests.get(i), ref_wall, release, elapsed_ms=elapsed)
        state.attest_status[hk] = verdict.as_status(now, round_no)
        bt.logging.info(
            f'Serving: UID {uid} attest {"PASS" if verdict.passed else "FAIL"} '
            f'{verdict.wall_ms or 0:.0f} ms (ref {ref_wall:.0f}, rtt {elapsed:.0f}) fill '
            f'{verdict.filled_bytes / 1e9:.1f} GB cards {len(verdict.uuids)}/{len(verdict.cards)} '
            f'uuid {verdict.uuid or "-"}{"" if verdict.reason.startswith("ok") else " — " + verdict.reason}'
        )
    return {hk: status_passed(state.attest_status.get(hk), round_no) for hk in hotkeys}
