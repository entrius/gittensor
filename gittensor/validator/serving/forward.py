# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving verification loop (sub-subnet B beta).

Each round, for every blessed release, the validator sends audit prompts from
that release's reference (live runtime on its own GPU, or a bank snapshot) to
every serving axon over the same ``InferenceSynapse`` the gateway uses for
user traffic, records each response's positional overlap with the reference
into the miner's rolling ``AuditWindow``, and produces a per-UID serving score.
A miner serves one release, so its score is the best it achieved across
releases; miners whose window passes are published as READY (tagged with
their release) to the gateway for the next round.

    score = window passes (0/1) x mean over this round's audits of latency_credit

Misses count as overlap 0 in the window and latency credit 0 in the round.
"""

from typing import TYPE_CHECKING, Dict, List, Tuple

import bittensor as bt

from gittensor.constants import (
    SERVING_CHALLENGE_TIMEOUT,
    SERVING_CHALLENGES_PER_ROUND,
)
from gittensor.serving.audit import AuditCase, AuditVerdict, reference_for, verify_response
from gittensor.serving.loadout import ServingRelease, load_serving_loadout
from gittensor.serving.state import ReadyMiner, RequestRecord, ServingState
from gittensor.synapses import InferenceSynapse
from gittensor.validator.serving.scoring import latency_credit

if TYPE_CHECKING:
    from neurons.validator import Validator


async def serving_challenges(self: 'Validator', miner_uids: set[int]) -> Dict[int, float]:
    """Audit each serving axon against every release and return UID -> serving score in [0, 1]."""
    loadout = load_serving_loadout()
    state: ServingState = getattr(self, 'serving_state', None) or ServingState()
    serving = get_serving_axons(self, miner_uids)
    if not serving:
        bt.logging.info('Serving: no serving axons found this round')
        state.publish_ready([])
        return {}

    uids = [uid for uid, _ in serving]
    hotkeys = {uid: self.metagraph.hotkeys[uid] for uid in uids}
    axons = [axon for _, axon in serving]
    best: Dict[int, Tuple[float, str]] = {uid: (0.0, '') for uid in uids}

    for release in loadout.releases:
        reference = reference_for(release)
        credit: Dict[int, float] = {uid: 0.0 for uid in uids}
        for _ in range(SERVING_CHALLENGES_PER_ROUND):
            case = reference.sample()
            synapse = InferenceSynapse(
                messages=case.messages, model_id=release.model_id, max_tokens=case.max_tokens, logprobs=True
            )
            responses = await self.dendrite(
                axons=axons, synapse=synapse, deserialize=False, timeout=SERVING_CHALLENGE_TIMEOUT
            )
            for uid, response in zip(uids, responses):
                verdict, elapsed_ms, record = score_response(uid, response, case, release)
                state.audits.record(hotkeys[uid], release.model_id, verdict.positional_overlap)
                credit[uid] += latency_credit(elapsed_ms)
                state.record(record)
        for uid in uids:
            window = state.audits.verdict(hotkeys[uid], release.model_id)
            score = (credit[uid] / SERVING_CHALLENGES_PER_ROUND) if window.passed else 0.0
            bt.logging.debug(f'Serving: UID {uid} {release.model_id} window {window.as_dict()} score {score:.3f}')
            if score > best[uid][0]:
                best[uid] = (score, release.model_id)

    scores = {uid: score for uid, (score, _) in best.items()}
    ready: List[ReadyMiner] = []
    for uid, axon in serving:
        score, model_id = best[uid]
        bt.logging.info(
            f'Serving: UID {uid} score {score:.3f} over {SERVING_CHALLENGES_PER_ROUND} audits'
            + (f' ({model_id})' if model_id else '')
        )
        if score > 0.0:
            ready.append(ReadyMiner(uid=uid, hotkey=hotkeys[uid], axon=axon, score=score, model_id=model_id))
    state.publish_ready(ready)
    bt.logging.info(f'Serving: {len(ready)} READY miner(s) published to gateway: {[m.uid for m in ready]}')
    return scores


def get_serving_axons(self: 'Validator', miner_uids: set[int]) -> List[Tuple[int, bt.AxonInfo]]:
    """UIDs (excluding self) whose axon is serving — the candidate serving miners.

    Beta heuristic: axon.is_serving is the only signal. Validators also serve
    axons (for PAT handling), so they will appear here and simply score zero on
    audits; a serving-miner registry replaces this later.
    """
    serving: List[Tuple[int, bt.AxonInfo]] = []
    for uid in sorted(miner_uids):
        if uid == self.uid:
            continue
        axon = self.metagraph.axons[uid]
        if axon is not None and axon.is_serving:
            serving.append((uid, axon))
    return serving


def score_response(
    uid: int, response: InferenceSynapse, case: AuditCase, release: ServingRelease
) -> Tuple[AuditVerdict, float, RequestRecord]:
    """Measure one audit response: (verdict with positional_overlap, elapsed ms, telemetry record).

    A missing response or a wrong model counts as overlap 0 and infinite latency.
    """
    import time

    process_time = getattr(getattr(response, 'dendrite', None), 'process_time', None)
    elapsed_ms = float(process_time) * 1000.0 if process_time is not None else float('inf')

    def rec(ok: bool, detail: str) -> RequestRecord:
        return RequestRecord(
            ts=time.time(),
            kind='audit',
            uid=uid,
            ok=ok,
            latency_ms=elapsed_ms,
            completion_tokens=len(getattr(response, 'tokens', None) or []),
            ttft_ms=getattr(response, 'ttft_ms', None),
            decode_tps=getattr(response, 'decode_tps', None),
            detail=detail,
        )

    if getattr(response, 'completion', None) is None:
        return AuditVerdict(False, 0.0, float('inf'), 'no response'), float('inf'), rec(False, 'no response')
    served = getattr(response, 'served_model_id', None)
    if served != release.model_id:
        reason = f'wrong model {served!r}'
        return AuditVerdict(False, 0.0, float('inf'), reason), float('inf'), rec(False, reason)

    verdict = verify_response(case, getattr(response, 'tokens', None), getattr(response, 'token_logprobs', None))
    return verdict, elapsed_ms, rec(verdict.passed, f'{verdict.reason} overlap={verdict.positional_overlap:.2f}')
