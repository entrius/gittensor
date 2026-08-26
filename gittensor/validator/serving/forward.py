# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving verification loop (sub-subnet B beta).

``ServingAuditThread`` runs ``audit_round`` on its own wall clock (every
``SERVING_AUDIT_INTERVAL_S``), independent of the validator's step loop, so
the gateway's READY set stays fresh while an OSS round takes hours.

There are no audit prompts. Every request the gateway served since the last
round is verified against the release's reference by teacher forcing the
miner's completion (``verify_served``): tokens must match the reference's
argmax and logprobs must agree to float noise. Each verdict enters the miner's
rolling ``AuditWindow``: misses/timeouts as 0, a wrong answer as a strike that
wipes the window and quarantines the hotkey. Miners whose window passes are
published READY; serving axons that are not READY (and not quarantined) are
published as *probation* so baseline traffic can give them a window.

    round score = window passes (0/1) x mean latency credit over this round's served requests x capacity

``capacity`` comes from the round's load probe: every READY miner gets
``SERVING_PROBE_REQUESTS`` reference prompts at the same instant as every other
miner, and verified tokens per wall-clock second over ``SERVING_PROBE_TARGET_TPS``
(capped at 1) is its capacity — so hotkeys sharing one GPU share one GPU's pay.
Probe outcomes affect capacity only, never the window. Round scores are settled
over the trailing ``SERVING_SETTLEMENT_ROUNDS`` rounds by ``ServingState``.
"""

import asyncio
import hashlib
import math
import random
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import aiohttp
import bittensor as bt

from gittensor.constants import (
    SERVING_BASELINE_PER_ROUND,
    SERVING_CHALLENGE_TIMEOUT,
    SERVING_MAX_TOKENS,
    SERVING_PROBE_REQUESTS,
    SERVING_PROBE_TARGET_TPS,
)
from gittensor.serving.audit import AuditCase, AuditVerdict, Reference, reference_for, verify_response, verify_served
from gittensor.serving.baseline import baseline_max_tokens, make_baseline_prompt
from gittensor.serving.loadout import ServingRelease, load_serving_loadout
from gittensor.serving.state import ReadyMiner, RequestRecord, ServedRequest, ServingState
from gittensor.serving.stream import consume_stream
from gittensor.synapses import InferenceSynapse
from gittensor.validator.serving.scoring import latency_credit

if TYPE_CHECKING:
    from neurons.validator import Validator


async def probe_axon(
    state: ServingState,
    dendrite: bt.Dendrite,
    uid: int,
    hotkey: str,
    axon: bt.AxonInfo,
    release: ServingRelease,
    cases: Sequence[AuditCase],
) -> float:
    """Fire all ``cases`` at once; return verified tokens per wall-clock second (0 when nothing verified)."""

    async def one(case: AuditCase) -> Tuple[AuditVerdict, float, RequestRecord, int]:
        synapse = InferenceSynapse(
            messages=case.messages, model_id=release.model_id, max_tokens=case.max_tokens, logprobs=True
        )
        response = await consume_stream(dendrite, axon, synapse, SERVING_CHALLENGE_TIMEOUT)
        verdict, elapsed_ms, record = score_response(uid, response, case, release)
        return verdict, elapsed_ms, record, len(getattr(response, 'tokens', None) or [])

    started = time.monotonic()
    results = await asyncio.gather(*(one(case) for case in cases))
    wall_s = max(time.monotonic() - started, 1e-3)
    tokens = 0
    for verdict, _, record, n_tokens in results:
        state.record(record)
        if verdict.passed:
            tokens += n_tokens
    return tokens / wall_s


def verify_served_round(
    state: ServingState, reference: Reference, release: ServingRelease, served: Sequence[ServedRequest]
) -> Dict[str, List[float]]:
    """Verify every served request for ``release`` into the window; return latency credits per hotkey."""
    credits: Dict[str, List[float]] = {}
    for req in served:
        if req.model_id != release.model_id:
            continue
        if not req.ok and 'budget' in req.detail.lower():  # this validator over-sent; not the miner's fault
            continue
        if not req.ok:
            verdict = AuditVerdict(False, 0.0, float('inf'), req.detail or 'no completion')
        else:
            try:
                verdict = verify_served(reference, req.messages, req.completion, req.tokens, req.token_logprobs)
            except Exception as e:  # reference hiccup: neither credit nor blame
                bt.logging.warning(f'Serving: could not verify a request served by UID {req.uid}: {e!r}')
                continue
        if verdict.hard:
            until = state.audits.strike(req.hotkey, release.model_id)
            bt.logging.warning(
                f'Serving: UID {req.uid} served a WRONG answer ({verdict.reason}); window wiped, '
                f'quarantined until {time.strftime("%H:%M:%S", time.gmtime(until))} UTC'
            )
        else:
            state.audits.record(req.hotkey, release.model_id, verdict.value)
        credits.setdefault(req.hotkey, []).append(
            latency_credit(req.latency_ms) if verdict.passed and req.latency_ms is not None else 0.0
        )
        state.record(
            RequestRecord(
                ts=time.time(),
                kind='verify',
                uid=req.uid,
                ok=verdict.passed,
                latency_ms=req.latency_ms,
                completion_tokens=len(req.tokens or []),
                detail=verdict.reason,
            )
        )
    return credits


async def baseline_round(
    state: ServingState,
    dendrite: bt.Dendrite,
    serving: Sequence[Tuple[int, str, bt.AxonInfo]],
    release: ServingRelease,
    window_s: float,
    per_miner: int = SERVING_BASELINE_PER_ROUND,
    rng: Optional[random.Random] = None,
) -> int:
    """Send every serving axon ``per_miner`` baseline prompts at random moments within ``window_s``.

    The requests take the same path as user traffic and are queued as served requests, so the next round verifies
    them like anything else. Quarantined hotkeys are skipped. Returns the number of requests sent.
    """
    rng = rng or random.Random()
    targets = [
        (uid, hotkey, axon)
        for uid, hotkey, axon in serving
        if state.audits.quarantined_until(hotkey, release.model_id) == 0.0
    ]

    async def one(uid: int, hotkey: str, axon: bt.AxonInfo, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        messages = make_baseline_prompt(rng)
        max_tokens = baseline_max_tokens(rng, min(SERVING_MAX_TOKENS, max(release.max_tokens, 512)))
        synapse = InferenceSynapse(messages=messages, model_id=release.model_id, max_tokens=max_tokens, logprobs=True)
        started = time.monotonic()
        try:
            response = await consume_stream(dendrite, axon, synapse, release.request_timeout)
        except Exception as e:
            response, err = None, repr(e)
        else:
            err = ''
        ok = response is not None and response.completion is not None and response.served_model_id == release.model_id
        status = getattr(getattr(response, 'dendrite', None), 'status_message', None) if response is not None else None
        state.enqueue_served(
            ServedRequest(
                ts=time.time(),
                uid=uid,
                hotkey=hotkey,
                model_id=release.model_id,
                messages=messages,
                ok=ok,
                latency_ms=(time.monotonic() - started) * 1000.0 if ok else None,
                completion=response.completion if response is not None else None,
                tokens=list(response.tokens) if response is not None and response.tokens else None,
                token_logprobs=list(response.token_logprobs)
                if response is not None and response.token_logprobs
                else None,
                detail='' if ok else str(status or err or 'no response'),
            )
        )

    jobs = [
        one(uid, hotkey, axon, rng.uniform(0.0, max(0.0, window_s)))
        for uid, hotkey, axon in targets
        for _ in range(per_miner)
    ]
    await asyncio.gather(*jobs)
    return len(jobs)


async def audit_round(
    state: ServingState,
    dendrite: bt.Dendrite,
    serving: Sequence[Tuple[int, str, bt.AxonInfo]],
    loadout=None,
) -> Dict[str, float]:
    """Verify served traffic, settle windows, probe READY miners; publish READY/probation; return hotkey -> score."""
    loadout = loadout or load_serving_loadout()
    served = state.drain_served()
    if not serving:
        bt.logging.info('Serving: no serving axons found this round')
        state.publish_round([], {})
        return {}

    best: Dict[str, Tuple[float, str]] = {hotkey: (0.0, '') for _, hotkey, _ in serving}
    probation: Dict[int, ReadyMiner] = {}

    for release in loadout.releases:
        try:
            reference = reference_for(release)
        except Exception as e:  # reference down / bank missing: skip this release, keep the others
            bt.logging.error(
                f'Serving: no reference for {release.model_id} this round ({e!r}); '
                'set SERVING_REFERENCE_URL to a conformant runtime'
            )
            continue
        credits = verify_served_round(state, reference, release, served)
        passing: List[Tuple[int, str, bt.AxonInfo, float]] = []
        for uid, hotkey, axon in serving:
            window = state.audits.verdict(hotkey, release.model_id)
            round_credits = credits.get(hotkey)
            credit = sum(round_credits) / len(round_credits) if round_credits else 1.0
            bt.logging.debug(
                f'Serving: UID {uid} {release.model_id} window {window.as_dict()} '
                f'served {len(round_credits or [])} credit {credit:.3f}'
            )
            if window.passed and credit > 0.0:
                passing.append((uid, hotkey, axon, credit))
            elif window.quarantined_until == 0.0 and uid not in probation:
                probation[uid] = ReadyMiner(uid=uid, hotkey=hotkey, axon=axon, score=0.0, model_id=release.model_id)
        if not passing:
            continue
        probe_cases = [reference.sample() for _ in range(SERVING_PROBE_REQUESTS)]
        if probe_cases:
            rates = await asyncio.gather(
                *(
                    probe_axon(state, dendrite, uid, hotkey, axon, release, probe_cases)
                    for uid, hotkey, axon, _ in passing
                )
            )
        else:  # probe disabled: capacity is not measured
            rates = [SERVING_PROBE_TARGET_TPS] * len(passing)
        for (uid, hotkey, _, credit), tps in zip(passing, rates):
            capacity = min(1.0, tps / SERVING_PROBE_TARGET_TPS)
            score = credit * capacity
            bt.logging.info(
                f'Serving: UID {uid} {release.model_id} probe {tps:.0f} tok/s capacity {capacity:.2f} '
                f'latency credit {credit:.2f} score {score:.3f}'
            )
            if score > best[hotkey][0]:
                best[hotkey] = (score, release.model_id)

    scores = {hotkey: score for hotkey, (score, _) in best.items()}
    ready: List[ReadyMiner] = []
    for uid, hotkey, axon in serving:
        score, model_id = best[hotkey]
        if score > 0.0:
            ready.append(ReadyMiner(uid=uid, hotkey=hotkey, axon=axon, score=score, model_id=model_id))
            probation.pop(uid, None)
    state.publish_round(ready, scores, list(probation.values()))
    bt.logging.info(
        f'Serving: verified {len(served)} served request(s); {len(ready)} READY miner(s) published to gateway: '
        f'{[m.uid for m in ready]}; {len(probation)} on probation'
    )
    return scores


async def _unlimited_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0))


class ServingAuditThread:
    """Runs ``audit_round`` every ``interval_s`` seconds on a private event loop in a daemon thread."""

    def __init__(
        self,
        validator: 'Validator',
        state: ServingState,
        interval_s: float,
        baseline_per_round: int = SERVING_BASELINE_PER_ROUND,
    ):
        self.validator = validator
        self.state = state
        self.interval_s = interval_s
        self.baseline_per_round = baseline_per_round
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='serving-audits', daemon=True)

    def start(self) -> None:
        self.thread.start()
        bt.logging.success(f'Serving audit loop started (every {self.interval_s:.0f}s)')

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        dendrite = bt.Dendrite(wallet=self.validator.wallet)
        # The probe streams from every READY miner at once (fleet x SERVING_PROBE_REQUESTS); aiohttp's default
        # connector caps a session at 100 connections, which would queue late miners on the validator and skew
        # their measured throughput. Uncapped connector for the audit dendrite only.
        dendrite._session = loop.run_until_complete(_unlimited_session())
        # Validators probe on the same interval; a per-hotkey phase offset keeps their bursts from landing on a
        # miner at the same instant and each reading half a card.
        self._stop.wait(probe_phase_offset(self.validator.wallet.hotkey.ss58_address, self.interval_s))
        while not self._stop.is_set():
            started = time.monotonic()
            serving: List[Tuple[int, str, bt.AxonInfo]] = []
            try:
                serving = get_serving_axons(self.validator)
                loop.run_until_complete(audit_round(self.state, dendrite, serving))
            except Exception as e:  # a serving fault must never take the validator down
                bt.logging.error(f'Serving round failed, no serving scores this round: {e!r}')
                self.state.publish_round([], {})
            path = audit_window_path(self.validator)
            if path is not None:
                try:
                    self.state.audits.save(path)
                except OSError as e:
                    bt.logging.warning(f'Serving: could not persist audit window to {path}: {e}')
            # The rest of the interval carries this validator's own baseline prompts, spread at random so nothing
            # marks the round boundary; they are verified next round alongside any user traffic.
            remaining = max(0.0, self.interval_s - (time.monotonic() - started))
            try:
                release = load_serving_loadout().primary
                sent = loop.run_until_complete(
                    baseline_round(
                        self.state, dendrite, serving, release, max(0.0, remaining - 5.0), self.baseline_per_round
                    )
                )
                bt.logging.debug(f'Serving: sent {sent} baseline request(s)')
            except Exception as e:
                bt.logging.error(f'Serving: baseline traffic failed this round: {e!r}')
            self._stop.wait(max(0.0, self.interval_s - (time.monotonic() - started)))


def probe_phase_offset(hotkey: str, interval_s: float) -> float:
    """Deterministic start offset in [0, interval) for this validator's audit clock."""
    return (int(hashlib.sha256(hotkey.encode()).hexdigest()[:8], 16) % 1_000_000) / 1_000_000 * interval_s


