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

    admitted    = window passes x speed credit > 0 x hardware attestation passes
    round score = admitted x output tokens the gateway saw the miner serve, in card-equivalents

Pay is per token served, counted from the gateway's own served-request records
(never the miner's word) and priced against the release's aggregate decode speed
(``scoring.card_equivalents``): a card flat out for the hour earns one card-hour,
an idle one nothing, and no card count or per-hotkey cap enters — the only
ceiling is the emission pool. Speed credit is measured on served traffic only
(TTFT and decode rate against the blessing's curve at the load this validator
imposed; ``scoring.py``) and is the READY miner's routing weight, so a slow card
is sent less and serves fewer paid tokens; a round with nothing verified freezes
at the last measured credit. Attestation (``attest.py``) is pass/fail admission:
the least recently challenged half of the READY miners is challenged every round;
a miner with no verdict yet stays on probation. Round scores are settled over the
trailing ``SERVING_SETTLEMENT_ROUNDS`` rounds by ``ServingState``.
"""

import asyncio
import hashlib
import math
import os
import random
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import aiohttp
import bittensor as bt
import requests

from gittensor.classes import RequestSpeed
from gittensor.constants import (
    BLOCKS_PER_TEMPO,
    SERVING_AUDIT_SAMPLE_FRACTION,
    SERVING_AUDIT_SAMPLE_MIN,
    SERVING_BASELINE_FIRST_BYTE_S,
    SERVING_BASELINE_PER_ROUND,
    SERVING_BUDGET_REFUSAL_RATIO,
    SERVING_DORMANT_AFTER_ROUNDS,
    SERVING_DORMANT_RETRY_ROUNDS,
    SERVING_MAX_TOKENS,
    SERVING_MIN_CALLER_STAKE,
    SERVING_STRIKE_MIN_FLEET_PASS,
    SERVING_VALIDATOR_TOKENS_PER_TEMPO,
    SERVING_VERIFY_WORKERS,
)
from gittensor.serving.audit import AuditVerdict, Reference, reference_for, verify_served
from gittensor.serving.baseline import baseline_max_tokens, make_baseline_prompt
from gittensor.serving.loadout import ServingRelease, load_serving_loadout
from gittensor.serving.state import ReadyMiner, RequestRecord, ServedRequest, ServingState, is_busy_detail
from gittensor.serving.store import ServingStore
from gittensor.serving.stream import consume_stream
from gittensor.synapses import InferenceSynapse
from gittensor.validator.serving.attest import attest_round, status_passed
from gittensor.validator.serving.persist import ServingRoundStorage
from gittensor.validator.serving.scoring import card_equivalents, paid_tokens, request_speed, token_rate_usd
from gittensor.validator.utils.config import (
    SERVING_AUDIT_INTERVAL_S,
    SERVING_PAY_CAP_WITHOUT_PRICING,
    STORE_DB_RESULTS,
)

if TYPE_CHECKING:
    from neurons.validator import Validator


def sample_for_audit(
    served: Sequence[ServedRequest],
    fraction: float = SERVING_AUDIT_SAMPLE_FRACTION,
    minimum: int = SERVING_AUDIT_SAMPLE_MIN,
    rng=None,
) -> Tuple[List[ServedRequest], List[ServedRequest]]:
    """(to verify, skipped): per hotkey every baseline prompt and every failed request, plus a random
    max(minimum, fraction x n) of the completed gateway requests. Order of the input is kept."""
    rng = rng or secrets.SystemRandom()
    by_hotkey: Dict[str, List[int]] = {}
    for i, req in enumerate(served):
        if req.ok and req.source == 'gateway':
            by_hotkey.setdefault(req.hotkey, []).append(i)
    keep = set(range(len(served)))
    for idxs in by_hotkey.values():
        n = len(idxs)
        k = max(minimum, math.ceil(fraction * n))
        if k < n:
            keep.difference_update(set(idxs) - set(rng.sample(idxs, k)))
    return [r for i, r in enumerate(served) if i in keep], [r for i, r in enumerate(served) if i not in keep]


def reference_agrees_with_fleet(
    served: Sequence[ServedRequest],
    verdicts: Sequence[Optional[AuditVerdict]],
    min_fleet_pass: float = SERVING_STRIKE_MIN_FLEET_PASS,
) -> bool:
    """Did at least ``min_fleet_pass`` of the hotkeys judged this round pass something? With two or more hotkeys
    judged and most of them failing the bands, the reference — not the fleet — is what drifted."""
    passed_by: Dict[str, bool] = {}
    for req, verdict in zip(served, verdicts):
        if verdict is None:
            continue
        passed_by[req.hotkey] = passed_by.get(req.hotkey, False) or verdict.passed
    if len(passed_by) < 2 or not any(v.hard for v in verdicts if v is not None):
        return True
    return sum(passed_by.values()) / len(passed_by) >= min_fleet_pass


def reference_rejects(reference: Reference, messages: List[Dict[str, str]]) -> bool:
    """Would the validator's own reference refuse this prompt (HTTP 4xx on a one-token greedy)? Only a live
    reference can be asked; anything else, or any other failure, is 'no', so the miss stands."""
    case_for = getattr(reference, 'case_for', None)
    if case_for is None:
        return False
    try:
        case_for(messages, 1)
    except requests.HTTPError as e:
        status = getattr(getattr(e, 'response', None), 'status_code', 0) or 0
        return 400 <= status < 500
    except Exception:
        return False
    return False


def budget_refusal_plausible(
    state: ServingState, hotkey: str, staked_caller: bool, now: Optional[float] = None
) -> bool:
    """Could ``hotkey`` honestly have refused this validator for budget? Only a permitted, unstaked caller has a
    budget at all, and only once this validator's own ledger of ``max_tokens`` sent in the trailing tempo is near
    the miner's per-tempo allowance. The refusal text is the miner's; the ledger is ours."""
    if staked_caller:
        return False
    sent = state.sent_tokens(hotkey, BLOCKS_PER_TEMPO * 12.0, now)
    return sent >= SERVING_BUDGET_REFUSAL_RATIO * SERVING_VALIDATOR_TOKENS_PER_TEMPO


def verify_served_round(
    state: ServingState,
    reference: Reference,
    release: ServingRelease,
    served: Sequence[ServedRequest],
    summary: Optional[Dict[str, int]] = None,
    last_miss: Optional[Dict[str, str]] = None,
    rng=None,
    staked_caller: bool = False,
) -> Tuple[Dict[str, List[RequestSpeed]], Dict[str, int]]:
    """Verify this round's audit sample of the requests served for ``release`` into the window; return per-request
    speed per hotkey and the output tokens each hotkey is paid for.

    Paid tokens are what the gateway saw served on user traffic: every unsampled completion, and every sampled one
    the reference did not fail. Baseline prompts anchor eligibility, not income. Reference calls run on a small
    thread pool; window updates stay on this thread. ``last_miss`` (hotkey -> reason) is filled with the most
    recent miss or strike reason so the round report can show a miner why. ``staked_caller``: this validator clears
    the miner's stake floor, so no miner has a budget to refuse it on.
    """
    summary = summary if summary is not None else {}
    last_miss = last_miss if last_miss is not None else {}
    tokens: Dict[str, int] = {}

    def pay(req: ServedRequest) -> None:
        if req.source == 'gateway' and req.ok:
            tokens[req.hotkey] = tokens.get(req.hotkey, 0) + paid_tokens(req)

    mine, skipped = sample_for_audit([req for req in served if req.release_id == release.release_id], rng=rng)
    for req in skipped:
        summary['served'] = summary.get('served', 0) + 1
        summary[req.source] = summary.get(req.source, 0) + 1
        summary['unsampled'] = summary.get('unsampled', 0) + 1
        pay(req)

    def judge(req: ServedRequest) -> Optional[AuditVerdict]:
        if not req.ok and 'budget' in req.detail.lower() and budget_refusal_plausible(state, req.hotkey, staked_caller):
            return None  # this validator over-sent by its own ledger; not the miner's fault
        if not req.ok and is_busy_detail(req.detail):
            # Refused at capacity: neutral, unconditionally — no ledger can corroborate load other validators put
            # on the card, and the refusal already cost the miner the tokens it did not serve. Failures are always
            # sampled, so tallying here counts every refusal exactly once (headroom telemetry, /v1/serving/status).
            state.busy_refusal(req.hotkey, now=req.ts)
            return None
        if not req.ok:
            if req.source == 'gateway' and reference_rejects(reference, req.messages):
                return None  # an honest runtime refuses this prompt too (over context, bad shape): not the miner's
            return AuditVerdict(False, 0.0, float('inf'), req.detail or 'no completion')
        for attempt in range(2):  # a connection blip to the reference is retried once before going neutral
            try:
                return verify_served(
                    reference,
                    req.messages,
                    req.completion,
                    req.tokens,
                    req.token_logprobs,
                    token_ids=req.token_ids,
                    token_bytes=req.token_bytes,
                    release=release,
                    max_tokens=req.max_tokens or None,
                )
            except requests.HTTPError as e:
                status = getattr(getattr(e, 'response', None), 'status_code', 0) or 0
                if 400 <= status < 500:  # the reference refused what the miner sent: the miner's answer, not a blip
                    return AuditVerdict(False, 0.0, float('inf'), f'reference rejected the completion (HTTP {status})')
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                bt.logging.warning(f'Serving: could not verify a request served by UID {req.uid}: {e!r}')
                return None
            except Exception as e:  # reference hiccup: neither credit nor blame
                if attempt == 0 and isinstance(e, (ConnectionError, OSError)) or 'Connect' in type(e).__name__:
                    time.sleep(1.0)
                    continue
                bt.logging.warning(f'Serving: could not verify a request served by UID {req.uid}: {e!r}')
                return None
        return None

    with ThreadPoolExecutor(max_workers=SERVING_VERIFY_WORKERS) as pool:
        verdicts = list(pool.map(judge, mine))

    def bump(key: str) -> None:
        summary[key] = summary.get(key, 0) + 1

    if not reference_agrees_with_fleet(mine, verdicts):
        # The reference is the odd one out this round (its own drift, not a fleet of wrong answers): band failures
        # are misses, not strikes.
        bump('reference_disagreement')
        verdicts = [
            AuditVerdict(False, v.prefix_agreement, v.mean_abs_logprob_diff, f'reference disagreement: {v.reason}')
            if v is not None and v.hard
            else v
            for v in verdicts
        ]

    ready_uids = {m.uid for m in state.ready_miners()}
    speeds: Dict[str, List[RequestSpeed]] = {}
    for req, verdict in zip(mine, verdicts):
        bump('served')
        bump(req.source)
        if verdict is None:
            bump('neutral')
            pay(req)
            continue
        if verdict.passed:
            pay(req)
        if not verdict.passed and not verdict.hard and req.uid in ready_uids:
            bt.logging.info(
                f'Serving: READY UID {req.uid} missed a {req.source} request ({verdict.reason}; '
                f'{len(req.tokens or [])} tokens, {req.latency_ms or 0:.0f} ms)'
            )
        if not verdict.passed:
            last_miss[req.hotkey] = verdict.reason
        if verdict.hard:
            until = state.audits.strike(req.hotkey, release.release_id)
            bump('strike')
            bt.logging.warning(
                f'Serving: UID {req.uid} served a WRONG answer ({verdict.reason}); window wiped, '
                f'quarantined until {time.strftime("%H:%M:%S", time.gmtime(until))} UTC'
            )
        else:
            state.audits.record(req.hotkey, release.release_id, verdict.value)
            bump('pass' if verdict.passed else 'miss')
        # Speed is priced on served traffic: time to first token and decode rate against the blessing's curve at
        # the load this validator had in flight to the miner (gittensor/validator/serving/scoring.py).
        speeds.setdefault(req.hotkey, []).append(
            request_speed(req, release) if verdict.passed else RequestSpeed(credit=0.0)
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
    return speeds, tokens


def _mean(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


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
    them like anything else. Quarantined and dormant hotkeys are skipped. Returns the number of requests sent.
    """
    rng = rng or random.Random()
    targets = [
        (uid, hotkey, axon)
        for uid, hotkey, axon in serving
        if state.audits.quarantined_until(hotkey, release.release_id) == 0.0 and not skip_baseline(state, hotkey)
    ]

    async def one(uid: int, hotkey: str, axon: bt.AxonInfo, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        messages = make_baseline_prompt(rng)
        max_tokens = baseline_max_tokens(rng, min(SERVING_MAX_TOKENS, max(release.max_tokens, 512)))
        synapse = InferenceSynapse(
            messages=messages,
            model_id=release.model_id,
            release_id=release.release_id,
            max_tokens=max_tokens,
            logprobs=True,
        )
        inflight = state.inflight().get(uid, 0) + 1
        state.charge_sent(hotkey, max_tokens)
        started = time.monotonic()
        try:
            response = await consume_stream(
                dendrite, axon, synapse, release.request_timeout, first_byte_s=SERVING_BASELINE_FIRST_BYTE_S
            )
        except Exception as e:
            response, err = None, repr(e)
        else:
            err = ''
        ok = response is not None and response.completion is not None and response.served_model_id == release.model_id
        status = getattr(getattr(response, 'dendrite', None), 'status_message', None) if response is not None else None
        if (
            response is not None and not ok
        ):  # the axon answered but served nothing: not a compute miner (or a broken one)
            served_as = getattr(response, 'served_model_id', None) or 'nothing'
            status = f'no completion: axon answered "{status or "OK"}" serving {served_as}, not {release.model_id}'
        state.enqueue_served(
            ServedRequest(
                ts=time.time(),
                uid=uid,
                hotkey=hotkey,
                model_id=release.model_id,
                release_id=release.release_id,
                messages=messages,
                ok=ok,
                latency_ms=(getattr(response, 'observed_latency_ms', None) or (time.monotonic() - started) * 1000.0)
                if ok
                else None,
                completion=response.completion if response is not None else None,
                tokens=list(response.tokens) if response is not None and response.tokens else None,
                token_ids=list(response.token_ids) if response is not None and response.token_ids else None,
                token_bytes=list(response.token_bytes) if response is not None and response.token_bytes else None,
                token_logprobs=list(response.token_logprobs)
                if response is not None and response.token_logprobs
                else None,
                detail='' if ok else str(status or err or 'no response'),
                source='baseline',
                ttft_ms=getattr(response, 'observed_ttft_ms', None) if ok else None,
                inflight=inflight,
                max_tokens=max_tokens,
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
    attest_rng=None,
    staked_caller: bool = False,
    round_s: float = SERVING_AUDIT_INTERVAL_S,
) -> Dict[str, float]:
    """Verify served traffic, settle windows, attest READY miners; publish READY/probation; return hotkey -> pay
    score (served tokens in card-equivalents over ``round_s``, the wall clock the tokens were served in)."""
    loadout = loadout or load_serving_loadout()
    served = state.drain_served()
    if not serving:
        bt.logging.info('Serving: no serving axons found this round')
        state.publish_round([], {})
        return {}

    update_dormancy(state, serving, served)
    active = [(uid, hotkey, axon) for uid, hotkey, axon in serving if not is_dormant(state, hotkey)]
    dormant = len(serving) - len(active)

    # Per admitted hotkey, the release it is READY for: (pay, routing weight, release_id), best pay first.
    admitted: Dict[str, Tuple[float, float, str]] = {}
    axon_of = {uid: axon for uid, _, axon in serving}
    probation: Dict[int, ReadyMiner] = {}
    summary: Dict[str, int] = {}
    windows: Dict[int, dict] = {}  # per-UID round report; published in state.last_round and persisted to the DB
    last_miss: Dict[str, str] = {}

    for release in loadout.releases:
        try:
            reference = reference_for(release)
        except Exception as e:  # reference down / bank missing: skip this release, keep the others
            bt.logging.error(
                f'Serving: no reference for {release.release_id} this round ({e!r}); '
                'set SERVING_REFERENCE_URL to a conformant runtime'
            )
            continue
        speeds, tokens = verify_served_round(
            state, reference, release, served, summary, last_miss, staked_caller=staked_caller
        )
        passing: List[Tuple[int, str, bt.AxonInfo, float]] = []
        for uid, hotkey, axon in active:
            window = state.audits.verdict(hotkey, release.release_id)
            round_speeds = speeds.get(hotkey) or []
            if round_speeds:
                credit = sum(sp.credit for sp in round_speeds) / len(round_speeds)
                state.last_credit[hotkey] = credit
            else:  # nothing verified this round: freeze at the last measured credit, do not assume a perfect one
                credit = state.last_credit.get(hotkey, 1.0)
            windows[uid] = {
                **window.as_dict(),
                'hotkey': hotkey,
                'model_id': release.model_id,
                'release_id': release.release_id,
                'served': len(round_speeds),
                'credit': round(credit, 4),
                'ttft_ms': _mean([sp.ttft_ms for sp in round_speeds if sp.ttft_ms is not None]),
                'decode_tps': _mean([sp.decode_tps for sp in round_speeds if sp.decode_tps is not None]),
                'tokens': tokens.get(hotkey, 0),
                'attested': False,
                'score': 0.0,
                'last_miss': last_miss.get(hotkey, ''),
            }
            bt.logging.debug(
                f'Serving: UID {uid} {release.release_id} window {window.as_dict()} '
                f'served {len(round_speeds)} credit {credit:.3f} tokens {tokens.get(hotkey, 0)}'
            )
            if window.passed and credit > 0.0:
                passing.append((uid, hotkey, axon, credit))
            elif window.quarantined_until == 0.0 and uid not in probation:
                probation[uid] = ReadyMiner(uid=uid, hotkey=hotkey, axon=axon, score=0.0, release_id=release.release_id)
        if not passing:
            continue
        attested = await attest_round(
            state, dendrite, [(uid, hotkey, axon) for uid, hotkey, axon, _ in passing], release, rng=attest_rng
        )
        for uid, hotkey, _, credit in passing:
            ok = bool(attested.get(hotkey, False))
            # Pay is what the gateway saw served, nothing else: an admitted card with no traffic earns nothing.
            score = card_equivalents(windows[uid]['tokens'], release, round_s) if ok else 0.0
            st = state.attest_status.get(hotkey, {})
            bt.logging.info(
                f'Serving: UID {uid} {release.release_id} attested {"yes" if ok else "no"} '
                f'speed credit {credit:.2f} tokens {windows[uid]["tokens"]} score {score:.4f} card-equivalents'
            )
            windows[uid].update(
                attested=ok,
                gpu_uuid=st.get('uuid', ''),
                gpu_uuids=st.get('uuids', [st['uuid']] if st.get('uuid') else []),
                attest_ms=st.get('wall_ms'),
                attest_reason=st.get('reason', 'not attested yet'),
                score=round(score, 6),
            )
            if not ok and not windows[uid]['last_miss']:
                windows[uid]['last_miss'] = f'not attested: {st.get("reason", "not attested yet")}'
            if not ok and uid not in probation:  # admission / failed attest: not READY, keep receiving baseline
                probation[uid] = ReadyMiner(
                    uid=uid, hotkey=hotkey, axon=axon_of[uid], score=0.0, release_id=release.release_id
                )
            if ok and (score, credit) > admitted.get(hotkey, (0.0, 0.0, ''))[:2]:
                admitted[hotkey] = (score, credit, release.release_id)

    scores = {hotkey: admitted.get(hotkey, (0.0,))[0] for _, hotkey, _ in active}
    ready: List[ReadyMiner] = []
    for uid, hotkey, axon in active:
        if hotkey in admitted:
            _, credit, release_id = admitted[hotkey]
            ready.append(ReadyMiner(uid=uid, hotkey=hotkey, axon=axon, score=credit, release_id=release_id))
            probation.pop(uid, None)
    for uid, report in windows.items():
        report['status'] = miner_status(report)
    quarantined = sum(1 for w in windows.values() if w['status'] == 'quarantined')
    summary.update(ready=len(ready), probation=len(probation), quarantined=quarantined, dormant=dormant)
    state.publish_round(ready, scores, list(probation.values()), {**summary, 'windows': windows})
    bt.logging.info(
        f'Serving round: served {summary.get("served", 0)} (gateway {summary.get("gateway", 0)} / baseline '
        f'{summary.get("baseline", 0)}) · pass {summary.get("pass", 0)} · miss {summary.get("miss", 0)} · '
        f'strike {summary.get("strike", 0)} · neutral {summary.get("neutral", 0)} · READY {len(ready)} '
        f'{[m.uid for m in ready]} · probation {len(probation)} · quarantined {quarantined} · dormant {dormant} · '
        f'paid {sum(w["tokens"] for w in windows.values())} output tokens at '
        f'${token_rate_usd(loadout.releases[0]) * 1e6:.3f}/M'
    )
    return scores


def seed_ready_from_store(validator: 'Validator', state: ServingState, loadout=None) -> None:
    """Startup: republish the READY set from the durable verdicts so a restart does not 429 the gateway.

    Routing eligibility only — no scores settle from a seed, so nothing is paid for a round this validator did
    not run. ``last_round_ts`` comes restored from the store and is left alone: a restart longer than the READY
    TTL seeds nothing and the gateway waits for a live round, exactly as if the validator had never stopped.
    """
    if time.time() - state.last_round_ts > state.ready_ttl_s:
        return
    loadout = loadout or load_serving_loadout()
    serving = get_serving_axons(validator)
    active = [(uid, hotkey, axon) for uid, hotkey, axon in serving if not is_dormant(state, hotkey)]
    best: Dict[str, Tuple[float, str]] = {}
    quarantined: set[str] = set()
    for release in loadout.releases:
        for _, hotkey, _ in active:
            window = state.audits.verdict(hotkey, release.release_id)
            if window.quarantined_until > 0.0:
                quarantined.add(hotkey)
                continue
            if not window.passed or not status_passed(state.attest_status.get(hotkey), state.attest_round):
                continue
            # No measured credit on record routes nothing: the miner waits one live round in probation rather
            # than taking user traffic on an assumed speed.
            credit = state.last_credit.get(hotkey, 0.0)
            if credit > best.get(hotkey, (0.0, ''))[0]:
                best[hotkey] = (credit, release.release_id)
    ready: List[ReadyMiner] = []
    probation: List[ReadyMiner] = []
    fallback_release = loadout.primary.release_id
    for uid, hotkey, axon in active:
        credit, release_id = best.get(hotkey, (0.0, ''))
        if credit > 0.0:
            ready.append(ReadyMiner(uid=uid, hotkey=hotkey, axon=axon, score=credit, release_id=release_id))
        elif hotkey not in quarantined:
            probation.append(ReadyMiner(uid=uid, hotkey=hotkey, axon=axon, score=0.0, release_id=fallback_release))
    if ready or probation:
        state.seed_ready(ready, probation)
        bt.logging.info(
            f'Serving: seeded READY {sorted(m.uid for m in ready)} · probation {sorted(m.uid for m in probation)} '
            f'from the durable store ({time.time() - state.last_round_ts:.0f}s since its last round)'
        )


def update_dormancy(
    state: ServingState, serving: Sequence[Tuple[int, str, bt.AxonInfo]], served: Sequence[ServedRequest]
) -> None:
    """A completion resets a hotkey's dormancy count; a round of requests with none bumps it. Unasked = unchanged.
    A busy refusal is an answer — the axon is alive, just full — else a saturated probation miner would go dormant
    on refused baseline probes and starve out of the probe stream."""
    asked = {req.hotkey for req in served}
    answered = {req.hotkey for req in served if req.completion or (not req.ok and is_busy_detail(req.detail))}
    for _, hotkey, _ in serving:
        if hotkey in answered:
            state.dormant_rounds[hotkey] = 0
        elif hotkey in asked:
            state.dormant_rounds[hotkey] = state.dormant_rounds.get(hotkey, 0) + 1


def is_dormant(state: ServingState, hotkey: str) -> bool:
    return state.dormant_rounds.get(hotkey, 0) >= SERVING_DORMANT_AFTER_ROUNDS


def skip_baseline(state: ServingState, hotkey: str) -> bool:
    """Dormant hotkeys get no baseline prompts except one retry every SERVING_DORMANT_RETRY_ROUNDS; a skipped
    round still counts so the retry clock keeps moving."""
    n = state.dormant_rounds.get(hotkey, 0)
    if n < SERVING_DORMANT_AFTER_ROUNDS or n % SERVING_DORMANT_RETRY_ROUNDS == 0:
        return False
    state.dormant_rounds[hotkey] = n + 1
    return True


def miner_status(report: dict) -> str:
    """'ready' | 'quarantined' | 'probation' for one UID's round report. READY is admission, not pay: an attested
    miner that served nothing this round is still routed."""
    if report.get('attested'):
        return 'ready'
    if report.get('quarantined_until', 0.0) > 0.0:
        return 'quarantined'
    return 'probation'


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
        store: Optional[ServingStore] = None,
    ):
        self.validator = validator
        self.state = state
        self.store = store
        self.interval_s = interval_s
        self.baseline_per_round = baseline_per_round
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='serving-audits', daemon=True)
        # Own connection: psycopg connections are not shared across threads, and the OSS round holds the other one.
        self.storage: Optional[ServingRoundStorage] = ServingRoundStorage() if STORE_DB_RESULTS else None

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
        # Baseline traffic and attestation go to every serving miner at once; aiohttp's default
        # connector caps a session at 100 connections, which would queue late miners on the validator and skew
        # their measured throughput. Uncapped connector for the audit dendrite only.
        dendrite._session = loop.run_until_complete(_unlimited_session())
        try:
            seed_ready_from_store(self.validator, self.state)
        except Exception as e:  # a seed is best-effort: the first live round publishes either way
            bt.logging.warning(f'Serving: could not seed READY from the durable store ({e!r})')
        # Validators audit on the same interval; a per-hotkey phase offset keeps their attest cohorts from landing on a
        # miner at the same instant and each reading half a card.
        self._stop.wait(probe_phase_offset(self.validator.wallet.hotkey.ss58_address, self.interval_s))
        while not self._stop.is_set():
            started = time.monotonic()
            serving: List[Tuple[int, str, bt.AxonInfo]] = []
            try:
                serving = get_serving_axons(self.validator)
                loop.run_until_complete(
                    audit_round(
                        self.state,
                        dendrite,
                        serving,
                        staked_caller=is_staked_caller(self.validator),
                        round_s=self.interval_s,
                    )
                )
            except Exception as e:  # a serving fault must never take the validator down
                # Nothing is published: the READY set stands until the TTL runs out and no hotkey records a zero
                # for a round this validator did not run. Publishing an empty round here zeroed every settled
                # score and blanked the gateway on any hiccup — a metagraph read, one malformed reply.
                bt.logging.error(f'Serving round failed, nothing settled this round: {e!r}')
            if self.store is not None:
                try:
                    self.store.save(self.state)
                except Exception as e:
                    bt.logging.warning(f'Serving: could not persist serving state to {self.store.path}: {e!r}')
            if self.storage is not None:
                try:
                    release = load_serving_loadout().primary
                except Exception:
                    release = None
                self.storage.store_round(
                    validator_hotkey=self.validator.wallet.hotkey.ss58_address,
                    state=self.state,
                    pricing=getattr(self.validator, 'last_serving_pricing', None),
                    release=release,
                    allow_unpriced_cap=SERVING_PAY_CAP_WITHOUT_PRICING,
                )
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


def is_staked_caller(self: 'Validator') -> bool:
    """Does this validator's hotkey clear the miners' stake floor (unlimited calls, no budget to be refused on)?"""
    try:
        return float(self.metagraph.S[self.uid]) >= float(
            os.getenv('SERVING_MIN_CALLER_STAKE', SERVING_MIN_CALLER_STAKE)
        )
    except (IndexError, TypeError, ValueError, AttributeError):
        return False


def get_serving_axons(self: 'Validator') -> List[Tuple[int, str, bt.AxonInfo]]:
    """(uid, hotkey, axon) for every UID (excluding self) whose axon is serving — the candidate serving miners.

    Snapshot of the metagraph taken on the audit thread. Beta heuristic: axon.is_serving is the only signal.
    UIDs that are actively validating (validator_trust > 0) are skipped — they serve axons for PAT handling, never
    inference. A permit alone is not the signal: on a small subnet nearly every UID holds one. A serving-miner
    registry replaces this later.
    """
    hotkeys = list(self.metagraph.hotkeys)
    axons = list(self.metagraph.axons)
    vtrust = getattr(self.metagraph, 'validator_trust', None)
    serving: List[Tuple[int, str, bt.AxonInfo]] = []
    for uid, (hotkey, axon) in enumerate(zip(hotkeys, axons)):
        if uid == self.uid:
            continue
        try:
            if vtrust is not None and float(vtrust[uid]) > 0.0:
                continue
        except (IndexError, TypeError, ValueError):
            pass
        if axon is not None and axon.is_serving:
            serving.append((uid, hotkey, axon))
    return serving
