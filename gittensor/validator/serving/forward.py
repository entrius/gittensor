# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving verification loop (sub-subnet B beta).

``ServingAuditThread`` runs ``audit_round`` on its own wall clock (every
``SERVING_AUDIT_INTERVAL_S``), independent of the validator's step loop, so
the gateway's READY set stays fresh while an OSS round takes hours.

Each round, for every blessed release, the validator sends audit prompts from
that release's reference (live runtime on its own GPU, or a bank snapshot) to
every serving axon over the same ``InferenceSynapse`` the gateway uses for
user traffic, verifies each response against the reference (exact tokens,
logprobs to float noise — the runtime is deterministic) and records the
outcome into the miner's rolling ``AuditWindow``, and produces a per-hotkey
serving score. Axons are audited concurrently; an axon that does not answer
the first prompt is not sent the rest (the misses still count). A miner
serves one release, so its score is the best it achieved across releases;
miners whose window passes are published as READY (tagged with their release)
to the gateway, and the scores are picked up by the next OSS round.

    score = window passes (0/1) x mean over this round's audits of latency_credit x capacity

Misses count as 0 in the window and latency credit 0 in the round. ``capacity``
comes from the round's load probe: every miner whose window passed gets
``SERVING_PROBE_REQUESTS`` prompts at the same instant as every other miner,
and verified tokens per wall-clock second over ``SERVING_PROBE_TARGET_TPS``
(capped at 1) is its capacity — so hotkeys sharing one GPU share one GPU's pay.
"""

import asyncio
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import bittensor as bt

from gittensor.constants import (
    SERVING_AUDIT_CONCURRENCY,
    SERVING_CHALLENGE_TIMEOUT,
    SERVING_CHALLENGES_PER_ROUND,
    SERVING_PROBE_REQUESTS,
    SERVING_PROBE_TARGET_TPS,
)
from gittensor.serving.audit import AuditCase, AuditVerdict, reference_for, verify_response
from gittensor.serving.loadout import ServingRelease, load_serving_loadout
from gittensor.serving.state import ReadyMiner, RequestRecord, ServingState
from gittensor.serving.stream import consume_stream
from gittensor.synapses import InferenceSynapse
from gittensor.validator.serving.scoring import latency_credit

if TYPE_CHECKING:
    from neurons.validator import Validator


async def audit_axon(
    dendrite: bt.Dendrite, uid: int, axon: bt.AxonInfo, release: ServingRelease, cases: Sequence[AuditCase]
) -> List[Tuple[AuditVerdict, float, RequestRecord]]:
    """Audit one axon with every case in order; stop after the first case that gets no response at all."""
    results: List[Tuple[AuditVerdict, float, RequestRecord]] = []
    for i, case in enumerate(cases):
        synapse = InferenceSynapse(
            messages=case.messages, model_id=release.model_id, max_tokens=case.max_tokens, logprobs=True
        )
        response = await consume_stream(dendrite, axon, synapse, SERVING_CHALLENGE_TIMEOUT)
        scored = score_response(uid, response, case, release)
        results.append(scored)
        if i == 0 and getattr(response, 'completion', None) is None:
            for later in cases[1:]:
                results.append(score_response(uid, synapse.model_copy(), later, release))
            break
    return results


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
        state.audits.record(hotkey, release.model_id, verdict.value)
        state.record(RequestRecord(**{**record.__dict__, 'kind': 'probe'}))
        if verdict.passed:
            tokens += n_tokens
    return tokens / wall_s


async def audit_round(
    state: ServingState,
    dendrite: bt.Dendrite,
    serving: Sequence[Tuple[int, str, bt.AxonInfo]],
    loadout=None,
) -> Dict[str, float]:
    """Audit every serving axon against every release; publish READY + scores; return hotkey -> score."""
    loadout = loadout or load_serving_loadout()
    if not serving:
        bt.logging.info('Serving: no serving axons found this round')
        state.publish_round([], {})
        return {}

    best: Dict[str, Tuple[float, str]] = {hotkey: (0.0, '') for _, hotkey, _ in serving}
    slots = asyncio.Semaphore(SERVING_AUDIT_CONCURRENCY)

    async def audit_one(uid: int, hotkey: str, axon: bt.AxonInfo, release: ServingRelease, cases) -> float:
        async with slots:
            results = await audit_axon(dendrite, uid, axon, release, cases)
        credit = 0.0
        for verdict, elapsed_ms, record in results:
            state.audits.record(hotkey, release.model_id, verdict.value)
            credit += latency_credit(elapsed_ms)
            state.record(record)
        return credit / len(cases)

    for release in loadout.releases:
        try:
            reference = reference_for(release)
            cases = [reference.sample() for _ in range(SERVING_CHALLENGES_PER_ROUND)]
        except Exception as e:  # reference down / bank missing: skip this release, keep auditing the others
            bt.logging.error(
                f'Serving: no reference for {release.model_id} this round ({e!r}); '
                'set SERVING_REFERENCE_URL to a conformant runtime or build its audit bank'
            )
            continue
        credits = await asyncio.gather(*(audit_one(uid, hotkey, axon, release, cases) for uid, hotkey, axon in serving))
        passing = [
            (uid, hotkey, axon, credit)
            for (uid, hotkey, axon), credit in zip(serving, credits)
            if credit > 0.0 and state.audits.verdict(hotkey, release.model_id).passed
        ]
        for (uid, hotkey, _), credit in zip(serving, credits):
            window = state.audits.verdict(hotkey, release.model_id)
            bt.logging.debug(f'Serving: UID {uid} {release.model_id} window {window.as_dict()} credit {credit:.3f}')
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
            score = credit * capacity if state.audits.verdict(hotkey, release.model_id).passed else 0.0
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
        bt.logging.info(
            f'Serving: UID {uid} score {score:.3f} over {SERVING_CHALLENGES_PER_ROUND} audits'
            + (f' ({model_id})' if model_id else '')
        )
        if score > 0.0:
            ready.append(ReadyMiner(uid=uid, hotkey=hotkey, axon=axon, score=score, model_id=model_id))
    state.publish_round(ready, scores)
    bt.logging.info(f'Serving: {len(ready)} READY miner(s) published to gateway: {[m.uid for m in ready]}')
    return scores


class ServingAuditThread:
    """Runs ``audit_round`` every ``interval_s`` seconds on a private event loop in a daemon thread."""

    def __init__(self, validator: 'Validator', state: ServingState, interval_s: float):
        self.validator = validator
        self.state = state
        self.interval_s = interval_s
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
        while not self._stop.is_set():
            started = time.monotonic()
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
            self._stop.wait(max(0.0, self.interval_s - (time.monotonic() - started)))


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
    """Measure one audit response: (verdict, elapsed ms, telemetry record).

    A missing response or a wrong model is a failed audit with infinite latency.
    """
    process_time = getattr(getattr(response, 'dendrite', None), 'process_time', None)
    elapsed_ms = float(process_time) * 1000.0 if process_time is not None else float('inf')

    def rec(ok: bool, detail: str) -> RequestRecord:
        return RequestRecord(
            ts=time.time(),
            kind='audit',
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