def audit_window_path(self: 'Validator') -> Optional[Path]:
    """Where the rolling audit window is persisted: next to state.npz, or None when the neuron has no state dir."""
    full_path = getattr(getattr(getattr(self, 'config', None), 'neuron', None), 'full_path', None)
    return Path(full_path) / 'serving_audits.json' if full_path else None


def get_serving_axons(self: 'Validator') -> List[Tuple[int, str, bt.AxonInfo]]:
    """(uid, hotkey, axon) for every UID (excluding self) whose axon is serving — the candidate serving miners.

    Snapshot of the metagraph taken on the audit thread. Beta heuristic: axon.is_serving is the only signal.
    Validators also serve axons (for PAT handling), so they appear here and score zero on the first prompt;
    a serving-miner registry replaces this later.
    """
    hotkeys = list(self.metagraph.hotkeys)
    axons = list(self.metagraph.axons)
    serving: List[Tuple[int, str, bt.AxonInfo]] = []
    for uid, (hotkey, axon) in enumerate(zip(hotkeys, axons)):
        if uid == self.uid:
            continue
        if axon is not None and axon.is_serving:
            serving.append((uid, hotkey, axon))
    return serving


def score_response(
    uid: int, response: InferenceSynapse, case: AuditCase, release: ServingRelease
) -> Tuple[AuditVerdict, float, RequestRecord]:
    """Measure one probe response: (verdict, elapsed ms, telemetry record).

    A missing response or a wrong model is a failed probe request with infinite latency.
    """
    process_time = getattr(getattr(response, 'dendrite', None), 'process_time', None)
    elapsed_ms = float(process_time) * 1000.0 if process_time is not None else float('inf')

    def rec(ok: bool, detail: str) -> RequestRecord:
        return RequestRecord(
            ts=time.time(),
            kind='probe',
            uid=uid,
            ok=ok,
            latency_ms=elapsed_ms if math.isfinite(elapsed_ms) else None,
            completion_tokens=len(getattr(response, 'tokens', None) or []),
            ttft_ms=getattr(response, 'ttft_ms', None),
            decode_tps=getattr(response, 'decode_tps', None),
            detail=detail,
        )

    if getattr(response, 'completion', None) is None:
        dendrite = getattr(response, 'dendrite', None)
        reason = f'no response ({getattr(dendrite, "status_code", None)} {getattr(dendrite, "status_message", None)})'
        return AuditVerdict(False, 0.0, float('inf'), reason), float('inf'), rec(False, reason)
    served = getattr(response, 'served_model_id', None)
    if served != release.model_id:
        reason = f'wrong model {served!r}'
        return AuditVerdict(False, 0.0, float('inf'), reason), float('inf'), rec(False, reason)

    verdict = verify_response(case, getattr(response, 'tokens', None), getattr(response, 'token_logprobs', None))
    return verdict, elapsed_ms, rec(verdict.passed, verdict.reason)
