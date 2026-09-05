"""Tests for the serving beta: deterministic backend, audit verification, gateway dispatch, emission pool blending."""

import asyncio
import json
import random
import time
from types import SimpleNamespace
from typing import Any, Dict

import bittensor as bt
import pytest
from fastapi.testclient import TestClient

from gittensor.constants import (
    SERVING_AUDIT_MAX_ABS_LOGPROB_DIFF,
    SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF,
    SERVING_AUDIT_MIN_PREFIX_AGREEMENT,
    SERVING_AUDIT_WINDOW,
    SERVING_AUDIT_WINDOW_THRESHOLDS,
    SERVING_PRICING_MAX_AGE_S,
)
from gittensor.serving.api import build_app, parse_api_keys
from gittensor.serving.audit import (
    AuditCase,
    AuditWindow,
    BankReference,
    EchoReference,
    reference_for,
    verify_response,
    window_threshold,
)
from gittensor.serving.backends import EchoBackend, GenerationResult, expected_completion
from gittensor.serving.loadout import ECHO_LOADOUT_PATH, ServingLoadout, ServingRelease, load_serving_loadout
from gittensor.serving.state import ReadyMiner, RequestRecord, ServedRequest, ServingState, prompt_token_estimate
from gittensor.synapses import InferenceSynapse
from gittensor.validator.serving.scoring import latency_credit

MSGS = [{'role': 'user', 'content': 'prompt'}]


def _echo_release() -> ServingRelease:
    return ServingRelease(model_id='echo-v0', backend='echo', max_tokens=8)


def test_expected_completion_is_deterministic():
    a = expected_completion(MSGS, 8, 'echo-v0')
    b = expected_completion(MSGS, 8, 'echo-v0')
    assert a.completion == b.completion
    assert a.tokens == b.tokens and a.token_logprobs == b.token_logprobs
    assert a.tokens is not None and len(a.tokens) == 8


def test_expected_completion_varies_by_inputs():
    base = expected_completion(MSGS, 8, 'echo-v0').completion
    assert expected_completion([{'role': 'user', 'content': 'other'}], 8, 'echo-v0').completion != base
    assert expected_completion(MSGS, 8, 'other-model').completion != base


def test_echo_backend_matches_expected_completion():
    backend = EchoBackend(_echo_release())
    result = backend.generate(MSGS, 8, logprobs=True)
    ref = expected_completion(MSGS, 8, 'echo-v0')
    assert result.completion == ref.completion
    assert result.tokens == ref.tokens
    assert result.model_id == 'echo-v0'
    assert backend.generate(MSGS, 8).tokens is None


def test_default_loadout_targets_sparkinfer():
    release = load_serving_loadout().primary
    assert release.backend == 'openai-compat'
    assert release.base_url and release.audit_bank
    assert release.reference_url is None  # validators point SERVING_REFERENCE_URL at their own/rented runtime
    assert release.max_tokens > 0


def test_echo_loadout_loads_via_env(monkeypatch):
    monkeypatch.setenv('SERVING_LOADOUT_PATH', str(ECHO_LOADOUT_PATH))
    loadout = load_serving_loadout()
    assert loadout.primary.backend == 'echo'
    assert isinstance(reference_for(loadout.primary), EchoReference)


def test_loadout_rejects_empty_and_duplicate_releases():
    import pytest

    with pytest.raises(ValueError):
        ServingLoadout(releases=[])
    with pytest.raises(ValueError):
        ServingLoadout(releases=[_echo_release(), _echo_release()])


def test_reference_url_env_override_and_release_lookup(monkeypatch):
    monkeypatch.setenv('SERVING_LOADOUT_PATH', str(ECHO_LOADOUT_PATH))
    monkeypatch.setenv('SERVING_REFERENCE_URL', 'http://127.0.0.1:9999')
    loadout = load_serving_loadout()
    assert loadout.primary.reference_url == 'http://127.0.0.1:9999'
    assert loadout.get('echo-v0') is loadout.primary
    # echo backend never uses a live reference even if a URL is set
    assert isinstance(reference_for(loadout.primary), EchoReference)


def test_base_url_env_override_points_miner_at_compose_services(monkeypatch):
    monkeypatch.setenv('SERVING_LOADOUT_PATH', str(ECHO_LOADOUT_PATH))
    monkeypatch.setenv('SERVING_BASE_URL', 'http://runtime:8080')
    loadout = load_serving_loadout()
    assert loadout.primary.base_url == 'http://runtime:8080'
    assert loadout.primary.attest_url == 'http://runtime:8081'
    monkeypatch.setenv('SERVING_ATTEST_URL', 'http://attest:8081')
    assert load_serving_loadout().primary.attest_url == 'http://attest:8081'


def test_live_reference_wins_over_bank(monkeypatch):
    from gittensor.serving import audit

    release = ServingRelease(
        model_id='m',
        backend='openai-compat',
        base_url='http://x',
        reference_url='http://ref',
        reference_api_key='sekrit',
        audit_bank='nope.json',
    )
    ref = reference_for(release)
    assert isinstance(ref, audit.LiveReference)
    captured = {}

    def fake_greedy(base_url, model_id, messages, max_tokens, timeout, api_key=None):
        captured['base_url'] = base_url
        captured['api_key'] = api_key
        return {
            'messages': messages,
            'max_tokens': max_tokens,
            'reference_tokens': ['a', 'b'],
            'reference_logprobs': [-0.1, -0.2],
            'reference_completion': 'ab',
        }

    monkeypatch.setattr(audit, 'greedy', fake_greedy)
    case = ref.sample()
    assert captured['base_url'] == 'http://ref' and case.reference_tokens == ['a', 'b']
    assert captured['api_key'] == 'sekrit'
    assert ref.case_for(MSGS, 4).max_tokens == 4


# --- audit verification -----------------------------------------------------


def _case(n: int = 10) -> AuditCase:
    ref = expected_completion(MSGS, n, 'echo-v0')
    assert ref.tokens is not None and ref.token_logprobs is not None
    return AuditCase(messages=MSGS, max_tokens=n, reference_tokens=ref.tokens, reference_logprobs=ref.token_logprobs)


def test_verify_exact_match_passes():
    case = _case()
    v = verify_response(case, case.reference_tokens, case.reference_logprobs)
    assert v.passed and v.prefix_agreement == 1.0 and v.mean_abs_logprob_diff == 0.0


def test_verify_missing_logprobs_fails():
    case = _case()
    assert not verify_response(case, case.reference_tokens, None).passed
    assert not verify_response(case, None, None).passed
    assert not verify_response(case, case.reference_tokens, case.reference_logprobs[:-1]).passed


def test_verify_late_divergence_within_band_passes():
    case = _case(10)
    tokens = case.reference_tokens[:9] + ['xxx']
    v = verify_response(case, tokens, case.reference_logprobs, min_prefix_agreement=0.8)
    assert v.passed and v.prefix_agreement == 0.9


def test_verify_early_divergence_fails():
    case = _case(10)
    tokens = case.reference_tokens[:3] + ['x'] * 7
    v = verify_response(case, tokens, case.reference_logprobs, min_prefix_agreement=0.8)
    assert not v.passed and 'prefix agreement' in v.reason
    assert not verify_response(case, ['x'] * 10, case.reference_logprobs).passed


def test_verify_logprob_drift_fails():
    case = _case(10)
    drifted = [lp - 2.0 for lp in case.reference_logprobs]
    v = verify_response(case, case.reference_tokens, drifted, max_mean_abs_logprob_diff=0.5)
    assert not v.passed and 'drift' in v.reason
    assert v.prefix_agreement == 1.0


def test_audit_bank_roundtrip(tmp_path):
    case = _case(4)
    path = tmp_path / 'bank.json'
    path.write_text(
        json.dumps(
            {
                'model_id': 'echo-v0',
                'runtime_pin': 'x',
                'cases': [
                    {
                        'messages': case.messages,
                        'max_tokens': case.max_tokens,
                        'reference_tokens': case.reference_tokens,
                        'reference_logprobs': case.reference_logprobs,
                    }
                ],
            }
        )
    )
    bank = BankReference.load(path)
    assert len(bank) == 1 and bank.sample().reference_tokens == case.reference_tokens


# --- state / dispatch -------------------------------------------------------


def _ready(uid: int, score: float = 1.0) -> ReadyMiner:
    return ReadyMiner(uid=uid, hotkey=f'hk{uid}', axon=None, score=score, release_id='echo-v0')  # type: ignore[arg-type]


def test_state_least_inflight_dispatch():
    state = ServingState(_rng=random.Random(7))
    assert state.acquire() is None
    state.publish_round([_ready(1, 0.5), _ready(2, 1.0)], {})
    first = state.acquire()
    assert first is not None
    second = state.acquire()  # the idle miner is the only one with the fewest in flight
    assert second is not None and second.uid != first.uid
    assert state.inflight() == {1: 1, 2: 1}
    state.release(first.uid)
    again = state.acquire()  # ... and again once one of them frees up
    assert again is not None and again.uid == first.uid
    state.publish_round([_ready(1)], {})
    only = state.acquire()
    assert only is not None and only.uid == 1
    state.record(RequestRecord(ts=0, kind='gateway', uid=1, ok=True, latency_ms=10))
    assert state.snapshot()['gateway_ok'] == 1


def test_ready_routing_spreads_instead_of_always_picking_the_top_score():
    """Soak 7: at one request per 20 s nothing overlaps, so a deterministic tie-break sent every request to one
    miner - loading it until its own TTFT (1030 ms vs 461 ms) cut its credit to 0.68 while the other idled."""
    from collections import Counter

    state = ServingState(_rng=random.Random(0))
    state.publish_round([_ready(1, 0.4), _ready(2, 1.0)], {})
    picks = Counter()
    for _ in range(400):
        miner = state.acquire()
        assert miner is not None
        picks[miner.uid] += 1
        state.release(miner.uid)
    assert picks[1] > 0 and picks[2] > 0  # neither miner is starved of traffic
    assert picks[2] > picks[1]  # the better-scoring miner is still preferred
    assert state.inflight() == {1: 0, 2: 0}


def test_ready_routing_is_uniform_when_no_score_separates_them():
    from collections import Counter

    state = ServingState(_rng=random.Random(1))
    state.publish_round([_ready(1, 0.0), _ready(2, 0.0)], {})
    picks = Counter()
    for _ in range(200):
        miner = state.acquire()
        assert miner is not None
        picks[miner.uid] += 1
        state.release(miner.uid)
    assert picks[1] > 0 and picks[2] > 0


def test_acquire_filters_by_release():
    state = ServingState()
    other = ReadyMiner(uid=9, hotkey='hk9', axon=None, score=1.0, release_id='other-model')  # type: ignore[arg-type]
    state.publish_round([_ready(1), other], {})
    assert state.acquire('other-model') is other
    assert state.acquire('missing') is None
    picked = state.acquire('echo-v0')
    assert picked is not None and picked.uid == 1


def test_acquire_excludes_uids_that_already_refused_busy():
    state = ServingState(_rng=random.Random(7))
    state.publish_round([_ready(1), _ready(2)], {})
    picked = state.acquire(exclude={1})
    assert picked is not None and picked.uid == 2
    assert state.acquire(exclude={1, 2}) is None
    state = ServingState()
    state.publish_round([], {}, probation=[_ready(3)])
    assert state.acquire(probation=True, exclude={3}) is None


def test_busy_ledger_counts_within_window_and_shows_in_snapshot():
    state = ServingState()
    state.busy_refusal('hk1', now=time.time() - 10)
    state.busy_refusal('hk1', now=time.time() - 7200)  # outside the snapshot's trailing hour
    assert state.busy_count('hk1', 3600.0) == 1
    assert state.busy_count('hk1', 8000.0) == 2
    assert state.snapshot()['busy'] == {'hk1': 1}


# --- gateway ----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, result: GenerationResult, model_id: str):
        self.completion = result.completion
        self.served_model_id = model_id
        self.tokens = result.tokens
        self.token_ids = result.token_ids
        self.token_bytes = None
        self.token_logprobs = result.token_logprobs
        self.ttft_ms = 12.0
        self.decode_tps = 99.0
        self.finish_reason = 'stop'
        self.usage = result.usage


def _gateway_client(state: ServingState, monkeypatch, releases=None):
    loadout = ServingLoadout(releases=list(releases) if releases else [_echo_release()])

    async def fake_dispatch(dendrite, miner, messages, max_tokens, lo, timeout, on_event=None):
        result = expected_completion(messages, max_tokens, lo.model_id)
        if on_event is not None:  # replay the chunk sequence a miner would stream
            from gittensor.serving.stream import SSEParser, result_to_sse

            parser = SSEParser()
            for chunk in result_to_sse(result, 'chatcmpl-miner', 0, logprobs=True):
                for event in parser.feed(chunk):
                    await on_event(event)
        return _FakeResponse(result, lo.model_id)

    monkeypatch.setattr('gittensor.serving.api._dispatch', fake_dispatch)
    app = build_app(state, loadout, parse_api_keys('k1, k2'), lambda: None, request_timeout=5)
    return TestClient(app)


def test_gateway_requires_key(monkeypatch):
    client = _gateway_client(ServingState(), monkeypatch)
    assert client.get('/v1/models').status_code == 401
    assert client.get('/v1/models', headers={'Authorization': 'Bearer nope'}).status_code == 401
    r = client.get('/v1/models', headers={'Authorization': 'Bearer k2'})
    assert r.status_code == 200 and set(r.json()['data'][0]) >= {'id', 'runtime_pin', 'model_sha256'}
    assert client.get('/health').status_code == 200


def test_gateway_429_without_ready_miners(monkeypatch):
    client = _gateway_client(ServingState(), monkeypatch)
    r = client.post('/v1/chat/completions', json={'messages': MSGS}, headers={'Authorization': 'Bearer k1'})
    assert r.status_code == 429


class _BusyResponse:
    """What _dispatch returns when the miner's blacklist refused busy: a 403, nothing streamed."""

    completion = None
    tokens = None
    token_ids = None
    token_bytes = None
    token_logprobs = None
    usage = None
    ttft_ms = None
    decode_tps = None
    served_model_id = None
    finish_reason = None
    dendrite = SimpleNamespace(status_message='Forbidden. Key is blacklisted: busy: all backend slots in use.')


def _busy_then_serve_client(state: ServingState, monkeypatch, busy_first: int = 1):
    """A gateway whose first ``busy_first`` dispatches refuse busy and the rest serve; returns (client, calls)."""
    calls: list = []

    async def fake_dispatch(dendrite, miner, messages, max_tokens, lo, timeout, on_event=None):
        calls.append(miner.uid)
        if len(calls) <= busy_first:
            return _BusyResponse()
        result = expected_completion(messages, max_tokens, lo.model_id)
        if on_event is not None:  # replay the chunk sequence a miner would stream
            from gittensor.serving.stream import SSEParser, result_to_sse

            parser = SSEParser()
            for chunk in result_to_sse(result, 'chatcmpl-miner', 0, logprobs=True):
                for event in parser.feed(chunk):
                    await on_event(event)
        return _FakeResponse(result, lo.model_id)

    monkeypatch.setattr('gittensor.serving.api._dispatch', fake_dispatch)
    app = build_app(state, ServingLoadout(releases=[_echo_release()]), parse_api_keys('k1'), lambda: None, 5)
    return TestClient(app), calls


def test_gateway_retries_a_busy_miner_and_serves_from_the_next(monkeypatch):
    """One miner refusing busy costs the request nothing: the gateway releases it and serves from the other."""
    state = ServingState(_rng=random.Random(7))
    state.publish_round([_ready(1), _ready(2)], {})
    client, calls = _busy_then_serve_client(state, monkeypatch)
    r = client.post(
        '/v1/chat/completions', json={'messages': MSGS, 'max_tokens': 4}, headers={'Authorization': 'Bearer k1'}
    )
    assert r.status_code == 200
    assert set(calls) == {1, 2} and r.json()['gittensor']['served_uid'] == calls[1]
    assert state.inflight() == {1: 0, 2: 0}  # the busy leg released its slot
    refused, served = state.drain_served()
    assert not refused.ok and 'busy' in refused.detail and served.ok


def test_gateway_429_when_every_ready_miner_refuses_busy(monkeypatch):
    """Saturation surfaces as a clean 429, never as an invisible queue: rejected demand stays visible."""
    state = ServingState(_rng=random.Random(7))
    state.publish_round([_ready(1), _ready(2)], {})
    client, calls = _busy_then_serve_client(state, monkeypatch, busy_first=99)
    r = client.post(
        '/v1/chat/completions', json={'messages': MSGS, 'max_tokens': 4}, headers={'Authorization': 'Bearer k1'}
    )
    assert r.status_code == 429 and r.json()['detail'] == 'all serving capacity busy'
    assert set(calls) == {1, 2}  # both tried once; the exclude set stops a re-pick
    assert state.inflight() == {1: 0, 2: 0}
    drained = state.drain_served()
    assert len(drained) == 2 and all(not q.ok and 'busy' in q.detail for q in drained)


def test_gateway_stream_usage_chunk_is_opt_in(monkeypatch):
    """OpenAI emits the choices-less usage chunk only under stream_options.include_usage."""
    state = ServingState(_rng=random.Random(7))
    state.publish_round([_ready(1)], {})
    client = _gateway_client(state, monkeypatch)
    headers = {'Authorization': 'Bearer k1'}

    def stream(body):
        r = client.post('/v1/chat/completions', headers=headers, json=body)
        assert r.status_code == 200 and 'data: [DONE]' in r.text
        return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: {')]

    plain = stream({'messages': MSGS, 'stream': True})
    assert plain and all(c['choices'] for c in plain)
    opted = stream({'messages': MSGS, 'stream': True, 'stream_options': {'include_usage': True}})
    assert any(not c['choices'] and 'usage' in c for c in opted)


def test_gateway_rejects_nonpositive_max_tokens(monkeypatch):
    state = ServingState(_rng=random.Random(7))
    state.publish_round([_ready(1)], {})
    client = _gateway_client(state, monkeypatch)
    headers = {'Authorization': 'Bearer k1'}
    for bad in (0, -3):
        r = client.post('/v1/chat/completions', headers=headers, json={'messages': MSGS, 'max_tokens': bad})
        assert r.status_code == 400 and 'positive' in r.json()['detail']
    ok = client.post('/v1/chat/completions', headers=headers, json={'messages': MSGS})
    assert ok.status_code == 200  # absent still defaults to the release's max_tokens


def test_gateway_stream_retries_busy_before_committing_to_the_stream(monkeypatch):
    """A busy refusal streams nothing, so the retry happens before the client sees any bytes."""
    state = ServingState(_rng=random.Random(7))
    state.publish_round([_ready(1), _ready(2)], {})
    client, calls = _busy_then_serve_client(state, monkeypatch)
    with client.stream(
        'POST',
        '/v1/chat/completions',
        json={'messages': MSGS, 'max_tokens': 4, 'stream': True},
        headers={'Authorization': 'Bearer k1'},
    ) as r:
        assert r.status_code == 200
        raw = b''.join(r.iter_bytes())
    from gittensor.serving.stream import SSEParser

    events = SSEParser().feed(raw)
    assert events[-1] is None and set(calls) == {1, 2}  # a full stream, served by the miner that had room
    assert state.inflight() == {1: 0, 2: 0}


def test_gateway_chat_completion_roundtrip(monkeypatch):
    state = ServingState()
    state.publish_round([_ready(7)], {})
    client = _gateway_client(state, monkeypatch)
    r = client.post(
        '/v1/chat/completions',
        json={'messages': MSGS, 'max_tokens': 4, 'logprobs': True},
        headers={'Authorization': 'Bearer k1'},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['object'] == 'chat.completion'
    assert body['choices'][0]['message']['content'] == expected_completion(MSGS, 4, 'echo-v0').completion
    assert len(body['choices'][0]['logprobs']['content']) == 4
    assert body['gittensor']['served_uid'] == 7
    assert state.inflight() == {7: 0}
    assert state.snapshot()['gateway_ok'] == 1


def test_gateway_streams_sse(monkeypatch):
    state = ServingState()
    state.publish_round([_ready(7)], {})
    client = _gateway_client(state, monkeypatch)
    with client.stream(
        'POST',
        '/v1/chat/completions',
        json={'messages': MSGS, 'max_tokens': 4, 'stream': True, 'stream_options': {'include_usage': True}},
        headers={'Authorization': 'Bearer k1'},
    ) as r:
        assert r.status_code == 200
        assert r.headers['content-type'].startswith('text/event-stream')
        raw = b''.join(r.iter_bytes())
    from gittensor.serving.stream import SSEParser

    events = SSEParser().feed(raw)
    assert events[-1] is None  # [DONE]
    chunks = [e for e in events if e is not None]
    assert {e['id'] for e in chunks} == {chunks[0]['id']} and chunks[0]['id'].startswith('chatcmpl-')
    content = ''.join(c['delta'].get('content', '') for e in chunks for c in e['choices'])
    assert content == expected_completion(MSGS, 4, 'echo-v0').completion
    assert all('logprobs' not in c for e in chunks for c in e['choices'])  # not requested
    assert chunks[-1]['usage']['completion_tokens'] == 4 and chunks[-1]['gittensor']['served_uid'] == 7
    assert state.inflight() == {7: 0} and state.snapshot()['gateway_ok'] == 1


def test_latency_credit_bands(monkeypatch):
    monkeypatch.setattr('gittensor.validator.serving.scoring.SERVING_LATENCY_FULL_CREDIT_MS', 1_000.0)
    monkeypatch.setattr('gittensor.validator.serving.scoring.SERVING_LATENCY_ZERO_CREDIT_MS', 3_000.0)
    assert latency_credit(500.0) == 1.0
    assert latency_credit(1_000.0) == 1.0
    assert latency_credit(2_000.0) == 0.5
    assert latency_credit(3_000.0) == 0.0
    assert latency_credit(10_000.0) == 0.0


def test_window_threshold_interpolates_table():
    table = ((1, 0.1), (5, 0.3), (20, 0.5))
    assert window_threshold(0, table) == float('inf')
    assert window_threshold(1, table) == 0.1
    assert window_threshold(3, table) == pytest.approx(0.2)
    assert window_threshold(20, table) == 0.5
    assert window_threshold(50, table) == 0.5  # saturates at the largest window


def test_audit_window_rolls_per_hotkey_and_release():
    w = AuditWindow(size=3, thresholds=((1, 0.5),))
    assert not w.verdict('hk', 'm').passed and w.verdict('hk', 'm').n_audits == 0
    for x in (1.0, 1.0, 1.0, 0.0):  # oldest 1.0 rolls out -> mean 2/3
        w.record('hk', 'm', x)
    v = w.verdict('hk', 'm')
    assert v.n_audits == 3 and v.mean == pytest.approx(2 / 3) and v.passed
    w.record('hk', 'm', 0.0)  # mean 1/3 < 0.5
    assert not w.verdict('hk', 'm').passed
    w.record('hk', 'other', 1.0)  # releases are tracked separately
    assert w.verdict('hk', 'other').passed
    assert w.verdict('hk2', 'm').n_audits == 0  # a new hotkey on the same UID starts clean


def test_audit_bands_match_measured_calibration():
    """Bands vs the 2026-08-24 measurements on the deterministic pin (internal notes, serving-experiments):
    honest max |delta| 0.0000 on 40/40 prompts; nearest cheater prompt mean 0.0057 / max 0.129."""
    n = 40
    ref = AuditCase(messages=[], max_tokens=n, reference_tokens=['t'] * n, reference_logprobs=[-0.5] * n)
    assert verify_response(ref, ref.reference_tokens, ref.reference_logprobs).passed  # honest: exact
    assert verify_response(ref, ref.reference_tokens, [-0.5 - 0.004] * n).passed  # within float-noise budget
    nearest = [-0.5 - 0.0057] * (n - 1) + [-0.5 - 0.129]
    v = verify_response(ref, ref.reference_tokens, nearest)
    assert not v.passed and v.reason.startswith('logprob drift')  # mean band
    outlier = [-0.5] * (n - 1) + [-0.5 - 0.129]  # mean 0.003 passes, max 0.129 must not
    v = verify_response(ref, ref.reference_tokens, outlier)
    assert not v.passed and v.reason.startswith('logprob outlier')
    fork = ref.reference_tokens[:-1] + ['x']  # any token divergence fails on a deterministic runtime
    assert not verify_response(ref, fork, ref.reference_logprobs).passed


def test_audit_window_tolerates_one_miss_per_round():
    """Bar is 0.8 over the last 10: one miss in a round of 4 is fine, a whole missed round is not, and it clears
    once a clean round pushes the misses below 20% of the window."""
    w = AuditWindow()
    hk = 'hk'
    for _ in range(4):
        w.record(hk, 'm', 1.0)
    assert w.verdict(hk, 'm').passed
    w.record(hk, 'm', 0.0)  # 4/5 = 0.8 -> still READY
    assert w.verdict(hk, 'm').passed
    w.record(hk, 'm', 0.0)  # 4/6 < 0.8 -> drops
    assert not w.verdict(hk, 'm').passed
    for _ in range(4):
        w.record(hk, 'm', 1.0)  # 8/10 = 0.8 -> back after one clean round
    assert w.verdict(hk, 'm').passed
    assert window_threshold(1, SERVING_AUDIT_WINDOW_THRESHOLDS) == window_threshold(SERVING_AUDIT_WINDOW)


def test_release_speed_facts_price_capacity_and_latency(tmp_path):
    from gittensor.serving.loadout import load_serving_loadout

    raw = {
        'releases': [
            {
                'model_id': 'm',
                'backend': 'echo',
                'speed': {'ttft_full_ms': 300.0, 'ttft_zero_ms': 900.0},
            }
        ]
    }
    path = tmp_path / 'loadout.json'
    path.write_text(json.dumps(raw))
    release = load_serving_loadout(path).primary
    assert (release.ttft_full_ms, release.ttft_zero_ms) == (300.0, 900.0)
    assert latency_credit(300.0, release.ttft_full_ms, release.ttft_zero_ms) == 1.0
    assert latency_credit(600.0, release.ttft_full_ms, release.ttft_zero_ms) == pytest.approx(0.5)
    assert latency_credit(400.0) == 1.0 and latency_credit(600.0) == pytest.approx(0.9)  # constants' defaults
    bare = load_serving_loadout(ECHO_LOADOUT_PATH).primary
    assert bare.ttft_full_ms is None and bare.attest_reference_url is None


def test_latency_credit_matches_measured_latencies():
    """Honest on-box 64-token audit p95 was 166 ms (2026-08-22/24 measurements); llama.cpp ~600 ms."""
    honest_p95, intercontinental_rtt = 166.0, 250.0
    assert latency_credit(honest_p95 + intercontinental_rtt) == 1.0
    proxied = honest_p95 + intercontinental_rtt + 2 * 150.0  # validator -> miner -> remote GPU in another region
    assert latency_credit(proxied) < 1.0
    assert latency_credit(600.0 + intercontinental_rtt + 2 * 150.0) < 0.4  # slow runtime behind the proxy


def test_serving_store_round_trips_audit_thread_state(tmp_path):
    from gittensor.serving.store import ServingStore, serving_store_path

    path = serving_store_path(str(tmp_path))
    assert path == tmp_path / 'serving.db' and serving_store_path(None) is None
    store = ServingStore(path)  # type: ignore[arg-type]
    assert store.load(ServingState()).audits.verdict('hk', 'm').n_audits == 0  # empty store -> untouched state

    state = ServingState(settlement_rounds=3)
    state.audits = AuditWindow(size=3)
    for x in (0.2, 0.9, 0.8, 0.7):  # 0.2 rolls out
        state.audits.record('hk', 'm', x)
    state.audits.record('hk2', 'm', 1.0)
    state.audits.strike('hk3', 'm', now=1000.0)
    state.publish_round([], {'hk': 0.9})
    state.publish_round([], {'hk': 1.0})
    state.dormant_rounds['dead'] = 2
    store.save(state)

    loaded = store.load(ServingState(settlement_rounds=3, audits=AuditWindow(size=3)))
    assert loaded.audits.verdict('hk', 'm').as_dict() == state.audits.verdict('hk', 'm').as_dict()
    assert loaded.audits.verdict('hk2', 'm').n_audits == 1
    assert loaded.audits.quarantined_until('hk3', 'm', now=1500.0) == state.audits.quarantined_until(
        'hk3', 'm', now=1500.0
    )
    assert loaded.settled_scores() == state.settled_scores()
    assert loaded.dormant_rounds == {'dead': 2}

    state.audits.record('hk', 'm', 0.1)
    store.save(state)  # a second save replaces, never appends
    assert store.load(ServingState(audits=AuditWindow(size=3))).audits.verdict('hk', 'm').n_audits == 3


def test_serving_store_migrates_the_legacy_json_window(tmp_path):
    from gittensor.serving.store import ServingStore

    legacy = tmp_path / 'serving_audits.json'
    legacy.write_text(json.dumps({'values': [['hk', 'm', [1.0, 0.0, 1.0]]], 'quarantine': [['hk2', 'm', 9e12]]}))
    store = ServingStore(tmp_path / 'serving.db')
    assert store.migrate_json(legacy) and not legacy.exists() and (tmp_path / 'serving_audits.json.migrated').exists()
    loaded = store.load(ServingState())
    assert loaded.audits.verdict('hk', 'm').n_audits == 3 and loaded.audits.quarantined_until('hk2', 'm') > 0
    assert not store.migrate_json(legacy)  # already migrated
    (tmp_path / 'broken.json').write_text('nope')
    assert not store.migrate_json(tmp_path / 'broken.json')


def test_probe_score_parses_teacher_forced_response(monkeypatch):
    from gittensor.serving import probe

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                'tokens': ['Par', 'is'],
                'token_ids': [1, 2],
                'logprobs': [-0.1, -0.02],
                'sum_logprob': -0.12,
                'top_logprobs': [[{'token': 'Par', 'logprob': -0.1}], [{'token': 'is', 'logprob': -0.02}]],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7, 'ttft_ms': 3.0},
            }

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return FakeResp()

    monkeypatch.setattr(probe.requests, 'post', fake_post)
    out = probe.score('http://ref:8080/', 'm', [{'role': 'user', 'content': 'q'}], 'Paris', 5.0, api_key='k')
    assert seen['url'] == 'http://ref:8080/v1/score' and seen['headers'] == {'Authorization': 'Bearer k'}
    assert seen['body']['completion'] == 'Paris' and seen['body']['top_logprobs'] == 1
    assert out['tokens'] == ['Par', 'is'] and out['logprobs'] == [-0.1, -0.02] and out['argmax'] == ['Par', 'is']
    assert out['usage']['ttft_ms'] == 3.0 and out['token_ids'] == [1, 2]
    forced = probe.score(
        'http://ref:8080/', 'm', [{'role': 'user', 'content': 'q'}], 'Paris', 5.0, completion_token_ids=[1, 2]
    )
    assert seen['body']['completion_token_ids'] == [1, 2] and 'completion' not in seen['body']
    assert forced['token_ids'] == [1, 2]


def test_verify_served_forces_miner_token_ids():
    """With miner token ids the reference scores that exact sequence: no re-tokenization, no length mismatch."""
    from gittensor.serving.audit import verify_served

    release = _echo_release()
    good = _served(1, release)
    tokens, logprobs = list(good.tokens or []), list(good.token_logprobs or [])
    ids = list(range(100, 100 + len(tokens)))
    calls = []

    class ForcingReference:
        model_id = release.model_id

        def score_served(self, messages, completion, token_ids=None):
            calls.append(list(token_ids) if token_ids else None)
            if token_ids:  # exactly the forced positions; a forced end-of-turn is the model's own choice here
                extra = len(token_ids) - len(tokens)
                forced = tokens + ['<|im_end|>'] * extra
                return {'tokens': forced, 'logprobs': logprobs + [0.0] * extra, 'argmax': forced, 'usage': {}}
            retok = tokens + ['x']  # a fresh tokenization of the text disagrees on length
            return {'tokens': retok, 'logprobs': logprobs + [0.0], 'argmax': retok, 'usage': {}}

        def sample(self):
            raise NotImplementedError

        def __len__(self):
            return 1

    ref = ForcingReference()
    assert verify_served(ref, good.messages, good.completion, tokens, logprobs, token_ids=ids).passed
    assert calls[-1] == ids
    eos = verify_served(
        ref, good.messages, good.completion, tokens + ['<|im_end|>'], logprobs + [0.0], token_ids=ids + [7]
    )
    assert eos.passed and calls[-1] == ids + [7]  # the end-of-turn position is forced and judged too
    fallback = verify_served(ref, good.messages, good.completion, tokens, logprobs)
    assert calls[-1] is None and 'tokenization mismatch' in fallback.reason
    short = verify_served(ref, good.messages, good.completion, tokens, logprobs, token_ids=ids[:-1])
    assert calls[-1] is None and not short.passed  # a malformed id list falls back to the text path

    class BindingReference(ForcingReference):
        def score_served(self, messages, completion, token_ids=None):
            out = super().score_served(messages, completion, token_ids)
            out['bytes'] = [b'not the completion'] + [b''] * (len(out['tokens']) - 1)
            return out

    lie = verify_served(BindingReference(), good.messages, good.completion, tokens, logprobs, token_ids=ids)
    assert not lie.passed and not lie.hard and 'do not spell' in lie.reason

    spelled = [(t if i == 0 else ' ' + t).encode() for i, t in enumerate(tokens)]  # what the forced ids spell

    class ExactReference(ForcingReference):
        spell = spelled

        def score_served(self, messages, completion, token_ids=None):
            out = super().score_served(messages, completion, token_ids)
            out['bytes'] = list(self.spell)
            return out

    mine_bytes = [list(b) for b in spelled]
    assert verify_served(
        ExactReference(), good.messages, good.completion, tokens, logprobs, token_ids=ids, token_bytes=mine_bytes
    ).passed
    forged = [list(b'x')] + mine_bytes[1:]
    assert (
        'streamed bytes'
        in verify_served(
            ExactReference(), good.messages, good.completion, tokens, logprobs, token_ids=ids, token_bytes=forged
        ).reason
    )
    # a multibyte character split across streamed tokens: the stream's text carries U+FFFD, the bytes are fine
    assert good.completion is not None

    class AccentedReference(ExactReference):
        spell = ['\u00e9'.encode() + spelled[0][1:]] + spelled[1:]

    split_text = '\ufffd\ufffd' + good.completion[1:]
    assert verify_served(AccentedReference(), good.messages, split_text, tokens, logprobs, token_ids=ids).passed
    # ...but the text the user received cannot differ from what the verified bytes spell in any other way
    padded = good.completion + ' visit example.com'
    assert (
        'do not spell' in verify_served(ExactReference(), good.messages, padded, tokens, logprobs, token_ids=ids).reason
    )


def test_verify_served_early_stop_must_be_the_models_own():
    """Stopping short of max_tokens is fine only with an end-of-turn token the reference agrees on; a wrong first
    token on aligned positions is a wrong answer; a real runtime must report token ids."""
    from gittensor.serving.audit import verify_served

    release = _echo_release()
    good = _served(1, release)
    tokens, logprobs = list(good.tokens or []), list(good.token_logprobs or [])
    n = len(tokens)
    ids = list(range(100, 100 + n))

    class Reference:
        model_id = release.model_id

        def __init__(self, stops: bool):
            self.stops = stops

        def score_served(self, messages, completion, token_ids=None):
            k = len(token_ids or [])
            forced = tokens[:k]
            if k > len(tokens):
                forced = tokens + ['<|im_end|>']
            argmax = list(forced)
            if k > len(tokens) and not self.stops:
                argmax[-1] = 'more'  # the model would have kept going
            return {'tokens': forced, 'logprobs': (logprobs + [0.0])[:k], 'argmax': argmax, 'usage': {}}

        def sample(self):
            raise NotImplementedError

        def __len__(self):
            return 1

    half = n // 2
    short = verify_served(
        Reference(True),
        good.messages,
        good.completion,
        tokens[:half],
        logprobs[:half],
        token_ids=ids[:half],
        max_tokens=n,
    )
    assert not short.passed and not short.hard and 'without end-of-turn' in short.reason
    ended = verify_served(
        Reference(True),
        good.messages,
        good.completion,
        tokens + ['<|im_end|>'],
        logprobs + [0.0],
        token_ids=ids + [7],
        max_tokens=n + 5,
    )
    assert ended.passed
    faked = verify_served(
        Reference(False),
        good.messages,
        good.completion,
        tokens + ['<|im_end|>'],
        logprobs + [0.0],
        token_ids=ids + [7],
        max_tokens=n + 5,
    )
    assert not faked.passed and faked.hard  # the reference's argmax at the end-of-turn position was not end-of-turn
    wrong_first = verify_served(
        Reference(True), good.messages, good.completion, ['zzz'] + tokens[1:], logprobs, token_ids=ids
    )
    assert not wrong_first.passed and wrong_first.hard and 'first token' in wrong_first.reason
    real = ServingRelease(model_id=release.model_id, backend='openai-compat', base_url='http://x')
    no_ids = verify_served(Reference(True), good.messages, good.completion, tokens, logprobs, release=real)
    assert not no_ids.passed and no_ids.reason == 'no token ids'


def test_early_stop_without_a_listed_end_of_turn_is_verified_by_forcing_the_releases_eos_id():
    """sparkinfer 7498736 lists only the content tokens on a natural stop (measured 2026-08-28), so the validator
    appends the release's end-of-turn id and asks the reference whether the model would have stopped there."""
    from gittensor.serving.audit import verify_served

    release = ServingRelease(model_id='m', backend='openai-compat', base_url='http://x', end_of_turn_token_id=151645)
    tokens, logprobs, ids = ['Hi', ' there', '!'], [-0.1, -0.2, -0.3], [12675, 1017, 0]
    forced_ids = []

    class Reference:
        model_id = 'm'

        def __init__(self, stops: bool):
            self.stops = stops

        def score_served(self, messages, completion, token_ids=None):
            forced_ids.append(list(token_ids or []))
            k = len(token_ids or [])
            argmax = tokens + (['<|im_end|>'] if self.stops else [' and']) if k == 4 else tokens[:k]
            return {'tokens': tokens[:k], 'logprobs': (logprobs + [-0.5])[:k], 'argmax': argmax, 'usage': {}}

        def sample(self):
            raise NotImplementedError

        def __len__(self):
            return 1

    msgs = [{'role': 'user', 'content': 'Say hi in three words.'}]
    ok = verify_served(
        Reference(True), msgs, 'Hi there!', tokens, logprobs, token_ids=ids, release=release, max_tokens=64
    )
    assert ok.passed and forced_ids[-1] == ids + [151645]
    cut = verify_served(
        Reference(False), msgs, 'Hi there!', tokens, logprobs, token_ids=ids, release=release, max_tokens=64
    )
    assert not cut.passed and cut.hard and 'would have continued' in cut.reason
    full = verify_served(
        Reference(False), msgs, 'Hi there!', tokens, logprobs, token_ids=ids, release=release, max_tokens=3
    )
    assert full.passed and forced_ids[-1] == ids  # used the whole budget: nothing to append
    bare = ServingRelease(model_id='m', backend='openai-compat', base_url='http://x')
    no_id = verify_served(
        Reference(True), msgs, 'Hi there!', tokens, logprobs, token_ids=ids, release=bare, max_tokens=64
    )
    assert not no_id.passed and not no_id.hard and 'without end-of-turn' in no_id.reason


def test_verified_bytes_must_spell_the_users_completion():
    from gittensor.serving.audit import spells

    assert spells('héllo wörld'.encode(), 'héllo wörld')
    assert spells('héllo'.encode(), 'h��llo')  # one é split across two streamed chunks
    assert spells('h日本o'.encode(), 'h����o')
    assert not spells('hello'.encode(), 'h�llo')  # a replacement character cannot stand for ASCII
    assert not spells('héllo'.encode(), 'h��llo BUY NOW')
    assert not spells('héllo'.encode(), 'x��llo')
    assert not spells(b'hello', 'hello world')


def test_gateway_limits_the_prompt_by_context_window_not_characters(monkeypatch):
    """The prompt may fill the release's context window (36,864 tokens on the blessed 5090 release, ~150k
    characters): a 30k-token prompt is routed with max_tokens clamped to what the window has left, a prompt the
    window cannot hold at all is OpenAI's 400 context_length_exceeded, and only a ~0.25 MB body is a 413."""
    from gittensor.constants import SERVING_CONTEXT_TOKENS_FALLBACK, SERVING_MAX_PROMPT_CHARS, SERVING_MAX_TOKENS
    from gittensor.serving import api as api_module
    from gittensor.serving.loadout import load_serving_loadout

    assert SERVING_MAX_TOKENS == 4096 and SERVING_MAX_PROMPT_CHARS >= 4 * SERVING_CONTEXT_TOKENS_FALLBACK
    shipped = load_serving_loadout().primary
    assert shipped.context_tokens == 36_864 and shipped.request_timeout >= 300.0  # 4096 tokens at ~19 tok/s fits

    good = _echo_release()
    state = ServingState()
    state.publish_round([ReadyMiner(uid=1, hotkey='hk1', axon=None, score=1.0, release_id=good.release_id)], {})  # type: ignore[arg-type]
    app = build_app(state, ServingLoadout(releases=[good]), {'k'}, lambda: object(), 5.0)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer k'}
    seen: Dict[str, int] = {}

    async def dispatch(dendrite, miner, messages, max_tokens, release, timeout, on_event=None):
        seen['max_tokens'] = max_tokens
        syn = InferenceSynapse(messages=messages, model_id=release.model_id, max_tokens=max_tokens)
        syn.completion, syn.served_model_id, syn.tokens, syn.token_logprobs = 'hi', release.model_id, ['hi'], [-0.1]
        return syn

    monkeypatch.setattr(api_module, '_dispatch', dispatch)
    context = SERVING_CONTEXT_TOKENS_FALLBACK
    long_prompt = [{'role': 'user', 'content': 'x' * (30_000 * 4)}]  # ~30k tokens: fits, with ~6.8k left
    r = client.post('/v1/chat/completions', headers=headers, json={'messages': long_prompt, 'max_tokens': 4096})
    assert r.status_code == 200 and seen['max_tokens'] == 4096
    nearly_full = [{'role': 'user', 'content': 'x' * (35_000 * 4)}]  # ~35k tokens: the completion is clamped
    r = client.post('/v1/chat/completions', headers=headers, json={'messages': nearly_full, 'max_tokens': 4096})
    assert r.status_code == 200 and seen['max_tokens'] == context - prompt_token_estimate(nearly_full)
    assert len(state.drain_served()) == 2  # both routed and recorded
    too_big = [{'role': 'user', 'content': 'x' * (context * 4)}]
    r = client.post('/v1/chat/completions', headers=headers, json={'messages': too_big})
    assert (
        r.status_code == 400 and 'context_length_exceeded' in r.json()['detail'] and str(context) in r.json()['detail']
    )
    assert not state.drain_served()
    # a release may carry its own window
    small = _echo_release()
    small.context_tokens = 1000
    app = build_app(ServingState(), ServingLoadout(releases=[small]), {'k'}, lambda: object(), 5.0)
    r = TestClient(app).post('/v1/chat/completions', headers=headers, json={'messages': long_prompt})
    assert r.status_code == 400 and '1000 tokens' in r.json()['detail']


def test_gateway_caps_prompt_size_and_stamps_the_miners_stream_end(monkeypatch):
    """An oversized prompt is refused at the door (413), never sent to a miner. A request's latency is the end of
    the miner's stream, not the moment a slow client finished reading it; and a stream the assembler cannot fold
    releases the miner's in-flight slot like any other miss."""
    from gittensor.constants import SERVING_MAX_PROMPT_CHARS
    from gittensor.serving import api as api_module

    good = _echo_release()
    state = ServingState()
    state.publish_round([ReadyMiner(uid=1, hotkey='hk1', axon=None, score=1.0, release_id=good.release_id)], {})  # type: ignore[arg-type]
    app = build_app(state, ServingLoadout(releases=[good]), {'k'}, lambda: object(), 5.0)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer k'}
    big = client.post(
        '/v1/chat/completions',
        headers=headers,
        json={'messages': [{'role': 'user', 'content': 'x' * (SERVING_MAX_PROMPT_CHARS + 1)}]},
    )
    assert big.status_code == 413 and state.inflight().get(1, 0) == 0 and not state.drain_served()

    async def slow_reader_dispatch(dendrite, miner, messages, max_tokens, release, timeout, on_event=None):
        syn = InferenceSynapse(messages=messages, model_id=release.model_id, max_tokens=max_tokens)
        syn.completion, syn.served_model_id, syn.observed_latency_ms = 'hi', release.model_id, 12.0
        if on_event is not None:
            await on_event({'choices': [{'delta': {'content': 'hi'}}], 'model': release.model_id})
            await on_event(None)
        return syn

    monkeypatch.setattr(api_module, '_dispatch', slow_reader_dispatch)
    r = client.post('/v1/chat/completions', headers=headers, json={'messages': MSGS, 'stream': True})
    assert r.status_code == 200
    (served,) = state.drain_served()
    assert served.ok and served.latency_ms == 12.0

    async def broken_dispatch(dendrite, miner, messages, max_tokens, release, timeout, on_event=None):
        if on_event is not None:
            await on_event({'choices': [{'delta': {'content': 'hi'}}]})
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(api_module, '_dispatch', broken_dispatch)
    r = client.post('/v1/chat/completions', headers=headers, json={'messages': MSGS, 'stream': True})
    assert r.status_code == 200
    (served,) = state.drain_served()
    assert not served.ok and state.inflight().get(1, 0) == 0


def test_runtime_rejected_prompt_is_checked_against_the_reference(monkeypatch):
    """A gateway request the miner's runtime refused counts as a miss only if the validator's reference would have
    answered it; when the reference refuses it too (over context) nobody is blamed. Baseline prompts are the
    validator's own and never need the check."""
    from types import SimpleNamespace

    import requests

    from gittensor.validator.serving.forward import verify_served_round

    good = _echo_release()

    class Reference(EchoReference):
        def __init__(self, status):
            super().__init__(good)
            self.status = status

        def case_for(self, messages, max_tokens=None):
            if self.status:
                err = requests.HTTPError('nope')
                err.response = SimpleNamespace(status_code=self.status)
                raise err

    def run(reference, source='gateway'):
        state = ServingState()
        summary = {}
        failed = _served(1, good, ok=False)
        failed.source = source
        verify_served_round(state, reference, good, [failed], summary)
        return summary.get('neutral', 0), summary.get('miss', 0)

    monkeypatch.setattr('gittensor.validator.serving.forward.time.sleep', lambda s: None)
    assert run(Reference(400)) == (1, 0)
    assert run(Reference(429)) == (1, 0)  # saturated reference rules neither way: the miss is excused
    assert run(Reference(500)) == (0, 1)
    assert run(Reference(None)) == (0, 1)
    assert run(Reference(400), source='baseline') == (0, 1)
    assert run(EchoReference(good)) == (0, 1)


def test_strikes_need_the_fleet_to_agree_with_the_reference():
    """When most hotkeys judged this round fail the bands, the reference drifted: misses, not strikes."""
    from gittensor.validator.serving.forward import verify_served_round

    good = _echo_release()
    ref = EchoReference(good)
    state = ServingState()
    summary = {}
    served = [_served(1, good, wrong=True), _served(2, good, wrong=True), _served(3, good)]
    verify_served_round(state, ref, good, served, summary)
    assert summary.get('strike', 0) == 0 and summary.get('miss') == 2 and summary.get('reference_disagreement') == 1
    assert state.audits.verdict('hk1', good.model_id).quarantined_until == 0.0
    state = ServingState()
    summary = {}
    served = [_served(1, good, wrong=True), _served(2, good), _served(3, good)]
    verify_served_round(state, ref, good, served, summary)
    assert summary.get('strike') == 1 and state.audits.verdict('hk1', good.model_id).quarantined_until > 0.0
    state = ServingState()
    summary = {}
    verify_served_round(state, ref, good, [_served(1, good, wrong=True)], summary)  # one hotkey: nothing to compare
    assert summary.get('strike') == 1


def test_quarantine_escalates_with_strikes_and_forgets_after_a_clean_week():
    """1 h, 4 h, 16 h and it stays there (64 h was cut 2026-09-05: an honest runtime can strike, see handle_attest).
    A strike after SERVING_STRIKE_FORGET_S without one starts the ladder over."""
    from gittensor.constants import SERVING_STRIKE_FORGET_S

    w = AuditWindow(quarantine_s=100.0)
    assert w.strike('hk', 'r', now=0.0) == 100.0
    assert w.strike('hk', 'r', now=0.0) == 400.0
    assert w.strike('hk', 'r', now=0.0) == 1600.0
    assert w.strike('hk', 'r', now=0.0) == 1600.0
    assert w.strike('hk', 'r', now=0.0) == 1600.0
    assert w.strike('other', 'r', now=0.0) == 100.0
    assert w.strikes('hk', 'r', now=SERVING_STRIKE_FORGET_S) == 5  # inside the week: the ladder stands
    assert w.verdict('hk', 'r', now=SERVING_STRIKE_FORGET_S + 1.0).strikes == 0  # a clean week: forgotten
    later = SERVING_STRIKE_FORGET_S + 1.0
    assert w.strike('hk', 'r', now=later) == later + 100.0  # and the next strike costs an hour again
    assert w.strike('hk', 'r', now=later + 10.0) == later + 10.0 + 400.0  # a repeat inside the week escalates


def test_last_credit_survives_a_restart(tmp_path):
    from gittensor.serving.store import ServingStore

    state = ServingState()
    state.last_credit['hk1'] = 0.75
    store = ServingStore(tmp_path / 'serving.db')
    store.save(state)
    assert store.load(ServingState()).last_credit == {'hk1': 0.75}


def test_stream_assembler_collects_token_ids():
    from gittensor.serving.stream import SSEParser, StreamAssembler, result_to_sse
    from gittensor.synapses import InferenceSynapse

    release = _echo_release()
    result = expected_completion(MSGS, 3, release.model_id)
    result.token_ids = [11, 12, 13]
    a = StreamAssembler()
    for event in SSEParser().feed(b''.join(result_to_sse(result, 'id', 0, True))):
        a.feed(event)
    syn = a.apply(InferenceSynapse(messages=MSGS, model_id=release.model_id))
    assert syn.token_ids == [11, 12, 13] and syn.tokens == result.tokens
    plain = StreamAssembler()
    result.token_ids = None
    for event in SSEParser().feed(b''.join(result_to_sse(result, 'id', 0, True))):
        plain.feed(event)
    assert plain.apply(InferenceSynapse(messages=MSGS, model_id=release.model_id)).token_ids is None


def test_emission_pools_fit_in_one_and_the_slack_burns():
    from gittensor.constants import (
        EMISSION_SHARE_TOLERANCE,
        OSS_EMISSION_SHARE,
        RECYCLE_UID,
        SERVING_EMISSION_SHARE_CAP,
    )
    from gittensor.validator.emission_allocation import blend_emission_pools

    assert OSS_EMISSION_SHARE + SERVING_EMISSION_SHARE_CAP <= 1.0 + EMISSION_SHARE_TOLERANCE
    # An empty round burns the whole emission: OSS slack, the unfunded serving cap, and the slice reserved for
    # neither pool all land on RECYCLE_UID, so on-chain normalization has nothing to redistribute.
    rewards = blend_emission_pools({}, {}, {RECYCLE_UID, 1}, None, {}, None)
    assert rewards[sorted({RECYCLE_UID, 1}).index(RECYCLE_UID)] == pytest.approx(1.0)
    assert sum(rewards) == pytest.approx(1.0)


def test_verify_rejects_malformed_reference():
    case = AuditCase(messages=[], max_tokens=4, reference_tokens=['a', 'b'], reference_logprobs=[-0.1])
    v = verify_response(case, ['a', 'b'], [-0.1, -0.2])
    assert not v.passed and v.reason == 'empty or malformed reference'


def test_gateway_400_on_user_shaped_bad_input(monkeypatch):
    state = ServingState()
    state.publish_round([_ready(7)], {})
    client = _gateway_client(state, monkeypatch)
    h = {'Authorization': 'Bearer k1'}
    assert (
        client.post('/v1/chat/completions', json={'messages': MSGS, 'max_tokens': 'lots'}, headers=h).status_code == 400
    )
    array_content = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    assert client.post('/v1/chat/completions', json={'messages': array_content}, headers=h).status_code == 400
    assert client.post('/v1/chat/completions', json={'messages': [{'role': 'user'}]}, headers=h).status_code == 400
    assert state.inflight() == {7: 0}


def _dendrite_echoing(good: ServingRelease, dead_axons=(), gpu_of=None, token_s: float = 0.0):
    """Fake streaming dendrite: honest echo chunks for every axon, nothing for `dead_axons`; counts calls per axon.

    ``gpu_of`` maps id(axon) -> a GPU key; when set, a request takes ``token_s`` per token times the number of
    requests in flight on that GPU, so hotkeys sharing a card slow each other down the way one real card does.
    """
    import asyncio
    from types import SimpleNamespace

    from gittensor.serving.stream import result_to_sse, sse_event

    calls: Dict[int, int] = {}  # id(axon) -> requests sent
    inflight: Dict[object, int] = {}

    async def call_stream(target_axon, synapse, timeout, deserialize):
        calls[id(target_axon)] = calls.get(id(target_axon), 0) + 1
        final = synapse.model_copy()
        if not any(target_axon is d for d in dead_axons):
            gpu = (gpu_of or {}).get(id(target_axon))
            if gpu is not None:
                yield sse_event({'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})
                inflight[gpu] = inflight.get(gpu, 0) + 1
                await asyncio.sleep(0)  # let the other concurrent requests register before we measure load
                await asyncio.sleep(token_s * synapse.max_tokens * inflight[gpu])
                inflight[gpu] -= 1
            ref = expected_completion(synapse.messages, synapse.max_tokens, good.model_id)
            ref.model_id = good.model_id
            for chunk in result_to_sse(ref, 'chatcmpl-miner', 0, logprobs=True):
                yield chunk
            final.dendrite.process_time = 0.05  # 50 ms -> full latency credit
        yield final

    return SimpleNamespace(call_stream=call_stream), calls


def _served(uid: int, release: ServingRelease, ok: bool = True, wrong: bool = False, latency_ms: float = 50.0):
    """A gateway request as served by an honest echo miner (or a dead / wrong-model one)."""
    import secrets

    messages = [{'role': 'user', 'content': secrets.token_hex(8)}]
    ref = expected_completion(messages, release.max_tokens, release.model_id)
    tokens = list(ref.tokens or [])
    logprobs = list(ref.token_logprobs or [])
    if wrong:
        tokens[len(tokens) // 2] = 'xxxxxxxx'
    return ServedRequest(
        ts=0.0,
        uid=uid,
        hotkey=f'hk{uid}',
        model_id=release.model_id,
        messages=messages,
        ok=ok,
        latency_ms=latency_ms if ok else None,
        completion=' '.join(tokens) if ok else None,
        tokens=tokens if ok else None,
        token_logprobs=logprobs if ok else None,
    )


ROUND_S = 300.0


def _round(state, dendrite, serving, release, monkeypatch, probes: int = 0):
    import asyncio

    from gittensor.validator.serving import forward as fwd

    return asyncio.run(
        fwd.audit_round(state, dendrite, serving, ServingLoadout(releases=[release]), round_s=ROUND_S)  # type: ignore[arg-type]
    )


def _pay(tokens: int, release: ServingRelease, round_s: float = ROUND_S) -> float:
    """The round score ``tokens`` gateway-served output tokens are worth: card-equivalents over the round."""
    from gittensor.validator.serving.scoring import card_equivalents

    return card_equivalents(tokens, release, round_s)


def _weights(state) -> Dict[int, float]:
    """Routing weight (speed credit) per READY UID."""
    return {m.uid: m.score for m in state.ready_miners()}


def test_verify_served_teacher_forces_the_completion():
    """An honest served answer passes; a changed token is a hard failure; a bad re-tokenization is a soft miss."""
    from gittensor.serving.audit import verify_served

    release = _echo_release()
    ref = EchoReference(release)
    good = _served(1, release)
    tokens, logprobs = list(good.tokens or []), list(good.token_logprobs or [])
    v = verify_served(ref, good.messages, good.completion, tokens, logprobs)
    assert v.passed and not v.hard
    eos = verify_served(ref, good.messages, good.completion, tokens + ['<|im_end|>'], logprobs + [0.0])
    assert eos.passed  # trailing end-of-turn token is stripped before comparing
    bad = _served(1, release, wrong=True)
    v = verify_served(ref, bad.messages, bad.completion, bad.tokens, bad.token_logprobs)
    assert not v.passed and v.hard and 'prefix agreement' in v.reason
    short = verify_served(ref, good.messages, good.completion, tokens[:-1], logprobs[:-1])
    assert not short.passed and not short.hard and 'tokenization mismatch' in short.reason
    assert not verify_served(ref, good.messages, None, None, None).passed


def test_audit_window_strike_wipes_and_quarantines(tmp_path):
    w = AuditWindow(quarantine_s=100.0)
    for _ in range(5):
        w.record('hk', 'm', 1.0)
    assert w.verdict('hk', 'm').passed
    until = w.strike('hk', 'm', now=1000.0)
    assert until == 1100.0
    v = w.verdict('hk', 'm', now=1050.0)
    assert not v.passed and v.n_audits == 0 and v.quarantined_until == 1100.0
    w.record('hk', 'm', 1.0)
    assert not w.verdict('hk', 'm', now=1050.0).passed  # still quarantined even with a clean record
    assert w.verdict('hk', 'm', now=1101.0).passed and w.verdict('hk', 'm', now=1101.0).quarantined_until == 0.0
    from gittensor.serving.store import ServingStore

    store = ServingStore(tmp_path / 'serving.db')
    store.save(ServingState(audits=w))
    again = store.load(ServingState(audits=AuditWindow(quarantine_s=100.0))).audits
    assert again.verdict('hk', 'm', now=1050.0).quarantined_until == 1100.0


def test_state_settles_over_trailing_rounds_and_serves_probation():
    from types import SimpleNamespace

    state = ServingState(settlement_rounds=4)
    axon = SimpleNamespace()
    new = ReadyMiner(uid=5, hotkey='hk5', axon=axon, score=0.0, release_id='echo-v0')  # type: ignore[arg-type]
    state.publish_round([_ready(1)], {'hk1': 1.0}, probation=[new])
    assert state.scores_for(['v', 'hk1', 'hk5']) == {1: 0.25}  # one clean round out of four
    user = state.acquire('echo-v0')
    assert user is not None and user.uid == 1  # users: READY only
    assert state.acquire('echo-v0', probation=True) is new  # baseline: the idle probation miner first
    busy = state.acquire('echo-v0', probation=True)
    assert busy is not None and busy.uid == 1  # one in flight on probation -> back to READY
    state.release(5)
    for _ in range(3):
        state.publish_round([_ready(1)], {'hk1': 1.0})
    assert state.scores_for(['v', 'hk1']) == {1: 1.0}
    state.publish_round([], {})  # miner vanished: the missing round counts 0
    assert state.scores_for(['v', 'hk1']) == {1: 0.75}
    for _ in range(3):
        state.publish_round([], {})
    assert state.scores_for(['v', 'hk1']) == {}  # fully settled out; no stale hotkeys kept
    state.enqueue_served(_served(1, _echo_release()))
    assert state.snapshot()['pending_verification'] == 1
    assert len(state.drain_served()) == 1 and state.drain_served() == []


def test_gateway_enqueues_served_requests_and_routes_baseline_to_probation(monkeypatch):
    from types import SimpleNamespace

    from gittensor.serving.api import build_app

    loadout = ServingLoadout(releases=[_echo_release()])
    state = ServingState()
    probation = ReadyMiner(uid=9, hotkey='hk9', axon=SimpleNamespace(), score=0.0, release_id='echo-v0')  # type: ignore[arg-type]
    state.publish_round([_ready(7)], {}, probation=[probation])

    async def fake_dispatch(dendrite, miner, messages, max_tokens, lo, timeout, on_event=None):
        return _FakeResponse(expected_completion(messages, max_tokens, lo.model_id), lo.model_id)

    monkeypatch.setattr('gittensor.serving.api._dispatch', fake_dispatch)
    app = build_app(
        state, loadout, parse_api_keys('user,base'), lambda: None, request_timeout=5, baseline_keys={'base'}
    )
    client = TestClient(app)
    for key, uid in (('user', 7), ('base', 9), ('base', 9), ('user', 7)):
        r = client.post(
            '/v1/chat/completions', json={'messages': MSGS, 'max_tokens': 4}, headers={'Authorization': f'Bearer {key}'}
        )
        assert r.status_code == 200 and r.json()['gittensor']['served_uid'] == uid, (key, r.text)
    served = state.drain_served()
    assert [q.uid for q in served] == [7, 9, 9, 7]
    assert all(q.ok and q.tokens and q.token_logprobs and q.completion for q in served)
    assert served[0].messages == MSGS and served[0].hotkey == 'hk7'


def test_audit_round_verifies_served_traffic(monkeypatch):
    """Served requests build the window; misses count 0; a wrong answer strikes; unverified axons go to probation."""
    from types import SimpleNamespace

    good = _echo_release()
    honest, cheater, fresh = (SimpleNamespace(is_serving=True) for _ in range(3))
    dendrite, _ = _dendrite_echoing(good)
    state = ServingState(settlement_rounds=1)
    serving = [(1, 'hk1', honest), (2, 'hk2', cheater), (3, 'hk3', fresh)]
    for _ in range(4):
        state.enqueue_served(_served(1, good))
    state.enqueue_served(_served(1, good, ok=False))
    state.enqueue_served(_served(2, good))
    state.enqueue_served(_served(2, good, wrong=True))

    scores = _round(state, dendrite, serving, good, monkeypatch)
    assert scores['hk1'] == pytest.approx(_pay(4 * 8, good))  # paid for the 4 completions it served, not the miss
    assert scores['hk2'] == 0.0 and scores['hk3'] == 0.0  # struck / unverified: nothing, whatever they served
    assert _weights(state) == {1: pytest.approx(0.8)}  # 4 of 5 served requests earned full latency credit
    w1, w2 = state.audits.verdict('hk1', good.model_id), state.audits.verdict('hk2', good.model_id)
    assert w1.passed and w1.n_audits == 5 and w1.mean == 0.8
    assert not w2.passed and w2.n_audits == 0 and w2.quarantined_until > 0  # struck
    assert [m.uid for m in state.ready_miners()] == [1]
    assert state.snapshot()['probation_uids'] == [3]  # the cheater is quarantined, not on probation
    assert sum(1 for r in state.recent(50) if r.kind == 'verify') == 7

    # A round in which nothing of hk1's was served pays nothing but keeps it READY, at the credit last measured
    # (0.8) rather than a perfect one the validator never observed.
    scores = _round(state, dendrite, serving, good, monkeypatch)
    assert scores['hk1'] == 0.0 and _weights(state) == {1: pytest.approx(0.8)}
    assert state.last_credit['hk1'] == 0.8
    assert state.last_round['windows'][1]['status'] == 'ready' and state.last_round['windows'][1]['tokens'] == 0


def test_baseline_round_spreads_prompts_and_queues_them_for_verification(monkeypatch):
    """Each live axon gets per_miner baseline prompts at random times; dead axons queue misses; quarantined skip."""
    import asyncio
    import random
    from types import SimpleNamespace

    from gittensor.validator.serving import forward as fwd

    good = _echo_release()
    live, dead, struck = (SimpleNamespace(is_serving=True) for _ in range(3))
    dendrite, calls = _dendrite_echoing(good, dead_axons=(dead,))
    state = ServingState(settlement_rounds=1)
    state.audits.strike('hk3', good.model_id)
    serving = [(1, 'hk1', live), (2, 'hk2', dead), (3, 'hk3', struck)]
    sent = asyncio.run(
        fwd.baseline_round(state, dendrite, serving, good, window_s=0.05, per_miner=2, rng=random.Random(7))  # type: ignore[arg-type]
    )
    assert sent == 4 and calls == {id(live): 2, id(dead): 2}
    queued = state.drain_served()
    assert sorted(q.uid for q in queued) == [1, 1, 2, 2]
    assert all(q.ok and q.tokens and q.messages[0]['role'] == 'user' for q in queued if q.uid == 1)
    assert all(not q.ok and q.detail for q in queued if q.uid == 2)
    assert len({q.messages[0]['content'] for q in queued}) == 4  # every prompt distinct
    for q in queued:
        state.enqueue_served(q)
    scores = _round(state, dendrite, serving, good, monkeypatch)
    assert scores == {'hk1': 0.0, 'hk2': 0.0, 'hk3': 0.0}  # baseline prompts anchor eligibility, not income
    assert _weights(state) == {1: 1.0}
    assert state.audits.verdict('hk1', good.model_id).n_audits == 2
    assert (
        state.audits.verdict('hk2', good.model_id).n_audits == 2
        and not state.audits.verdict('hk2', good.model_id).passed
    )


def test_budget_refusal_is_neutral_only_by_the_validators_own_ledger(monkeypatch):
    """The refusal text is the miner's to write. It is neutral only when this validator's own count of max_tokens
    sent in the trailing tempo is near the miner's allowance — and never for a staked caller, which has no budget."""
    from types import SimpleNamespace

    from gittensor.constants import SERVING_VALIDATOR_TOKENS_PER_TEMPO

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    dendrite, _ = _dendrite_echoing(good)

    def refused():
        r = _served(1, good, ok=False)
        r.detail = 'Validator audit budget spent (50000 tokens per tempo)'
        return r

    # An unstaked validator that sent almost nothing: the refusal is a lie -> a miss.
    state = ServingState(settlement_rounds=1)
    state.enqueue_served(_served(1, good))
    state.enqueue_served(refused())
    _round(state, dendrite, [(1, 'hk1', axon)], good, monkeypatch)
    assert state.audits.verdict('hk1', good.model_id).mean == 0.5

    # The same validator after sending the allowance: the refusal is plausible -> neutral.
    state = ServingState(settlement_rounds=1)
    state.charge_sent('hk1', SERVING_VALIDATOR_TOKENS_PER_TEMPO)
    state.enqueue_served(_served(1, good))
    state.enqueue_served(refused())
    _round(state, dendrite, [(1, 'hk1', axon)], good, monkeypatch)
    w = state.audits.verdict('hk1', good.model_id)
    assert w.n_audits == 1 and w.mean == 1.0

    # A staked caller is never on a budget: the refusal is a miss however much it sent.
    state = ServingState(settlement_rounds=1)
    state.charge_sent('hk1', SERVING_VALIDATOR_TOKENS_PER_TEMPO)
    state.enqueue_served(_served(1, good))
    state.enqueue_served(refused())
    asyncio.run(
        fwd_module().audit_round(
            state,
            dendrite,  # type: ignore[arg-type]
            [(1, 'hk1', axon)],  # type: ignore[list-item]
            ServingLoadout(releases=[good]),
            staked_caller=True,
        )
    )
    assert state.audits.verdict('hk1', good.model_id).mean == 0.5


def test_busy_refusal_is_neutral_and_tallied_never_a_strike(monkeypatch):
    """A miner refusing at capacity takes no window hit and earns nothing for the refused request; the refusal is
    tallied as headroom telemetry at the request's own timestamp. Unconditional — no ledger can corroborate the
    load other validators put on the card, and the refused tokens are already the penalty."""
    from types import SimpleNamespace

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    dendrite, _ = _dendrite_echoing(good)

    def refused():
        r = _served(1, good, ok=False)
        r.detail = 'Forbidden. Key is blacklisted: busy: all backend slots in use.'
        return r

    state = ServingState(settlement_rounds=1)
    state.enqueue_served(_served(1, good))
    state.enqueue_served(refused())
    _round(state, dendrite, [(1, 'hk1', axon)], good, monkeypatch)
    w = state.audits.verdict('hk1', good.model_id)
    assert w.n_audits == 1 and w.mean == 1.0  # the refusal never entered the window
    assert state.busy_count('hk1', 3600.0, now=1.0) == 1  # tallied once, at the request's ts (0.0)
    assert state.dormant_rounds.get('hk1', 0) == 0


def test_busy_refusals_keep_a_hotkey_out_of_dormancy():
    """A whole round of busy refusals is an alive-but-full card, not a dead axon; anything else starves a saturated
    probation miner out of the probe stream."""
    from types import SimpleNamespace

    good = _echo_release()
    fwd = fwd_module()
    state = ServingState()
    busy = _served(1, good, ok=False)
    busy.detail = 'Forbidden. Key is blacklisted: busy: all backend slots in use.'
    dead = _served(2, good, ok=False)
    dead.detail = 'timeout'
    serving = [(1, 'hk1', SimpleNamespace(is_serving=True)), (2, 'hk2', SimpleNamespace(is_serving=True))]
    fwd.update_dormancy(state, serving, [busy, dead])  # type: ignore[arg-type]
    assert state.dormant_rounds.get('hk1', 0) == 0 and state.dormant_rounds.get('hk2') == 1


def test_sent_token_ledger_is_kept_at_dispatch(monkeypatch):
    """Gateway and baseline requests both charge the validator's own ledger with the max_tokens they asked for."""
    from gittensor.validator.serving.forward import baseline_round

    good = _echo_release()
    state = ServingState()
    state.publish_round([ReadyMiner(uid=1, hotkey='hk1', axon=None, score=1.0, release_id=good.release_id)], {})  # type: ignore[arg-type]
    dendrite, _ = _dendrite_echoing(good)
    app = build_app(state, ServingLoadout(releases=[good]), {'k'}, lambda: dendrite, 5.0)
    client = TestClient(app)
    r = client.post(
        '/v1/chat/completions',
        headers={'Authorization': 'Bearer k'},
        json={'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 40},
    )
    assert r.status_code == 200
    assert state.sent_tokens('hk1', 3600.0) == 40
    axon = SimpleNamespace(is_serving=True)
    asyncio.run(baseline_round(state, dendrite, [(1, 'hk1', axon)], good, 0.0, per_miner=1))  # type: ignore[arg-type]
    assert state.sent_tokens('hk1', 3600.0) > 40
    assert state.sent_tokens('hk1', 3600.0, now=time.time() + 7200) == 0


def test_malformed_miner_data_is_a_miss_not_a_neutral():
    """bytes out of range, ids out of range, more tokens than asked for: the miner's miss, judged before the
    reference is ever asked. A reference that rejects the miner's ids (HTTP 4xx) is the same."""
    from gittensor.serving.audit import verify_served

    release = _echo_release()
    ref = EchoReference(release)
    good = _served(1, release)
    tokens, logprobs = list(good.tokens or []), list(good.token_logprobs or [])
    n = len(tokens)
    bad_bytes = verify_served(ref, good.messages, good.completion, tokens, logprobs, token_bytes=[[256]] * n)
    assert not bad_bytes.passed and not bad_bytes.hard and 'malformed token bytes' in bad_bytes.reason
    bad_ids = verify_served(ref, good.messages, good.completion, tokens, logprobs, token_ids=[10**9] * n)
    assert not bad_ids.passed and 'malformed token ids' in bad_ids.reason
    neg_ids = verify_served(ref, good.messages, good.completion, tokens, logprobs, token_ids=[-1] * n)
    assert not neg_ids.passed and 'malformed token ids' in neg_ids.reason
    long = verify_served(ref, good.messages, good.completion, tokens, logprobs, max_tokens=n - 2)
    assert not long.passed and 'tokens for a' in long.reason
    assert verify_served(ref, good.messages, good.completion, tokens, logprobs, max_tokens=n).passed


def test_reference_rejection_is_the_miners_miss_and_a_reference_fault_is_neutral(monkeypatch):
    from types import SimpleNamespace

    import requests

    from gittensor.validator.serving.forward import verify_served_round

    good = _echo_release()

    class Rejecting:
        model_id = good.model_id

        def __init__(self, status):
            self.status = status

        def score_served(self, messages, completion, token_ids=None):
            err = requests.HTTPError('nope')
            err.response = SimpleNamespace(status_code=self.status)
            raise err

        def sample(self):
            raise NotImplementedError

        def __len__(self):
            return 1

    monkeypatch.setattr('gittensor.validator.serving.forward.time.sleep', lambda s: None)
    state = ServingState()
    summary = {}
    verify_served_round(state, Rejecting(400), good, [_served(1, good)], summary)  # type: ignore[arg-type]
    assert summary.get('miss') == 1 and state.audits.verdict('hk1', good.model_id).mean == 0.0
    state = ServingState()
    summary = {}
    verify_served_round(state, Rejecting(503), good, [_served(1, good)], summary)  # type: ignore[arg-type]
    assert summary.get('neutral') == 1 and state.audits.verdict('hk1', good.model_id).n_audits == 0
    state = ServingState()
    summary = {}
    _, tokens = verify_served_round(state, Rejecting(429), good, [_served(1, good)], summary)  # type: ignore[arg-type]
    assert summary.get('neutral') == 1 and summary.get('miss', 0) == 0
    assert state.audits.verdict('hk1', good.model_id).n_audits == 0  # no window damage from reference saturation
    assert tokens.get('hk1', 0) > 0  # the gateway saw the tokens served; an unverifiable round still pays them


def test_reference_429_is_retried_until_a_slot_frees(monkeypatch):
    """R6 backpressure: the reference refusing at capacity is retried with backoff, and the verdict that lands
    once a slot frees is the real one — a burst never turns the ready set's audits into misses."""
    from types import SimpleNamespace

    import requests

    from gittensor.constants import SERVING_VERIFY_BUSY_RETRIES
    from gittensor.validator.serving.forward import verify_served_round

    good = _echo_release()

    class Saturated(EchoReference):
        def __init__(self, busy_calls):
            super().__init__(good)
            self.busy_calls = busy_calls
            self.slept = []

        def score_served(self, messages, completion, token_ids=None):
            if self.busy_calls > 0:
                self.busy_calls -= 1
                err = requests.HTTPError('too many requests')
                err.response = SimpleNamespace(status_code=429)
                raise err
            return super().score_served(messages, completion, token_ids=token_ids)

    ref = Saturated(SERVING_VERIFY_BUSY_RETRIES)
    monkeypatch.setattr('gittensor.validator.serving.forward.time.sleep', ref.slept.append)
    state = ServingState()
    summary = {}
    verify_served_round(state, ref, good, [_served(1, good)], summary)
    assert summary.get('pass') == 1 and summary.get('neutral', 0) == 0
    assert state.audits.verdict('hk1', good.model_id).n_audits == 1
    assert len(ref.slept) == SERVING_VERIFY_BUSY_RETRIES and ref.slept == sorted(ref.slept)  # backoff grows


def fwd_module():
    from gittensor.validator.serving import forward as fwd

    return fwd


def test_baseline_prompts_vary_in_shape_and_length():
    import random

    from gittensor.serving.baseline import baseline_max_tokens, make_baseline_prompt

    rng = random.Random(1)
    prompts = [make_baseline_prompt(rng)[0]['content'] for _ in range(200)]
    lengths = sorted(len(p) for p in prompts)
    assert len(set(prompts)) > 190 and lengths[0] < 120 and lengths[-1] > 2000
    assert {baseline_max_tokens(rng, 1024) for _ in range(200)} >= {64, 128, 256, 512}
    assert baseline_max_tokens(rng, 100) <= 100


def test_get_serving_axons_skips_active_validators_not_permit_holders():
    """On a small subnet nearly every UID holds a permit (testnet 422: 10 of 46, incl. the test miner); only UIDs
    with validator_trust > 0 are actually validating."""
    from types import SimpleNamespace

    from gittensor.validator.serving.forward import get_serving_axons

    axon = SimpleNamespace(is_serving=True)
    vali = SimpleNamespace(
        uid=0,
        metagraph=SimpleNamespace(
            hotkeys=['me', 'other-vali', 'miner-with-permit', 'miner', 'off'],
            axons=[axon, axon, axon, axon, SimpleNamespace(is_serving=False)],
            validator_permit=[True, True, True, False, False],
            validator_trust=[1.0, 0.99, 0.0, 0.0, 0.0],
        ),
    )
    assert [(u, h) for u, h, _ in get_serving_axons(vali)] == [(2, 'miner-with-permit'), (3, 'miner')]  # type: ignore[arg-type]


def test_round_summary_is_published_for_status(monkeypatch):
    from types import SimpleNamespace

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    dendrite, _ = _dendrite_echoing(good)
    state = ServingState(settlement_rounds=1)
    for _ in range(2):
        state.enqueue_served(_served(1, good))
        base = _served(1, good)
        base.source = 'baseline'
        state.enqueue_served(base)
    state.enqueue_served(_served(1, good, ok=False))
    _round(state, dendrite, [(1, 'hk1', axon), (2, 'hk2', axon)], good, monkeypatch)
    last = state.snapshot()['last_round']
    assert last['served'] == 5 and last['gateway'] == 3 and last['baseline'] == 2
    assert last['pass'] == 4 and last['miss'] == 1 and last['ready'] == 1 and last['probation'] == 1
    assert (
        last['windows'][1]['n_audits'] == 5
        and last['windows'][1]['served'] == 5
        and last['windows'][2]['n_audits'] == 0
    )


def test_consume_stream_observes_time_to_first_token():
    import asyncio
    from types import SimpleNamespace

    from gittensor.serving.stream import consume_stream
    from gittensor.synapses import InferenceSynapse

    good = _echo_release()
    dendrite, _ = _dendrite_echoing(good)
    axon = SimpleNamespace(is_serving=True)
    syn = InferenceSynapse(messages=MSGS, model_id=good.model_id, max_tokens=4, logprobs=True)
    out = asyncio.run(consume_stream(dendrite, axon, syn, 5.0))  # type: ignore[arg-type]
    assert out.completion and out.observed_ttft_ms is not None and 0.0 <= out.observed_ttft_ms < 5000.0
    dead_dendrite, _ = _dendrite_echoing(good, dead_axons=(axon,))
    miss = asyncio.run(consume_stream(dead_dendrite, axon, syn.model_copy(), 5.0))  # type: ignore[arg-type]
    assert miss.completion is None and miss.observed_ttft_ms is None


def test_consume_stream_first_byte_bound_cuts_silence_not_slowness():
    import asyncio
    from types import SimpleNamespace

    from gittensor.serving.stream import consume_stream
    from gittensor.synapses import InferenceSynapse

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    syn = InferenceSynapse(messages=MSGS, model_id=good.model_id, max_tokens=4, logprobs=True)

    class Silent:
        async def call_stream(self, **kw):
            await asyncio.sleep(10)
            yield kw['synapse']

    with pytest.raises(TimeoutError, match='first byte'):
        asyncio.run(consume_stream(Silent(), axon, syn, 60.0, first_byte_s=0.05))  # type: ignore[arg-type]

    class SlowTail:
        def __init__(self, inner):
            self.inner = inner

        async def call_stream(self, **kw):
            first = True
            async for chunk in self.inner.call_stream(**kw):
                if not first:
                    await asyncio.sleep(0.12)
                first = False
                yield chunk

    inner, _ = _dendrite_echoing(good)
    out = asyncio.run(consume_stream(SlowTail(inner), axon, syn.model_copy(), 5.0, first_byte_s=0.05))  # type: ignore[arg-type]
    assert out.completion


def test_first_byte_timeout_never_leaks_cancellation():
    """The audit thread died in soak 11 when the first-byte cancel surfaced as a bare CancelledError."""
    import asyncio
    from types import SimpleNamespace

    from gittensor.serving.stream import consume_stream
    from gittensor.synapses import InferenceSynapse

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    syn = InferenceSynapse(messages=MSGS, model_id=good.model_id, max_tokens=4, logprobs=True)

    class Stubborn:
        async def call_stream(self, **kw):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise asyncio.CancelledError('fresh cancel from stream internals')
            yield kw['synapse']

    with pytest.raises(TimeoutError, match='first byte'):
        asyncio.run(consume_stream(Stubborn(), axon, syn, 60.0, first_byte_s=0.05))  # type: ignore[arg-type]


def test_latency_credit_uses_time_to_first_token_not_total_latency(monkeypatch):
    """A long answer that streamed promptly earns full credit; a slow first token does not."""
    from types import SimpleNamespace

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    dendrite, _ = _dendrite_echoing(good)
    state = ServingState(settlement_rounds=1)
    prompt = _served(1, good, latency_ms=4800.0)  # 512 tokens took ~5 s ...
    prompt.ttft_ms = 120.0  # ... but the first token came fast
    slow = _served(2, good, latency_ms=900.0)
    slow.ttft_ms = 1000.0  # queued for a second before answering
    state.enqueue_served(prompt)
    state.enqueue_served(slow)
    scores = _round(state, dendrite, [(1, 'hk1', axon), (2, 'hk2', axon)], good, monkeypatch)
    assert _weights(state) == {
        1: 1.0,
        2: pytest.approx(0.5, abs=1e-3),
    }  # a 16-char prompt earns a sub-ms prefill allowance
    assert scores['hk1'] == scores['hk2'] == pytest.approx(_pay(8, good))  # speed routes; tokens pay


def test_ttft_credit_allows_for_the_prompts_own_prefill():
    """A 30k-token prompt takes an honest 5090 ~1.25 s to prefill, so a 1.4 s TTFT on it is a fast card; the same
    1.4 s on a 100-token prompt is a slow one. The allowance is sized by the reference's token count, never the
    miner's; without a reference count the validator estimates from the prompt text."""
    from gittensor.validator.serving.scoring import (
        prefill_allowance_ms,
        prompt_token_estimate,
        request_speed,
    )

    release = _echo_release()
    release.prefill_tps = 24_000.0
    assert prefill_allowance_ms(30_000, release) == pytest.approx(1250.0)
    assert prefill_allowance_ms(0, release) == 0.0 and prefill_allowance_ms(-5, release) == 0.0

    req = _served(1, release, latency_ms=1500.0)
    req.ttft_ms = 1400.0
    assert request_speed(req, release, prompt_tokens=30_000).credit == 1.0  # 150 ms residual: inside FULL
    assert request_speed(req, release, prompt_tokens=100).credit == pytest.approx(0.1, abs=0.01)  # ~1.4 s residual
    # the miner's own claim buys nothing: only the reference's count (or the validator's estimate) enters
    req.prompt_tokens = 1_000_000
    assert request_speed(req, release, prompt_tokens=100).credit == pytest.approx(0.1, abs=0.01)
    # no reference count: ~4 characters per token from the prompt text
    long = _served(1, release, latency_ms=1500.0)
    long.ttft_ms = 1400.0
    long.messages = [{'role': 'user', 'content': 'x' * 120_000}]  # ~30k tokens
    assert prompt_token_estimate(long.messages) == 30_001
    assert request_speed(long, release).credit == 1.0
    assert request_speed(req, release).credit == pytest.approx(0.1, abs=0.01)  # a 16-char prompt: no allowance


def test_verify_served_carries_the_references_prompt_token_count():
    """Teacher-forced scoring reports the prompt's token count; the verdict carries it for the speed credit and
    reads None when the reference reports nothing (echo, bank)."""
    from gittensor.serving.audit import EchoReference, verify_served

    good = _echo_release()
    req = _served(1, good)

    class Counting(EchoReference):
        def score_served(self, messages, completion, token_ids=None):
            return {**super().score_served(messages, completion, token_ids), 'usage': {'prompt_tokens': 4321}}

    plain = verify_served(EchoReference(good), req.messages, req.completion, req.tokens, req.token_logprobs)
    assert plain.passed and plain.prompt_tokens is None
    counted = verify_served(Counting(good), req.messages, req.completion, req.tokens, req.token_logprobs)
    assert counted.passed and counted.prompt_tokens == 4321


def test_ready_miner_misses_are_logged_with_a_reason(monkeypatch):
    from types import SimpleNamespace

    import bittensor as bt

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    dendrite, _ = _dendrite_echoing(good)
    state = ServingState(settlement_rounds=1)
    state.publish_round([_ready(1)], {'hk1': 1.0})
    seen: list = []
    monkeypatch.setattr(bt.logging, 'info', lambda msg, *a, **k: seen.append(str(msg)))
    for _ in range(4):
        state.enqueue_served(_served(1, good))
    miss = _served(1, good, ok=False)
    miss.detail = 'Request timeout after 60.0 seconds'
    state.enqueue_served(miss)
    state.enqueue_served(_served(2, good, ok=False))  # not READY: stays quiet
    _round(state, dendrite, [(1, 'hk1', axon), (2, 'hk2', axon)], good, monkeypatch)
    hits = [m for m in seen if 'READY UID 1 missed' in m]
    assert len(hits) == 1 and 'Request timeout after 60.0 seconds' in hits[0] and 'gateway request' in hits[0]
    assert not any('READY UID 2' in m for m in seen)


def test_audit_round_skips_release_without_reference(monkeypatch):
    """A release whose reference is unreachable is skipped and logged; the round still completes."""
    from types import SimpleNamespace

    good = _echo_release()
    bad = ServingRelease(
        model_id='ghost', backend='openai-compat', base_url='http://x', reference_url='http://127.0.0.1:1'
    )
    dendrite, _ = _dendrite_echoing(good)
    axon = SimpleNamespace(is_serving=True)
    state = ServingState(settlement_rounds=1)
    state.enqueue_served(_served(1, good))
    import asyncio

    from gittensor.validator.serving import forward as fwd

    scores = asyncio.run(
        fwd.audit_round(state, dendrite, [(1, 'hk1', axon)], ServingLoadout(releases=[bad, good]), round_s=ROUND_S)  # type: ignore[arg-type]
    )
    assert scores == {'hk1': pytest.approx(_pay(8, good))}
    assert [m.uid for m in state.ready_miners()] == [1]
    assert state.scores_for(['v', 'hk1']) == {1: pytest.approx(_pay(8, good))}
    assert state.scores_for(['v', 'other']) == {}  # UID 1's hotkey changed since the round: nothing carries over


def test_decode_speed_prices_served_requests_against_the_blessing_curve():
    from gittensor.validator.serving.scoring import decode_credit, expected_decode_tps, request_speed

    curve = {1: 440.0, 6: 46.0, 16: 19.0}
    assert expected_decode_tps(curve, 1) == 440.0 and expected_decode_tps(curve, 0) == 440.0
    assert expected_decode_tps(curve, 16) == 19.0 and expected_decode_tps(curve, 40) == 19.0
    # between points the aggregate (per-request x n) is interpolated, then divided by n: 6 -> 276, 16 -> 304 tok/s
    assert expected_decode_tps(curve, 11) == pytest.approx((276.0 + (304.0 - 276.0) * 0.5) / 11)
    assert expected_decode_tps(curve, 3) == pytest.approx((440.0 + (276.0 - 440.0) * 0.4) / 3)  # 124.8, not 282.4
    assert expected_decode_tps(None, 6) == 46.0  # constants' fallback curve
    assert decode_credit(440.0, 440.0) == 1.0 and decode_credit(600.0, 440.0) == 1.0  # never more than one card
    assert decode_credit(352.0, 440.0) == 1.0  # 0.8x: inside the tolerance for WAN-observed decode
    assert decode_credit(264.0, 440.0) == pytest.approx(0.75)  # 0.6x -> 0.6 / 0.8
    assert decode_credit(200.0, 440.0) == 0.0  # under the floor: shared card / not the blessed runtime

    release = _echo_release()
    release.decode_per_request = curve

    def req(tokens: int, ttft_ms: float, latency_ms: float, inflight: int = 1) -> ServedRequest:
        return ServedRequest(
            ts=0.0,
            uid=1,
            hotkey='hk1',
            model_id=release.model_id,
            messages=MSGS,
            ok=True,
            latency_ms=latency_ms,
            completion='x',
            tokens=['t'] * tokens,
            token_logprobs=[0.0] * tokens,
            ttft_ms=ttft_ms,
            inflight=inflight,
        )

    honest = req(64, 100.0, 100.0 + 64 / 440.0 * 1000.0)  # 440 tok/s after a 100 ms TTFT
    honest_speed = request_speed(honest, release)
    assert honest_speed.credit == pytest.approx(1.0)
    assert honest_speed.ttft_ms == 100.0 and honest_speed.decode_tps == pytest.approx(440.0)
    busy = req(64, 100.0, 100.0 + 64 / 19.0 * 1000.0, inflight=16)  # 19 tok/s is what one card does at 16 in flight
    assert request_speed(busy, release).credit == pytest.approx(1.0)
    shared = req(64, 100.0, 100.0 + 64 / 19.0 * 1000.0, inflight=1)  # 19 tok/s while we sent it one request
    assert request_speed(shared, release).credit == 0.0
    slowish = req(64, 100.0, 100.0 + 64 / 264.0 * 1000.0)
    assert request_speed(slowish, release).credit == pytest.approx(0.75)
    short = req(8, 100.0, 5_000.0)  # too few tokens to measure decode: TTFT band only
    assert request_speed(short, release).credit == 1.0 and request_speed(short, release).decode_tps is None
    slow_ttft = req(64, 1_000.0, 1_000.0 + 64 / 440.0 * 1000.0)
    assert request_speed(slow_ttft, release).credit == pytest.approx(0.5, abs=1e-3)


def test_gateway_and_baseline_record_inflight_at_dispatch(monkeypatch):
    state = ServingState()
    state.publish_round([_ready(7)], {})
    client = _gateway_client(state, monkeypatch)
    h = {'Authorization': 'Bearer k1'}
    assert client.post('/v1/chat/completions', json={'messages': MSGS}, headers=h).status_code == 200
    (served,) = state.drain_served()
    assert served.inflight == 1  # the only request in flight when it was dispatched


def test_release_speed_curve_parses(tmp_path):
    from gittensor.serving.loadout import load_serving_loadout

    raw = {'releases': [{'model_id': 'm', 'backend': 'echo', 'speed': {'decode_per_request': {'1': 440, '16': 19.5}}}]}
    path = tmp_path / 'loadout.json'
    path.write_text(json.dumps(raw))
    assert load_serving_loadout(path).primary.decode_per_request == {1: 440.0, 16: 19.5}


def test_token_pay_is_derived_from_the_release_speed(tmp_path):
    """Only the card-hour target is hand-set: the per-token rate is that target over what one card decodes in an
    hour, so a card flat out earns exactly the card-hour, 1.5 cards' worth of tokens earns 1.5, an idle card 0."""
    from gittensor.constants import (
        SERVING_AGGREGATE_DECODE_TPS_FALLBACK,
        SERVING_GPU_HOUR_USD,
        SERVING_PREFILL_TPS_FALLBACK,
        SERVING_PROMPT_TEMPLATE_TOKENS,
    )
    from gittensor.serving.loadout import load_serving_loadout
    from gittensor.validator.serving.scoring import (
        aggregate_decode_tps,
        card_equivalents,
        paid_prompt_tokens,
        paid_tokens,
        prefill_tps,
        prompt_token_ceiling,
        prompt_token_rate_usd,
        token_rate_usd,
    )

    release = _echo_release()
    release.aggregate_decode_tps = 280.0
    hour = int(280.0 * 3600)  # ~1.0M output tokens: one 5090 for an hour on the current runtime
    assert card_equivalents(hour, release, 3600.0) == pytest.approx(1.0)
    assert card_equivalents(hour // 2, release, 3600.0) == pytest.approx(0.5)
    assert card_equivalents(1_500_000, release, 3600.0) * SERVING_GPU_HOUR_USD == pytest.approx(1.04, abs=0.01)
    assert card_equivalents(hour // 12, release, 300.0) == pytest.approx(1.0)  # one 5-minute round flat out
    assert card_equivalents(0, release, 300.0) == 0.0 and card_equivalents(100, release, 0.0) == 0.0
    assert token_rate_usd(release) * 1e6 == pytest.approx(0.694, abs=0.001)  # $/M output
    assert token_rate_usd(release) * hour == pytest.approx(SERVING_GPU_HOUR_USD)
    bare = _echo_release()
    assert aggregate_decode_tps(bare) == SERVING_AGGREGATE_DECODE_TPS_FALLBACK
    raw = {
        'releases': [{'model_id': 'm', 'backend': 'echo', 'speed': {'aggregate_decode_tps': 400, 'prefill_tps': 30000}}]
    }
    path = tmp_path / 'loadout.json'
    path.write_text(json.dumps(raw))
    assert aggregate_decode_tps(load_serving_loadout(path).primary) == 400.0
    assert prefill_tps(load_serving_loadout(path).primary) == 30000.0
    assert load_serving_loadout().primary.aggregate_decode_tps  # the shipped release self-prices
    # the gateway pays what it asked for at most: a runtime that streams past max_tokens is not paid for it
    req = _served(1, release)
    assert paid_tokens(req) == 8
    req.max_tokens = 5
    assert paid_tokens(req) == 5
    req.tokens = None
    assert paid_tokens(req) == 0

    # Prefill is the same card-time at the card's prefill rate: an hour of prompt tokens is one card-hour too, and
    # a prompt-heavy request pays for the prefill it cost on top of its decode, not 1:1 with output.
    release.prefill_tps = 24_000.0
    assert prefill_tps(bare) == SERVING_PREFILL_TPS_FALLBACK
    prefill_hour = int(24_000.0 * 3600)  # ~86M prompt tokens
    assert card_equivalents(0, release, 3600.0, prefill_hour) == pytest.approx(1.0)
    assert card_equivalents(hour, release, 3600.0, prefill_hour) == pytest.approx(2.0)
    assert card_equivalents(150, release, 300.0, 30_000) == pytest.approx((150 / 280.0 + 30_000 / 24_000.0) / 300.0)
    assert card_equivalents(150, release, 300.0, 30_000) > card_equivalents(150, release, 300.0)
    assert card_equivalents(0, release, 300.0, 0) == 0.0 and card_equivalents(0, release, 0.0, 100) == 0.0
    assert prompt_token_rate_usd(release) * 1e6 == pytest.approx(0.0081, abs=0.0001)  # $/M prompt: ~1/85 of output
    assert token_rate_usd(release) / prompt_token_rate_usd(release) == pytest.approx(24_000.0 / 280.0)
    # the count is miner-reported, so it is clamped to what the prompt could tokenize to: a runtime claiming a
    # 100k-token prompt for 16 characters is paid one per character plus the template
    req = _served(1, release)
    req.prompt_tokens = 12
    assert paid_prompt_tokens(req) == 12
    req.prompt_tokens = 100_000
    assert (
        paid_prompt_tokens(req)
        == prompt_token_ceiling(req.messages)
        == 16 + len('user') + SERVING_PROMPT_TEMPLATE_TOKENS
    )
    req.prompt_tokens = -5
    assert paid_prompt_tokens(req) == 0


def test_pay_counts_gateway_tokens_the_reference_did_not_fail():
    """Unsampled completions pay in full; a sampled one pays only if it verified; a failed request and baseline
    prompts pay nothing; a neutral verdict (reference hiccup) still pays what the gateway saw served."""
    import random

    from gittensor.validator.serving.forward import verify_served_round

    good = _echo_release()
    ref = EchoReference(good)
    served = [_served(1, good) for _ in range(12)]  # min(10, 20%) sampled: 2 go unverified
    served += [_served(2, good), _served(2, good, wrong=True), _served(2, good, ok=False)]
    baseline = _served(3, good)
    baseline.source = 'baseline'
    served.append(baseline)
    for req in served:
        req.prompt_tokens = 7  # the runtime's usage.prompt_tokens; every prompt here is 16 hex chars
    state = ServingState()
    summary: Dict[str, int] = {}
    prompt: Dict[str, int] = {}
    speeds, tokens = verify_served_round(state, ref, good, served, summary, rng=random.Random(0), prompt_tokens=prompt)
    assert summary['unsampled'] == 2 and len(speeds['hk1']) == 10
    assert tokens == {'hk1': 12 * 8, 'hk2': 8}  # hk2: one completion paid, the wrong one and the miss are not
    assert prompt == {'hk1': 12 * 7, 'hk2': 7}  # prefill follows the same requests: nothing for the miss or baseline
    assert state.audits.verdict('hk2', good.release_id).quarantined_until > 0  # ... and the strike stands

    class Flaky:
        """A reference that cannot be reached: every verdict neutral."""

        model_id = good.model_id
        max_tokens = good.max_tokens

        def case_for(self, messages, max_tokens=None):
            raise ConnectionError('reference down')

    state = ServingState()
    _, tokens = verify_served_round(state, Flaky(), good, [_served(1, good)], {})  # type: ignore[arg-type]
    assert tokens == {'hk1': 8}


def test_round_pays_prefill_as_card_time(monkeypatch):
    """Two hotkeys serve the same completions; the one whose prompts were long is paid the prefill on top, at the
    release's prefill rate, and the round report carries both token counts for the DB and the UI."""
    from types import SimpleNamespace

    from gittensor.validator.serving.scoring import card_equivalents

    release = _echo_release()
    release.prefill_tps = 24_000.0
    dendrite, _ = _dendrite_echoing(release)
    state = ServingState(settlement_rounds=1)
    serving = [(1, 'hk1', SimpleNamespace(is_serving=True)), (2, 'hk2', SimpleNamespace(is_serving=True))]
    for i in range(3):
        state.enqueue_served(_served(1, release))
        long = _served(2, release)  # the same honest echo completion, on a 4000-character prompt
        long.messages = [{'role': 'user', 'content': f'{i}' + 'x' * 3999}]
        ref = expected_completion(long.messages, release.max_tokens, release.model_id)
        long.tokens, long.token_logprobs = list(ref.tokens or []), list(ref.token_logprobs or [])
        long.completion = ' '.join(long.tokens)
        long.prompt_tokens = 1000  # what the runtime reported; under the 4000 + template ceiling
        state.enqueue_served(long)
    scores = _round(state, dendrite, serving, release, monkeypatch)
    assert scores['hk1'] == pytest.approx(card_equivalents(3 * 8, release, ROUND_S))
    assert scores['hk2'] == pytest.approx(card_equivalents(3 * 8, release, ROUND_S, 3 * 1000))
    assert scores['hk2'] > scores['hk1']
    # 3000 prompt tokens at 24k tok/s is 0.125 s of card-time on top of 24 tokens' 0.086 s of decode
    assert scores['hk2'] / scores['hk1'] == pytest.approx(1 + (3000 / 24_000.0) / (24 / 280.0))
    windows = state.last_round['windows']
    assert windows[1]['tokens'] == 24 and windows[1]['prompt_tokens'] == 0
    assert windows[2]['tokens'] == 24 and windows[2]['prompt_tokens'] == 3000


def test_round_rows_carry_prompt_tokens_and_both_rates():
    """The persisted round carries the fleet's prompt tokens and the $/M prompt rate beside the output ones, and
    each miner row its own prompt tokens — as many values as the INSERTs have placeholders."""
    import datetime as dt

    from gittensor.validator.serving.persist import round_rows
    from gittensor.validator.storage.queries import BULK_INSERT_SERVING_MINER_ROUNDS, INSERT_SERVING_ROUND

    release = _echo_release()
    release.aggregate_decode_tps = 280.0
    release.prefill_tps = 24_000.0
    last_round = {
        'served': 5,
        'gateway': 4,
        'baseline': 1,
        'windows': {
            1: {'hotkey': 'hk1', 'model_id': release.model_id, 'tokens': 800, 'prompt_tokens': 30_000, 'score': 0.1},
            2: {'hotkey': 'hk2', 'model_id': release.model_id, 'tokens': 200, 'prompt_tokens': 0, 'score': 0.02},
        },
    }
    summary, miners = round_rows('vali', dt.datetime.now(dt.timezone.utc), last_round, {}, None, release)
    assert len(summary) == INSERT_SERVING_ROUND.count('%s')
    assert all(len(row) == BULK_INSERT_SERVING_MINER_ROUNDS.count('%s') for row in miners)
    assert summary[-4:-2] == (1000, pytest.approx(0.694, abs=0.001))  # output tokens, $/M output
    assert summary[-2] == 30_000 and summary[-1] == pytest.approx(0.0081, abs=0.0001)  # prompt tokens, $/M prompt
    assert sorted((row[2], row[-2], row[-1]) for row in miners) == [(1, 800, 30_000), (2, 200, 0)]


def test_emission_pool_is_the_only_ceiling(monkeypatch):
    """Below the cap every token is paid at the full rate; above it the pool is split by token share."""
    from gittensor.classes import ServingPricing
    from gittensor.validator import emission_allocation as ea

    monkeypatch.setattr(ea, 'SERVING_GPU_HOUR_USD', 0.70)
    monkeypatch.setattr(ea, 'SERVING_EMISSION_SHARE_CAP', 0.17)
    monkeypatch.setattr(ea, 'OSS_EMISSION_SHARE', 0.83)
    pricing = ServingPricing(alpha_per_hour_to_miners=100.0, alpha_usd=0.7)  # one card-hour = 1% of emissions
    uids = {0, 1, 2}
    below = ea.blend_emission_pools({}, {}, uids, None, {1: 1.5, 2: 0.5}, pricing)  # 1.5M + 0.5M tokens/h
    assert below[1] == pytest.approx(0.015) and below[2] == pytest.approx(0.005)  # full rate, no per-hotkey cap
    above = ea.blend_emission_pools({}, {}, uids, None, {1: 30.0, 2: 10.0}, pricing)  # 40 card-hours > 17 cap
    assert above[1] == pytest.approx(0.17 * 0.75) and above[2] == pytest.approx(0.17 * 0.25)
    assert above[0] == pytest.approx(0.83)  # nothing of the serving cap recycles once it binds


def test_serving_share_prices_gpu_hours_inside_the_cap(monkeypatch):
    from gittensor.classes import ServingPricing
    from gittensor.validator import emission_allocation as ea

    monkeypatch.setattr(ea, 'SERVING_GPU_HOUR_USD', 0.70)
    monkeypatch.setattr(ea, 'SERVING_EMISSION_SHARE_CAP', 0.17)
    # 2026-08-26: ~123 alpha/h to miners at $0.847/alpha -> one card is 0.70 / 104.2 = 0.67% of emissions
    pricing = ServingPricing(alpha_per_hour_to_miners=123.0, alpha_usd=0.847)
    assert ea.serving_share(0.0, pricing) == 0.0
    assert ea.serving_share(1.0, pricing) == pytest.approx(0.70 / (123.0 * 0.847))
    assert ea.serving_share(25.0, pricing) == pytest.approx(0.168, abs=0.001)
    assert ea.serving_share(100.0, pricing) == 0.17  # capped: 100 cards dilute
    # No usable pricing pays nothing: on a priced network a failed read must not hand one card the whole cap.
    assert ea.serving_share(1.0, None) == 0.0
    assert ea.serving_share(1.0, ServingPricing(0.0, 0.847)) == 0.0
    assert ea.serving_share(100.0, None) == 0.0
    # ... unless the network has no price to read at all (testnet), where the cap is split pro-rata.
    assert ea.serving_share(1.0, None, allow_unpriced_cap=True) == 0.17
    assert ea.serving_share(1.0, ServingPricing(0.0, 0.847), allow_unpriced_cap=True) == 0.17
    assert ea.serving_share(0.0, None, allow_unpriced_cap=True) == 0.0  # nothing verified is still nothing


def test_blend_pays_serving_by_price_and_recycles_the_rest(monkeypatch):
    from gittensor.classes import ServingPricing
    from gittensor.validator import emission_allocation as ea

    monkeypatch.setattr(ea, 'SERVING_GPU_HOUR_USD', 0.70)
    monkeypatch.setattr(ea, 'SERVING_EMISSION_SHARE_CAP', 0.17)
    monkeypatch.setattr(ea, 'OSS_EMISSION_SHARE', 0.83)
    pricing = ServingPricing(alpha_per_hour_to_miners=100.0, alpha_usd=0.7)  # one card = exactly 1% of emissions
    uids = {0, 1, 2}
    rewards = ea.blend_emission_pools({}, {}, uids, None, {1: 1.0, 2: 0.5}, pricing)
    assert rewards[1] == pytest.approx(0.010) and rewards[2] == pytest.approx(0.005)
    assert rewards[0] == pytest.approx(1.0 - 0.015)  # OSS slack (no repos) + the unfunded serving cap recycle
    assert sum(rewards) == pytest.approx(1.0)


def test_tao_usd_rate_caches_carries_and_falls_back(monkeypatch):
    from gittensor.validator.serving import pricing as pr

    calls = []
    monkeypatch.setattr(pr, '_fetch_tao_usd', lambda: calls.append(1) or 220.5)
    monkeypatch.setattr(pr, '_tao_usd_cache', None)
    assert pr.tao_usd_rate(now=0.0) == 220.5
    assert pr.tao_usd_rate(now=1.0) == 220.5 and len(calls) == 1  # cached inside the refresh window
    monkeypatch.setattr(pr, '_fetch_tao_usd', lambda: None)
    assert pr.tao_usd_rate(now=10_000.0) == 220.5  # refresh failed: the last fetched rate carries
    assert pr.tao_usd_rate(now=10_001.0) == 220.5  # ... and is re-stamped, so no retry until the next window
    monkeypatch.setattr(pr, '_tao_usd_cache', None)
    assert pr.tao_usd_rate(now=20_000.0) == pr.SERVING_TAO_USD_FALLBACK  # nothing ever fetched


def test_fetch_tao_usd_retries_then_gives_up(monkeypatch):
    import requests as rq

    from gittensor.validator.serving import pricing as pr

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {'bittensor': {'usd': 220.5}}

    attempts = []

    def flaky_get(url, timeout):
        attempts.append(url)
        if len(attempts) < 3:
            raise rq.ConnectionError('down')
        return _Resp()

    monkeypatch.setattr(pr.requests, 'get', flaky_get)
    assert pr._fetch_tao_usd() == 220.5 and len(attempts) == 3

    down = []

    def dead_get(url, timeout):
        down.append(url)
        raise rq.ConnectionError('down')

    monkeypatch.setattr(pr.requests, 'get', dead_get)
    assert pr._fetch_tao_usd() is None and len(down) == 3


def test_serving_pricing_reads_chain_and_feed(monkeypatch):
    from types import SimpleNamespace

    from gittensor.validator.serving import pricing as pr

    vali = SimpleNamespace(
        metagraph=SimpleNamespace(E=[100.0, 200.0], netuid=74),
        subtensor=SimpleNamespace(subnet=lambda netuid: SimpleNamespace(price=0.004)),
    )
    monkeypatch.setattr(pr, '_last_usable', None)
    monkeypatch.setattr(pr, 'tao_usd_rate', lambda: 250.0)
    p = pr.serving_pricing(vali)  # type: ignore[arg-type]
    assert p is not None and p.alpha_per_hour_to_miners == pytest.approx(150.0 * 60 / 72) and p.alpha_usd == 1.0

    # A read that comes back unusable, or throws, reuses the last usable pricing rather than dropping pay.
    vali.subtensor = SimpleNamespace(subnet=lambda netuid: SimpleNamespace(price=0.0))
    assert pr.serving_pricing(vali) == p  # type: ignore[arg-type]
    vali.subtensor = SimpleNamespace(subnet=lambda netuid: (_ for _ in ()).throw(RuntimeError('rpc')))
    assert pr.serving_pricing(vali) == p  # type: ignore[arg-type]

    # Once that last good reading is older than the max age it is not reused, and nothing is priced.
    monkeypatch.setattr(pr, '_last_usable', (time.time() - SERVING_PRICING_MAX_AGE_S - 1.0, p))
    assert pr.serving_pricing(vali) is None  # type: ignore[arg-type]
    monkeypatch.setattr(pr, '_last_usable', None)  # a validator that has never priced pays nothing
    assert pr.serving_pricing(vali) is None  # type: ignore[arg-type]


def test_ready_set_expires_after_ttl():
    from types import SimpleNamespace

    state = ServingState(ready_ttl_s=10.0, settlement_rounds=1)
    miner = ReadyMiner(uid=1, hotkey='hk1', axon=SimpleNamespace(), score=1.0, release_id='m')  # type: ignore[arg-type]
    state.publish_round([miner], {'hk1': 1.0})
    assert state.acquire('m') is miner
    state.last_round_ts -= 11.0  # the audit thread stopped publishing
    assert state.ready_miners() == [] and state.acquire('m') is None
    assert state.scores_for(['hk1']) == {0: 1.0}  # scores still blend; only routing stops


def test_oss_round_blends_latest_serving_scores(monkeypatch):
    """The OSS round reads the audit thread's scores by current hotkey instead of running audits itself."""
    import asyncio
    from types import SimpleNamespace

    from gittensor.validator import forward as top

    monkeypatch.setattr(top, 'SERVING_ENABLED', True)
    monkeypatch.setattr(top, 'VALIDATOR_STEPS_INTERVAL', 1)
    monkeypatch.setattr(top, 'VALIDATOR_WAIT', 0)
    monkeypatch.setattr(top, 'get_all_uids', lambda self: {1})
    monkeypatch.setattr(top, 'load_master_repo_weights', lambda: {})
    monkeypatch.setattr(top, 'load_programming_language_weights', lambda: {})
    monkeypatch.setattr(top, 'load_token_config', lambda: {})

    async def oss(self, *a):
        return {}, set(), set()

    async def issues(*a, **k):
        return None

    async def store(*a, **k):
        return None

    seen = {}

    def blend(evals, repos, uids, maintainers, serving_scores, pricing=None, allow_unpriced_cap=False):
        seen['serving'] = serving_scores
        seen['pricing'] = pricing
        return [0.0]

    monkeypatch.setattr(top, 'oss_contributions', oss)
    monkeypatch.setattr(top, 'issue_discovery', issues)
    monkeypatch.setattr(top, 'build_maintainer_uids_by_repo', lambda *a: {})
    monkeypatch.setattr(top, 'blend_emission_pools', blend)
    monkeypatch.setattr(top, 'serving_pricing', lambda self: 'priced')

    state = ServingState(settlement_rounds=1)
    state.publish_round([], {'hk1': 0.5, 'gone': 0.9})
    vali = SimpleNamespace(
        step=0,
        serving_state=state,
        metagraph=SimpleNamespace(hotkeys=['v', 'hk1']),
        evaluation_cache=None,
        bulk_store_evaluation=store,
        update_scores=lambda *a, **k: None,
    )
    asyncio.run(top.forward(vali))  # type: ignore[arg-type]
    assert seen['serving'] == {1: 0.5} and seen['pricing'] == 'priced'


def test_request_record_drops_non_finite_telemetry():
    rec = RequestRecord(ts=0.0, kind='gateway', uid=1, ok=True, latency_ms=float('inf'), ttft_ms=float('nan'))
    assert rec.latency_ms is None and rec.ttft_ms is None and rec.decode_tps is None
    assert RequestRecord(ts=0.0, kind='gateway', uid=1, ok=True, latency_ms=12.5).latency_ms == 12.5


def test_inference_synapse_hashes_request_fields():
    from gittensor.synapses import InferenceSynapse

    a = InferenceSynapse(messages=[{'role': 'user', 'content': 'x'}], model_id='m', max_tokens=8)
    b = InferenceSynapse(messages=[{'role': 'user', 'content': 'y'}], model_id='m', max_tokens=8)
    assert a.body_hash != b.body_hash
    assert a.body_hash == InferenceSynapse(messages=a.messages, model_id='m', max_tokens=8).body_hash


def test_serving_miner_blacklists_non_validators(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from gittensor.synapses import InferenceSynapse
    from neurons.serving_miner import blacklist_inference

    monkeypatch.setenv('SERVING_MIN_CALLER_STAKE', '100')
    monkeypatch.setenv('SERVING_VALIDATOR_TOKENS_PER_TEMPO', '150')
    miner = SimpleNamespace(
        metagraph=SimpleNamespace(
            hotkeys=['vali', 'builder', 'small', 'permitted'],
            S=[5000.0, 100.0, 99.0, 50.0],
            validator_permit=[True, False, False, True],
            block=720,
        ),
        audit_budget={},
        slot_count=99,
        slot_claims={},
    )

    def call(hotkey, max_tokens=64):
        syn = InferenceSynapse(messages=MSGS, model_id='m', max_tokens=max_tokens)
        assert syn.dendrite is not None
        syn.dendrite.hotkey = hotkey
        return asyncio.run(blacklist_inference(miner, syn))  # type: ignore[arg-type]

    assert call('vali') == (False, 'Staked caller')
    assert call('builder') == (False, 'Staked caller')
    assert call('small')[0] and 'Stake 99 below 100' in call('small')[1]
    assert call('stranger') == (True, 'Unrecognized hotkey')
    # a permit holder below the floor gets a per-tempo token budget, any request shape
    assert call('permitted', 100) == (False, 'Permitted validator')
    assert call('permitted', 50) == (False, 'Permitted validator')
    refused = call('permitted', 1)
    assert refused[0] and 'budget spent' in refused[1]
    miner.metagraph.block = 1080  # next tempo: budget resets
    assert call('permitted', 150) == (False, 'Permitted validator')
    assert call('permitted', 1)[0]


def test_probe_phase_offset_is_deterministic_and_spread():
    from gittensor.validator.serving.forward import probe_phase_offset

    a, b = probe_phase_offset('5Gjr7VuY', 300.0), probe_phase_offset('5E2LP6En', 300.0)
    assert a == probe_phase_offset('5Gjr7VuY', 300.0) and 0.0 <= a < 300.0 and 0.0 <= b < 300.0 and a != b


def test_dead_axons_go_dormant_and_stop_receiving_baseline_prompts(monkeypatch):
    """A hotkey that never returns a completion drops out of the report after N rounds and is only retried hourly."""
    import asyncio
    import random
    from types import SimpleNamespace

    from gittensor.constants import SERVING_DORMANT_AFTER_ROUNDS, SERVING_DORMANT_RETRY_ROUNDS
    from gittensor.validator.serving import forward as fwd

    good = _echo_release()
    live, dead = (SimpleNamespace(is_serving=True) for _ in range(2))
    dendrite, calls = _dendrite_echoing(good, dead_axons=(dead,))
    state = ServingState(settlement_rounds=1)
    serving = [(1, 'hk1', live), (2, 'hk2', dead)]

    def cycle() -> int:
        sent = asyncio.run(
            fwd.baseline_round(state, dendrite, serving, good, window_s=0.01, per_miner=1, rng=random.Random(1))  # type: ignore[arg-type]
        )
        for q in state.drain_served():
            state.enqueue_served(q)
        _round(state, dendrite, serving, good, monkeypatch)
        return sent

    for _ in range(SERVING_DORMANT_AFTER_ROUNDS - 1):
        assert cycle() == 2  # still asked while it earns its dormancy
        assert 2 in state.last_round['windows'] and state.snapshot()['probation_uids'] == [2]
    assert cycle() == 2  # asked once more; the audit that follows tips it over
    assert state.last_round['dormant'] == 1 and 2 not in state.last_round['windows']
    assert state.snapshot()['probation_uids'] == []
    dead_calls = calls[id(dead)]
    quiet = 0
    while cycle() == 1:  # dormant: only the live axon is asked...
        quiet += 1
        assert quiet < 3 * SERVING_DORMANT_RETRY_ROUNDS
    assert quiet == SERVING_DORMANT_RETRY_ROUNDS - SERVING_DORMANT_AFTER_ROUNDS  # ...until the hourly retry
    assert calls[id(dead)] == dead_calls + 1
    assert state.last_round['dormant'] == 1  # still dormant after an unanswered retry


def test_axon_that_answers_without_a_completion_gets_a_real_reason(monkeypatch):
    """An OSS/validator axon says "Success" and serves nothing; the miss reason must say so, not "Success"."""
    import asyncio
    import random
    from types import SimpleNamespace
    from typing import Any

    from gittensor.validator.serving import forward as fwd

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)

    async def consume(_dendrite, _axon, synapse, _timeout, **_kw):
        synapse.dendrite = bt.TerminalInfo(status_message='Success')
        return synapse  # unfilled: no completion, no served_model_id

    monkeypatch.setattr(fwd, 'consume_stream', consume)
    state = ServingState(settlement_rounds=1)
    serving: list = [(1, 'hk1', axon)]
    dendrite: Any = object()
    asyncio.run(fwd.baseline_round(state, dendrite, serving, good, window_s=0.01, per_miner=1, rng=random.Random(1)))
    (q,) = state.drain_served()
    assert not q.ok and q.detail.startswith('no completion: axon answered "Success"') and good.model_id in q.detail


def _attest_release(reference: str = 'http://ref:8081') -> ServingRelease:
    release = _echo_release()
    release.attest_reference_url = reference
    release.vram_model_reserved_bytes = 24e9
    return release


def _attest_reply(
    digest: str = 'd', wall_ms: float = 1500.0, filled: int = 8_000_000_000, uuid: str = 'GPU-a', queued_ms=0.0
):
    from gittensor.synapses import AttestSynapse

    return AttestSynapse(
        seed=1,
        devices=[{'uuid': uuid, 'digest': digest, 'wall_ms': wall_ms, 'filled_bytes': filled, 'vram_total': 34e9}],
        wall_ms=wall_ms,
        queued_ms=queued_ms,
    )


def test_attest_verdicts():
    from gittensor.validator.serving.attest import judge

    release = _attest_release()
    assert judge(_attest_reply(), 'd', 1400.0, release).passed
    assert judge(None, 'd', 1400.0, release).reason.startswith('no attestation')
    assert judge(_attest_reply(digest='x'), 'd', 1400.0, release).reason == 'digest mismatch'
    slow = judge(_attest_reply(wall_ms=1500.0, queued_ms=1500.0), 'd', 1400.0, release)  # queued behind another
    assert not slow.passed and slow.reason.startswith('too slow')
    assert judge(_attest_reply(filled=2_000_000_000), 'd', 1400.0, release).reason.startswith('under-filled')
    from gittensor.synapses import AttestSynapse

    assert judge(AttestSynapse(seed=1, error='sidecar down'), 'd', 1400.0, release).reason.startswith('no attestation')


def test_attest_cohort_is_random_half_plus_unproven_and_failed():
    import random

    from gittensor.validator.serving.attest import choose_cohort

    hotkeys = [f'hk{i}' for i in range(20)]
    status = {hk: {'passed': True} for hk in hotkeys}
    status['hk3'] = {'passed': False}
    del status['hk7']  # never attested
    rng = random.Random(1)
    cohort = choose_cohort(hotkeys, status, rng=rng)
    assert 'hk3' in cohort and 'hk7' in cohort and 10 <= len(cohort) <= 12
    # least recently challenged first: simulate rounds, every hotkey is challenged at least every 2 rounds
    last = {hk: 0 for hk in hotkeys}
    for rnd in range(1, 21):
        for hk in choose_cohort(hotkeys, {hk: {'passed': True, 'round': last[hk]} for hk in hotkeys}, rng=rng):
            last[hk] = rnd
        assert all(rnd - r <= 2 for r in last.values())
    # a hotkey that was just challenged is not drawn again while others wait (ties broken at random)
    fresh = {hk: {'passed': True, 'round': 5} for hk in hotkeys}
    fresh['hk0']['round'] = 9
    assert 'hk0' not in choose_cohort(hotkeys, fresh, rng=rng)


def test_attest_round_gates_pay_and_persists(monkeypatch, tmp_path):
    import asyncio

    """No verdict -> probation; a failing cohort member is not admitted; a passing one is; non-cohort members keep
    their last verdict; two hotkeys behind one card are both admitted (they split its tokens); status survives
    the store."""
    import random
    from types import SimpleNamespace

    from gittensor.serving.store import ServingStore
    from gittensor.validator.serving import attest as att

    release = _attest_release()
    monkeypatch.setattr(att, 'reference_challenge', lambda rel, seed, iters, timeout: ('d', 1400.0))
    replies = {}

    async def call(target_axon, synapse, timeout, deserialize):
        return replies[id(target_axon)]

    axons = {hk: SimpleNamespace(is_serving=True) for hk in ('hk1', 'hk2', 'hk3')}
    replies[id(axons['hk1'])] = _attest_reply(uuid='GPU-1')
    replies[id(axons['hk2'])] = _attest_reply(uuid='GPU-2', digest='wrong')
    replies[id(axons['hk3'])] = _attest_reply(uuid='GPU-1')  # same card as hk1
    dendrite = SimpleNamespace(call=call)
    state = ServingState()
    candidates = [(1, 'hk1', axons['hk1']), (2, 'hk2', axons['hk2']), (3, 'hk3', axons['hk3'])]
    out = asyncio.run(att.attest_round(state, dendrite, candidates, release, rng=random.Random(0)))  # type: ignore[arg-type]
    assert out == {'hk1': True, 'hk2': False, 'hk3': True}  # hk2 wrong digest; nothing counts cards
    assert state.attest_status['hk2']['reason'] == 'digest mismatch'
    assert state.attest_status['hk1']['reason'] == 'ok' and 'capacity' not in state.attest_status['hk1']

    replies[id(axons['hk2'])] = _attest_reply(uuid='GPU-2')
    out = asyncio.run(att.attest_round(state, dendrite, candidates, release, rng=random.Random(0)))  # type: ignore[arg-type]
    assert out == {'hk1': True, 'hk2': True, 'hk3': True}  # the failure was re-challenged; the others carried
    store = ServingStore(tmp_path / 'serving.db')
    store.save(state)
    again = store.load(ServingState())
    assert again.attest_status['hk1']['uuid'] == 'GPU-1' and again.attest_status['hk2']['passed'] is True

    monkeypatch.setattr(att, 'reference_challenge', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('ref down')))
    replies[id(axons['hk2'])] = _attest_reply(uuid='GPU-2', digest='wrong')
    neutral = asyncio.run(att.attest_round(state, dendrite, candidates, release))  # type: ignore[arg-type]
    assert neutral == {'hk1': True, 'hk2': True, 'hk3': True}  # reference down: nothing changes


def test_audit_round_requires_attestation_when_a_reference_is_configured(monkeypatch):
    from types import SimpleNamespace

    from gittensor.validator.serving import attest as att

    release = _attest_release()
    monkeypatch.setattr(att, 'reference_challenge', lambda rel, seed, iters, timeout: ('d', 1400.0))
    good, bad = SimpleNamespace(is_serving=True), SimpleNamespace(is_serving=True)
    replies = {id(good): _attest_reply(uuid='GPU-g'), id(bad): _attest_reply(uuid='GPU-b', wall_ms=9_000.0)}
    dendrite, _ = _dendrite_echoing(release)

    async def call(target_axon, synapse, timeout, deserialize):
        return replies[id(target_axon)]

    dendrite.call = call
    state = ServingState(settlement_rounds=1)
    for uid in (1, 2):
        state.enqueue_served(_served(uid, release))
    scores = _round(state, dendrite, [(1, 'hk1', good), (2, 'hk2', bad)], release, monkeypatch)
    assert scores == {'hk1': pytest.approx(_pay(8, release)), 'hk2': 0.0}
    snap = state.snapshot()
    assert snap['ready_uids'] == [1] and snap['probation_uids'] == [2]  # failed attest: not READY, not struck
    w = state.audits.verdict('hk2', release.model_id)
    assert w.passed and w.quarantined_until == 0.0
    tele = state.last_round['windows']
    assert tele[1]['attested'] and tele[1]['gpu_uuid'] == 'GPU-g' and not tele[2]['attested']
    assert tele[2]['attest_reason'].startswith('too slow')


def test_sidecar_url_derives_from_the_runtime_url():
    from gittensor.serving.loadout import _sidecar_url

    assert _sidecar_url('http://82.76.142.91:45565') == 'http://82.76.142.91:8081'
    assert _sidecar_url('http://reference:8080/') == 'http://reference:8081'
    assert _sidecar_url(None) is None


def test_miner_axon_hooks_match_their_forward_synapse_types():
    """bt.Axon.attach asserts blacklist/priority/verify signatures against the forward's synapse annotation."""
    import inspect
    from functools import partial

    from neurons import serving_miner as sm

    for fwd, bl, pr, vf, syn in (
        (sm.handle_inference, sm.blacklist_inference, sm.priority_inference, sm.verify_inference, 'InferenceSynapse'),
        (sm.handle_attest, sm.blacklist_attest, sm.priority_attest, sm.verify_attest, 'AttestSynapse'),
    ):
        for fn in (fwd, bl, pr, vf):
            (param,) = inspect.signature(partial(fn, None)).parameters.values()
            assert param.name == 'synapse' and getattr(param.annotation, '__name__', param.annotation) == syn


def test_attest_is_for_validating_hotkeys_one_challenge_at_a_time(monkeypatch):
    """A permit alone opened a free, unlimited, VRAM-filling call on every miner; now it takes validator_trust > 0
    (or the stake floor), and one challenge per caller at a time. The miner sends its sidecar's bearer."""
    import asyncio
    from types import SimpleNamespace

    import requests

    from gittensor.synapses import AttestSynapse
    from neurons.serving_miner import blacklist_attest, handle_attest

    monkeypatch.setenv('SERVING_MIN_CALLER_STAKE', '100')
    miner = SimpleNamespace(
        metagraph=SimpleNamespace(
            hotkeys=['vali', 'staked', 'permitted'],
            S=[5.0, 100.0, 50.0],
            validator_permit=[True, True, True],
            validator_trust=[0.9, 0.0, 0.0],
            block=720,
        ),
        audit_budget={},
        attest_inflight=set(),
        release=SimpleNamespace(attest_url='http://sidecar:8081', attest_api_key='sekrit'),
    )

    def gate(hotkey):
        syn = AttestSynapse(seed=1)
        assert syn.dendrite is not None
        syn.dendrite.hotkey = hotkey
        return asyncio.run(blacklist_attest(miner, syn))  # type: ignore[arg-type]

    assert gate('vali') == (False, 'Permitted validator')
    assert gate('staked') == (False, 'Staked caller')
    assert gate('permitted') == (True, 'Attestation is for validating hotkeys')
    assert gate('nobody') == (True, 'Unrecognized hotkey')
    assert all(used == 0 for _, used in miner.audit_budget.values())  # nothing charged
    miner.attest_inflight.add('vali')
    assert gate('vali') == (True, 'Attestation already in flight for this caller')
    miner.attest_inflight.clear()

    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, headers=headers, inflight=set(miner.attest_inflight))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'devices': [{'uuid': 'g'}], 'queued_ms': 1})

    monkeypatch.setattr(requests, 'post', post)
    syn = AttestSynapse(seed=1)
    assert syn.dendrite is not None
    syn.dendrite.hotkey = 'vali'
    out = asyncio.run(handle_attest(miner, syn))  # type: ignore[arg-type]
    assert out.devices == [{'uuid': 'g'}] and seen['headers'] == {'Authorization': 'Bearer sekrit'}
    assert seen['inflight'] == {'vali'} and miner.attest_inflight == set()


def _card(uuid: str, digest: str = 'd', wall_ms: float = 1500.0, filled: int = 8_000_000_000, free_before=None):
    dev = {'uuid': uuid, 'digest': digest, 'wall_ms': wall_ms, 'filled_bytes': filled, 'vram_total': 34e9}
    if free_before is not None:
        dev['vram_free_before'] = free_before
    return dev


def test_attest_judges_every_card_and_admits_on_any():
    """One hotkey, N cards: each card is judged alone; the hotkey passes with any passing card; the reason names the
    first failure; a card without the model resident is not a serving card."""
    from gittensor.synapses import AttestSynapse
    from gittensor.validator.serving.attest import judge

    release = _attest_release()
    two = judge(AttestSynapse(seed=1, devices=[_card('GPU-a'), _card('GPU-b')]), 'd', 1400.0, release)
    assert two.passed and two.uuids == ['GPU-a', 'GPU-b'] and two.reason == 'ok (2 cards)'
    one_bad = judge(
        AttestSynapse(seed=1, devices=[_card('GPU-a'), _card('GPU-b', wall_ms=9_000.0)]), 'd', 1400.0, release
    )
    assert one_bad.passed and one_bad.uuids == ['GPU-a']
    assert one_bad.reason.startswith('1/2 cards ok (too slow')
    none = judge(
        AttestSynapse(seed=1, devices=[_card('GPU-a', digest='x'), _card('GPU-b', digest='x')]), 'd', 1400.0, release
    )
    assert not none.passed and none.uuids == [] and none.reason == 'digest mismatch'
    loaded = judge(AttestSynapse(seed=1, devices=[_card('GPU-a', free_before=9e9)]), 'd', 1400.0, release)
    assert loaded.passed
    bare = judge(AttestSynapse(seed=1, devices=[_card('GPU-a', free_before=33e9)]), 'd', 1400.0, release)
    assert not bare.passed and bare.reason.startswith('model not resident')
    status = two.as_status(0.0, 1)
    assert status['uuids'] == ['GPU-a', 'GPU-b'] and len(status['cards']) == 2 and 'capacity' not in status


def test_pay_follows_served_tokens_not_attested_cards(monkeypatch):
    """A two-card hotkey and a one-card hotkey that served the same tokens are paid the same; the card count is
    telemetry. Attestation admits, and a failed attest zeroes the round's pay however much was served."""
    import asyncio
    import random
    from types import SimpleNamespace

    from gittensor.synapses import AttestSynapse
    from gittensor.validator.serving import attest as att
    from gittensor.validator.serving import forward as fwd

    release = _attest_release()
    monkeypatch.setattr(att, 'reference_challenge', lambda rel, seed, iters, timeout: ('d', 1400.0))
    replies = {}

    async def call(target_axon, synapse, timeout, deserialize):
        return replies[id(target_axon)]

    axons = {hk: SimpleNamespace(is_serving=True) for hk in ('hk1', 'hk2')}
    replies[id(axons['hk1'])] = AttestSynapse(seed=1, devices=[_card('GPU-a'), _card('GPU-b')])
    replies[id(axons['hk2'])] = AttestSynapse(seed=1, devices=[_card('GPU-c')])
    dendrite = SimpleNamespace(call=call)
    state = ServingState()
    candidates = [(1, 'hk1', axons['hk1']), (2, 'hk2', axons['hk2'])]
    out = asyncio.run(att.attest_round(state, dendrite, candidates, release, rng=random.Random(0)))  # type: ignore[arg-type]
    assert out == {'hk1': True, 'hk2': True}
    assert att.status_passed(state.attest_status['hk1']) and state.attest_status['hk1']['uuids'] == ['GPU-a', 'GPU-b']
    assert att.status_passed({'passed': True, 'uuid': 'GPU-old'})  # a status persisted before this change

    # the audit round pays the tokens the gateway saw served; the cards behind a hotkey do not enter
    good = _echo_release()
    good.attest_reference_url = 'http://ref:8081'
    good.vram_model_reserved_bytes = 24e9
    state = ServingState(settlement_rounds=1)
    for _ in range(3):
        state.enqueue_served(_served(1, good))
        state.enqueue_served(_served(2, good))
    replies[id(axons['hk1'])] = AttestSynapse(seed=1, devices=[_card('GPU-a'), _card('GPU-b')])
    replies[id(axons['hk2'])] = AttestSynapse(seed=1, devices=[_card('GPU-c')])
    echo_dendrite, _ = _dendrite_echoing(good)
    echo_dendrite.call = call  # type: ignore[attr-defined]
    monkeypatch.setattr(
        fwd, 'reference_for', lambda rel: __import__('gittensor.serving.audit', fromlist=['x']).reference_for(rel)
    )
    scores = asyncio.run(
        fwd.audit_round(
            state,
            echo_dendrite,  # type: ignore[arg-type]
            [(1, 'hk1', axons['hk1']), (2, 'hk2', axons['hk2'])],  # type: ignore[arg-type]
            loadout=SimpleNamespace(releases=[good]),
            attest_rng=random.Random(0),
        )
    )
    assert scores['hk1'] == scores['hk2'] == pytest.approx(_pay(3 * 8, good))
    tele = state.snapshot()['last_round']['windows']
    assert tele[1]['gpu_uuids'] == ['GPU-a', 'GPU-b'] and tele[2]['gpu_uuids'] == ['GPU-c']
    assert tele[1]['tokens'] == tele[2]['tokens'] == 24 and tele[1]['attested'] and tele[2]['attested']

    # a hotkey that fails attestation is paid nothing for the round, whatever it served, and is not READY
    for _ in range(3):
        state.enqueue_served(_served(1, good))
        state.enqueue_served(_served(2, good))
    replies[id(axons['hk2'])] = AttestSynapse(seed=1, devices=[_card('GPU-c', digest='x')])
    state.attest_status = {}
    scores = asyncio.run(
        fwd.audit_round(
            state,
            echo_dendrite,  # type: ignore[arg-type]
            [(1, 'hk1', axons['hk1']), (2, 'hk2', axons['hk2'])],  # type: ignore[arg-type]
            loadout=SimpleNamespace(releases=[good]),
            attest_rng=random.Random(0),
            round_s=ROUND_S,
        )
    )
    assert scores == {'hk1': pytest.approx(_pay(24, good)), 'hk2': 0.0}
    assert state.snapshot()['ready_uids'] == [1] and state.snapshot()['probation_uids'] == [2]
    tele = state.snapshot()['last_round']['windows']
    assert tele[2]['tokens'] == 24 and not tele[2]['attested'] and tele[2]['status'] == 'probation'


def test_attest_cards_answer_their_own_index_and_the_round_trip_bounds_the_count():
    """Device i answers seed + i, recomputed by the reference: one real digest repeated N times passes once. And the
    whole reply must land within one card's budget plus the slack, however each card's own wall reads."""
    from gittensor.synapses import AttestSynapse
    from gittensor.validator.serving.attest import judge

    release = _attest_release()
    per_index = {0: 'd0', 1: 'd1', 2: 'd2'}.get
    faked = judge(
        AttestSynapse(seed=1, devices=[_card('GPU-a', digest='d0'), _card('GPU-b', digest='d0')]),
        per_index,
        1400.0,
        release,
    )
    assert faked.uuids == ['GPU-a'] and faked.reason.startswith('1/2 cards ok (digest mismatch')
    honest = judge(
        AttestSynapse(seed=1, devices=[_card('GPU-a', digest='d0'), _card('GPU-b', digest='d1')]),
        per_index,
        1400.0,
        release,
        elapsed_ms=1500.0,
    )
    assert honest.uuids == ['GPU-a', 'GPU-b']
    beyond = judge(
        AttestSynapse(seed=1, devices=[_card(f'GPU-{i}', digest=f'd{i}') for i in range(4)]), per_index, 1400.0, release
    )
    assert len(beyond.uuids) == 3 and 'no reference digest' in beyond.reason
    slow = judge(
        AttestSynapse(seed=1, devices=[_card('GPU-a', digest='d0')]), per_index, 1400.0, release, elapsed_ms=9_000.0
    )
    assert not slow.passed and slow.uuids == [] and 'round trip' in slow.reason
    capped = judge(
        AttestSynapse(seed=1, devices=[_card(f'GPU-{i}', digest='d') for i in range(20)]),
        'd',
        1400.0,
        release,
        max_cards=3,
    )
    assert len(capped.cards) == 3


def test_attest_malformed_report_is_a_failed_card_not_a_crash():
    from gittensor.synapses import AttestSynapse
    from gittensor.validator.serving.attest import judge, status_passed

    release = _attest_release()
    bad = judge(
        AttestSynapse(seed=1, devices=[{'uuid': 'GPU-a', 'digest': 'd', 'filled_bytes': 'x'}]), 'd', 1400.0, release
    )
    assert not bad.passed and bad.reason.startswith('malformed device report')
    nested = judge(
        AttestSynapse(seed=1, devices=[{'uuid': 'GPU-a', 'digest': 'd', 'vram_total': [1]}]), 'd', 1400.0, release
    )
    assert not nested.passed and nested.reason.startswith('malformed device report')
    # a verdict the reference has not renewed for longer than the memory window admits nothing
    stale = {'passed': True, 'round': 1}
    assert status_passed(stale, round_no=13) and not status_passed(stale, round_no=14)
    assert status_passed(stale) and not status_passed({'passed': False, 'round': 13}, round_no=13)


def test_attest_fault_is_neutral_and_the_round_counter_persists(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from gittensor.serving.store import ServingStore
    from gittensor.validator.serving import attest as att

    release = _attest_release()
    monkeypatch.setattr(att, 'reference_challenge', lambda rel, seed, iters, timeout: ('d', 1400.0))
    axon = SimpleNamespace(is_serving=True)

    async def call(target_axon, synapse, timeout, deserialize):
        return _attest_reply(uuid='GPU-1')

    dendrite = SimpleNamespace(call=call)
    state = ServingState()
    assert asyncio.run(att.attest_round(state, dendrite, [(1, 'hk1', axon)], release)) == {'hk1': True}  # type: ignore[arg-type]
    monkeypatch.setattr(att, 'judge', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    assert asyncio.run(att.attest_round(state, dendrite, [(1, 'hk1', axon)], release)) == {'hk1': True}  # type: ignore[arg-type]
    assert state.attest_round == 2
    store = ServingStore(tmp_path / 'serving.db')
    store.save(state)
    again = store.load(ServingState())
    assert again.attest_round == 2 and again.attest_status['hk1']['uuid'] == 'GPU-1'


def test_reference_challenge_sends_the_bearer(monkeypatch):
    import requests

    from gittensor.validator.serving import attest as att

    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'digest': 'd', 'wall_ms': 800.0})

    monkeypatch.setattr(requests, 'post', post)
    release = _attest_release()
    release.attest_reference_api_key = 'secret'
    assert att.reference_challenge(release, 5, 3, 10.0) == ('d', 800.0)
    assert seen['headers'] == {'Authorization': 'Bearer secret'} and seen['json']['seed'] == 5


def test_sample_for_audit_keeps_baseline_and_failures_and_draws_the_rest():
    """Per hotkey: baseline prompts and failed requests are always verified; completed gateway requests are drawn at
    max(minimum, fraction x n); a low-traffic miner is verified in full."""
    import random

    from gittensor.validator.serving.forward import sample_for_audit

    def req(hk, source='gateway', ok=True):
        return ServedRequest(ts=0.0, uid=1, hotkey=hk, model_id='m', messages=[], ok=ok, latency_ms=1.0, source=source)

    served = (
        [req('busy') for _ in range(100)]
        + [req('busy', source='baseline') for _ in range(2)]
        + [req('busy', ok=False) for _ in range(3)]
        + [req('quiet') for _ in range(7)]
    )
    keep, skipped = sample_for_audit(served, fraction=0.2, minimum=10, rng=random.Random(0))
    busy = [r for r in keep if r.hotkey == 'busy']
    assert sum(1 for r in busy if r.source == 'gateway' and r.ok) == 20
    assert sum(1 for r in busy if r.source == 'baseline') == 2 and sum(1 for r in busy if not r.ok) == 3
    assert sum(1 for r in keep if r.hotkey == 'quiet') == 7
    assert len(skipped) == 80 and all(r.hotkey == 'busy' and r.ok and r.source == 'gateway' for r in skipped)
    kept = {id(r) for r in keep}
    assert [id(r) for r in keep] == [id(r) for r in served if id(r) in kept]  # input order kept
    tiny = [req('busy') for _ in range(50)]
    assert len(sample_for_audit(tiny, fraction=0.2, minimum=10, rng=random.Random(1))[0]) == 10


def test_release_id_defaults_to_model_id_and_keys_the_loadout():
    bare = ServingRelease(model_id='qwen', backend='echo')
    assert bare.release_id == 'qwen'
    assert (bare.min_prefix_agreement, bare.max_mean_abs_logprob_diff, bare.max_abs_logprob_diff) == (
        SERVING_AUDIT_MIN_PREFIX_AGREEMENT,
        SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF,
        SERVING_AUDIT_MAX_ABS_LOGPROB_DIFF,
    )
    tuned = ServingRelease.from_dict(
        {
            'model_id': 'qwen',
            'backend': 'echo',
            'release_id': 'qwen-sparkinfer-abc',
            'audit': {'min_prefix_agreement': 0.9, 'max_abs_logprob_diff': 0.4},
        }
    )
    assert tuned.release_id == 'qwen-sparkinfer-abc'
    assert (tuned.min_prefix_agreement, tuned.max_abs_logprob_diff) == (0.9, 0.4)
    assert tuned.max_mean_abs_logprob_diff == SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF  # unset -> the constant
    lo = ServingLoadout(releases=[tuned, bare])
    assert lo.get('qwen-sparkinfer-abc') is tuned and lo.get('qwen') is bare
    with pytest.raises(KeyError):
        lo.get('nope')
    with pytest.raises(ValueError):  # two runtimes of one model are distinct releases, one id each
        ServingLoadout(releases=[tuned, ServingRelease(model_id='x', backend='echo', release_id='qwen-sparkinfer-abc')])


def test_two_releases_of_one_model_keep_separate_windows_and_pools():
    fast = ServingRelease(model_id='qwen', backend='echo', release_id='qwen-fast')
    slow = ServingRelease(model_id='qwen', backend='echo', release_id='qwen-slow')
    w = AuditWindow(size=2, thresholds=((1, 0.5),))
    w.record('hk', fast.release_id, 1.0)
    w.record('hk', slow.release_id, 0.0)
    assert w.verdict('hk', fast.release_id).passed and not w.verdict('hk', slow.release_id).passed

    state = ServingState()
    state.publish_round(
        [
            ReadyMiner(uid=1, hotkey='hk1', axon=None, score=1.0, release_id='qwen-fast'),  # type: ignore[arg-type]
            ReadyMiner(uid=2, hotkey='hk2', axon=None, score=1.0, release_id='qwen-slow'),  # type: ignore[arg-type]
        ],
        {},
    )
    fast_miner, slow_miner = state.acquire('qwen-fast'), state.acquire('qwen-slow')
    assert fast_miner is not None and fast_miner.uid == 1
    assert slow_miner is not None and slow_miner.uid == 2


def test_gateway_routes_by_requested_model(monkeypatch):
    other = ServingRelease(model_id='other-v0', backend='echo', release_id='other-v0', max_tokens=8)
    state = ServingState()
    state.publish_round(
        [
            _ready(7),  # echo-v0
            ReadyMiner(uid=8, hotkey='hk8', axon=None, score=1.0, release_id='other-v0'),  # type: ignore[arg-type]
        ],
        {},
    )
    client = _gateway_client(state, monkeypatch, releases=[_echo_release(), other])
    listed = client.get('/v1/models', headers={'Authorization': 'Bearer k1'}).json()['data']
    assert [m['id'] for m in listed] == ['echo-v0', 'other-v0']

    def ask(model=None):
        body = {'messages': MSGS, 'max_tokens': 2}
        if model is not None:
            body['model'] = model
        return client.post('/v1/chat/completions', json=body, headers={'Authorization': 'Bearer k1'})

    assert ask().json()['gittensor']['served_uid'] == 7  # no model -> the primary release
    assert ask('other-v0').json()['gittensor']['served_uid'] == 8
    assert ask('nope').status_code == 404
    served = state.drain_served()
    assert {r.release_id for r in served} == {'echo-v0', 'other-v0'}


def test_miner_refuses_a_request_routed_for_another_release():
    from neurons.serving_miner import blacklist_inference

    miner = SimpleNamespace(
        release=ServingRelease(model_id='qwen', backend='echo', release_id='qwen-fast'),
        metagraph=SimpleNamespace(hotkeys=['vali'], S=[2_000_000.0]),
        slot_count=99,
        slot_claims={},
    )
    syn = InferenceSynapse(messages=MSGS, model_id='qwen', release_id='qwen-slow', max_tokens=4)
    syn.dendrite = bt.TerminalInfo(hotkey='vali')
    blocked, reason = asyncio.run(blacklist_inference(miner, syn))  # type: ignore[arg-type]
    assert blocked and 'qwen-slow' in reason
    syn.release_id = 'qwen-fast'
    assert not asyncio.run(blacklist_inference(miner, syn))[0]  # type: ignore[arg-type]
    syn.release_id = ''  # a caller that does not name a release is served as before
    assert not asyncio.run(blacklist_inference(miner, syn))[0]  # type: ignore[arg-type]


def test_miner_refuses_busy_before_charging_budget_and_attest_bypasses_it():
    """Every backend slot taken -> "busy" up front (R6 at the axon), before the budget charge; a freed slot serves
    and charges as before. Attestation skips the gate: a full card must still pass attest rounds."""
    from gittensor.synapses import AttestSynapse
    from neurons.serving_miner import blacklist_attest, blacklist_inference

    miner = SimpleNamespace(
        release=ServingRelease(model_id='qwen', backend='echo', release_id='qwen-fast'),
        metagraph=SimpleNamespace(
            hotkeys=['auditor'], S=[100.0], validator_permit=[True], validator_trust=[1.0], block=720
        ),
        slot_count=0,
        slot_claims={},
        audit_budget={},
        attest_inflight=set(),
    )
    syn = InferenceSynapse(messages=MSGS, model_id='qwen', max_tokens=4)
    syn.dendrite = bt.TerminalInfo(hotkey='auditor')
    blocked, reason = asyncio.run(blacklist_inference(miner, syn))  # type: ignore[arg-type]
    assert blocked and 'busy' in reason
    assert miner.audit_budget == {}
    att = AttestSynapse(seed=1)
    att.dendrite = bt.TerminalInfo(hotkey='auditor')
    assert not asyncio.run(blacklist_attest(miner, att))[0]  # type: ignore[arg-type]
    miner.slot_count = 1
    assert not asyncio.run(blacklist_inference(miner, syn))[0]  # type: ignore[arg-type]
    assert miner.audit_budget['auditor'][1] == 4
    assert len(miner.slot_claims) == 1  # the admitted request holds its slot from the moment of admission


def test_burst_admissions_never_exceed_slots():
    """40 simultaneous admission checks against 16 slots admit exactly 16 — the race #1743 shipped with: a gate
    that samples slots already handed to running streams admits an entire burst before any stream has begun."""
    from neurons.serving_miner import blacklist_inference

    miner = SimpleNamespace(
        release=ServingRelease(model_id='qwen', backend='echo', release_id='qwen-fast'),
        metagraph=SimpleNamespace(hotkeys=['vali'], S=[2_000_000.0]),
        slot_count=16,
        slot_claims={},
    )

    async def burst():
        syns = []
        for _ in range(40):
            syn = InferenceSynapse(messages=MSGS, model_id='qwen', max_tokens=4)
            syn.dendrite = bt.TerminalInfo(hotkey='vali')
            syns.append(syn)
        return syns, await asyncio.gather(*(blacklist_inference(miner, s) for s in syns))  # type: ignore[arg-type]

    syns, verdicts = asyncio.run(burst())
    admitted = [v for v in verdicts if not v[0]]
    refused = [v for v in verdicts if v[0]]
    assert len(admitted) == 16 and len(refused) == 24
    assert all('busy' in reason for _, reason in refused)
    assert len(miner.slot_claims) == 16
    del syns


def test_slot_claims_expire_and_a_refused_caller_releases_one():
    from neurons.serving_miner import blacklist_inference

    miner = SimpleNamespace(
        release=ServingRelease(model_id='qwen', backend='echo', release_id='qwen-fast'),
        metagraph=SimpleNamespace(hotkeys=['vali'], S=[2_000_000.0]),
        slot_count=1,
        slot_claims={},
    )
    syn = InferenceSynapse(messages=MSGS, model_id='qwen', max_tokens=4)
    syn.dendrite = bt.TerminalInfo(hotkey='stranger')
    assert asyncio.run(blacklist_inference(miner, syn)) == (True, 'Unrecognized hotkey')  # type: ignore[arg-type]
    assert miner.slot_claims == {}  # a caller-gate refusal does not hold the slot it briefly claimed

    syn.dendrite = bt.TerminalInfo(hotkey='vali')
    assert not asyncio.run(blacklist_inference(miner, syn))[0]  # type: ignore[arg-type]
    assert len(miner.slot_claims) == 1
    other = InferenceSynapse(messages=MSGS, model_id='qwen', max_tokens=4)
    other.dendrite = bt.TerminalInfo(hotkey='vali')
    blocked, reason = asyncio.run(blacklist_inference(miner, other))  # type: ignore[arg-type]
    assert blocked and 'busy' in reason
    miner.slot_claims[next(iter(miner.slot_claims))] = 0.0  # the handler never ran; the claim ages out
    assert not asyncio.run(blacklist_inference(miner, other))[0]  # type: ignore[arg-type]


def test_strikes_count_up_and_survive_a_restart(tmp_path):
    from gittensor.serving.store import ServingStore

    w = AuditWindow(quarantine_s=100.0)
    w.record('hk', 'r1', 1.0)
    assert w.verdict('hk', 'r1').strikes == 0
    w.strike('hk', 'r1', now=1000.0)
    w.strike('hk', 'r1', now=2000.0)
    w.strike('hk', 'r2', now=1000.0)
    now = 3000.0
    assert w.strikes('hk', 'r1', now) == 2 and w.strikes('hk', 'r2', now) == 1 and w.strikes('hk2', 'r1', now) == 0
    assert w.verdict('hk', 'r1', now=3000.0).as_dict()['strikes'] == 2

    store = ServingStore(tmp_path / 'serving.db')
    store.save(ServingState(audits=w))
    again = store.load(ServingState(audits=AuditWindow(quarantine_s=100.0))).audits
    assert again.strikes('hk', 'r1', now) == 2 and again.strikes('hk', 'r2', now) == 1
    assert again.quarantined_until('hk', 'r1', now=2050.0) == 2400.0  # the second strike: 4x the first
    assert again._last_strike[('hk', 'r1')] == 2000.0  # the forget clock survives the restart too
    assert again.strikes('hk', 'r1') == 0  # ...so by wall-clock time (1970 + 2000 s) they are long forgotten


def test_store_adds_the_last_strike_column_to_an_older_database(tmp_path):
    import sqlite3

    from gittensor.serving.store import ServingStore

    path = tmp_path / 'serving.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE quarantine (hotkey TEXT, release_id TEXT, until REAL, strikes INTEGER DEFAULT 0)')
        db.execute('CREATE TABLE audit_values (hotkey TEXT, release_id TEXT, seq INTEGER, value REAL)')
        db.execute('INSERT INTO quarantine VALUES (?, ?, ?, ?)', ('hk', 'r', 0.0, 3))
    loaded = ServingStore(path).load(ServingState(audits=AuditWindow(quarantine_s=100.0))).audits
    assert loaded._strikes[('hk', 'r')] == 3 and loaded._last_strike[('hk', 'r')] == 0.0
    assert loaded.strikes('hk', 'r') == 0  # a pre-upgrade strike carries no clock: forgotten on first look


def test_store_migrates_a_model_id_keyed_database(tmp_path):
    import sqlite3

    from gittensor.serving.store import ServingStore

    path = tmp_path / 'serving.db'
    with sqlite3.connect(path) as db:  # the pre-release_id schema
        db.execute('CREATE TABLE audit_values (hotkey TEXT, model_id TEXT, seq INTEGER, value REAL)')
        db.execute('CREATE TABLE quarantine (hotkey TEXT, model_id TEXT, until REAL)')
        db.execute("INSERT INTO audit_values VALUES ('hk', 'qwen', 0, 1.0)")
        db.execute("INSERT INTO quarantine VALUES ('hk2', 'qwen', 9e12)")
    loaded = ServingStore(path).load(ServingState())
    assert loaded.audits.verdict('hk', 'qwen').n_audits == 1  # rows carry over keyed by the old model_id
    assert loaded.audits.quarantined_until('hk2', 'qwen') > 0 and loaded.audits.strikes('hk2', 'qwen') == 0


def test_release_audit_bands_override_the_constants():
    case = AuditCase(
        messages=MSGS,
        max_tokens=4,
        reference_tokens=['a', 'b', 'c', 'd'],
        reference_logprobs=[-0.1, -0.2, -0.3, -0.4],
    )
    drifted = [x - 0.05 for x in case.reference_logprobs]  # past the default mean band (0.005)
    assert not verify_response(case, case.reference_tokens, drifted).passed
    loose = ServingRelease(
        model_id='qwen',
        backend='echo',
        audit_max_mean_abs_logprob_diff=0.1,
        audit_max_abs_logprob_diff=0.2,
    )
    assert verify_response(
        case,
        case.reference_tokens,
        drifted,
        loose.min_prefix_agreement,
        loose.max_mean_abs_logprob_diff,
        loose.max_abs_logprob_diff,
    ).passed


def test_completion_is_rebuilt_from_token_bytes_when_a_character_is_split():
    """A multibyte character split across two tokens reaches the deltas as U+FFFD; the bytes still spell it.

    Shape taken from a live 5090 serving the blessed pin: sparkinfer split U+2308 across two tokens, so the
    caller received '**�⌈m/2�⌉**' for '**⌈m/2⌉**'.
    """
    from gittensor.serving.stream import StreamAssembler

    ceiling, close = '⌈'.encode(), '⌉'.encode()  # 3 bytes each
    pieces = [b'**', ceiling[:1], ceiling[1:], b'm/2', close[:1], close[1:], b'**']
    a = StreamAssembler()
    for raw in pieces:
        a.content += raw.decode('utf-8', 'replace')  # what the runtime sent as delta text
        a.tokens.append(raw.decode('utf-8', 'replace'))
        a.token_bytes.append(list(raw))
        a.token_logprobs.append(-0.1)
    a.done = True

    assert '�' in a.content  # the damage, as the runtime emitted it
    assert a.text() == '**⌈m/2⌉**'
    assert a.apply(InferenceSynapse(messages=MSGS, model_id='m')).completion == '**⌈m/2⌉**'


def test_completion_keeps_the_deltas_when_the_bytes_cannot_be_trusted():
    from gittensor.serving.stream import StreamAssembler

    partial = StreamAssembler()  # a runtime that reports bytes for only some tokens
    partial.content, partial.tokens = 'ab', ['a', 'b']
    partial.token_bytes = [[97]]
    partial.done = True
    assert partial.text() == 'ab'

    truncated = StreamAssembler()  # bytes that do not decode: keep what the caller was streamed
    truncated.content, truncated.tokens = 'a�', ['a', '�']
    truncated.token_bytes = [[97], [0xE2, 0x88]]
    truncated.done = True
    assert truncated.text() == 'a�'

    none_reported = StreamAssembler()  # logprobs off: no bytes at all
    none_reported.content, none_reported.tokens = 'hello', []
    none_reported.done = True
    assert none_reported.text() == 'hello'


def test_spells_accepts_a_character_the_runtime_re_emitted_after_the_damaged_chunk():
    """Soak 7, blessed pin: the caller was streamed '**<FFFD>⌈m/2<FFFD>⌉**' where the reference reads
    '**⌈m/2⌉**' — the runtime streamed U+FFFD for the split first chunk and then the whole character. The run
    sits beside the character it stands for, not in place of it, so honest miners were being missed."""
    from gittensor.serving.audit import spells

    assert spells('**⌈m/2⌉** and **m** children'.encode(), '**�⌈m/2�⌉** and **m** children')
    assert spells('a⌈b'.encode(), 'a�b')  # the run does stand in for the character
    assert spells('a⌈b'.encode(), 'a��b')  # ... or for a run of them


def test_spells_still_refuses_altered_or_dropped_ascii():
    from gittensor.serving.audit import spells

    assert not spells('abc'.encode(), 'a�')  # ASCII dropped behind a replacement run
    assert not spells('abc'.encode(), 'abd')
    assert not spells('a⌈bc'.encode(), 'a�bd')  # ASCII changed beside a valid run
    assert not spells('ab'.encode(), 'a�b�c')  # ASCII added


def test_store_persists_the_round_timestamp(tmp_path):
    from gittensor.serving.store import ServingStore

    store = ServingStore(tmp_path / 'serving.db')
    state = ServingState()
    state.last_round_ts = 123.5
    store.save(state)
    assert store.load(ServingState()).last_round_ts == 123.5


def test_seed_ready_from_store_republishes_without_settling():
    """A restart within the READY TTL republishes the durable verdicts so the gateway does not 429 until the
    first live round; nothing settles from a seed, and a miner without a measured credit or attest on record
    waits in probation rather than taking user traffic on an assumed speed."""
    from gittensor.validator.serving import forward as fwd

    release = _echo_release()
    loadout = ServingLoadout(releases=[release])
    axons = [SimpleNamespace(is_serving=True) for _ in range(5)]
    validator = SimpleNamespace(
        uid=0,
        metagraph=SimpleNamespace(
            hotkeys=['vali', 'hk1', 'hk2', 'hk3', 'hk4'], axons=axons, validator_trust=[0, 0, 0, 0, 0]
        ),
    )
    state = ServingState()
    for hk in ('hk1', 'hk2', 'hk4'):
        for _ in range(6):
            state.audits.record(hk, release.release_id, 1.0)
    state.audits.strike('hk2', release.release_id)  # quarantined: neither READY nor probation
    state.last_credit.update({'hk1': 0.9, 'hk2': 0.9})  # hk4 passed its window but has no measured credit
    state.attest_status['hk1'] = {'passed': True, 'round': 0}
    state.attest_status['hk4'] = {'passed': True, 'round': 0}
    state.last_round_ts = time.time() - 60.0

    fwd.seed_ready_from_store(validator, state, loadout=loadout)  # type: ignore[arg-type]
    ready = state.ready_miners()
    assert [(m.uid, m.release_id) for m in ready] == [(1, release.release_id)]
    assert ready[0].score == pytest.approx(0.9)  # routed at the last measured credit
    assert state.snapshot()['probation_uids'] == [3, 4]
    assert state.settled_scores() == {}  # a seed pays nothing

    state.last_round_ts = time.time() - state.ready_ttl_s - 1.0
    fwd.seed_ready_from_store(validator, state, loadout=loadout)  # type: ignore[arg-type]  # past the TTL: no trust
    assert state.ready_miners() == []


def _attest_miner(**over) -> Any:  # a ServingMiner stand-in; Any so the hooks accept it
    miner = SimpleNamespace(
        release=SimpleNamespace(attest_url='http://sidecar:8081', attest_api_key='', request_timeout=30.0),
        metagraph=SimpleNamespace(hotkeys=['vali'], S=[2_000_000.0], validator_permit=[True], validator_trust=[1.0]),
        audit_budget={},
        attest_inflight=set(),
        slot_count=99,
        slot_claims={},
        prefilling={},
        attest_hold_until=0.0,
    )
    for k, v in over.items():
        setattr(miner, k, v)
    return miner


def test_attest_holds_admissions_and_waits_for_prefill_to_clear(monkeypatch):
    """The 2026-09-04/05 mainnet quarantines: the sidecar's VRAM fill landed on a prefill in flight, sparkinfer fell
    back to a pass whose output the reference rejects, and the validator's own challenge struck an honest card. Now
    a challenge refuses new admissions "busy" (neutral, rerouted) and its fill waits for admitted requests to reach
    their first content delta."""
    from gittensor.serving.state import is_busy_detail
    from gittensor.synapses import AttestSynapse
    from neurons import serving_miner as sm

    miner = _attest_miner()
    events: Dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {'devices': [{'wall_ms': 700.0, 'filled_bytes': 8_000_000_000}], 'queued_ms': 0.0}

    def fake_post(url, **kw):
        events['fill'] = time.monotonic()
        return _Resp()

    monkeypatch.setattr(sm.requests, 'post', fake_post)

    async def scenario():
        req = InferenceSynapse(messages=MSGS, model_id='m', max_tokens=4)
        req.dendrite = bt.TerminalInfo(hotkey='vali')
        assert not (await sm.blacklist_inference(miner, req))[0]  # admitted: prefilling from this moment
        assert sm.prefilling(miner) == 1

        async def user_traffic_meanwhile():
            await asyncio.sleep(0.15)
            late = InferenceSynapse(messages=MSGS, model_id='m', max_tokens=4)
            late.dendrite = bt.TerminalInfo(hotkey='vali')
            blocked, reason = await sm.blacklist_inference(miner, late)
            events['refused'] = (blocked, reason)
            miner.prefilling.pop(id(req))  # the admitted request reaches its first content delta
            events['cleared'] = time.monotonic()

        att = AttestSynapse(seed=7)
        att.dendrite = bt.TerminalInfo(hotkey='vali')
        side = asyncio.ensure_future(user_traffic_meanwhile())
        out = await sm.handle_attest(miner, att)
        await side
        return out

    out = asyncio.run(scenario())
    assert out.error is None and out.wall_ms == 700.0
    blocked, reason = events['refused']
    assert blocked and is_busy_detail(reason) and 'attestation' in reason  # busy, never a miss
    assert events['fill'] >= events['cleared']  # the fill waited for the prefill to finish
    assert events['fill'] - events['cleared'] < 0.2
    assert miner.attest_hold_until == 0.0 and miner.attest_inflight == set()
    fresh = InferenceSynapse(messages=MSGS, model_id='m', max_tokens=4)
    fresh.dendrite = bt.TerminalInfo(hotkey='vali')
    assert not asyncio.run(sm.blacklist_inference(miner, fresh))[0]  # admissions reopen with the challenge


def test_attest_prefill_wait_is_bounded_and_ignores_stale_marks():
    """A prefill that never reports its first content delays the fill by at most the drain bound, never the round;
    a mark nobody cleared (handler never ran, runtime stopped answering) holds nothing back."""
    from neurons import serving_miner as sm

    miner = _attest_miner(prefilling={1: time.monotonic()})
    t0 = time.monotonic()
    assert asyncio.run(sm.drain_prefill(miner, max_wait_s=0.2)) is False
    assert 0.2 <= time.monotonic() - t0 < 0.6
    miner.prefilling = {1: time.monotonic() - sm.PREFILL_STALE_S - 1.0}
    t0 = time.monotonic()
    assert asyncio.run(sm.drain_prefill(miner, max_wait_s=0.2)) is True
    assert time.monotonic() - t0 < 0.1 and miner.prefilling == {}
    assert asyncio.run(sm.drain_prefill(_attest_miner(), max_wait_s=0.2)) is True  # nothing admitted: no wait


def test_attest_hold_outlives_one_caller_while_another_still_fills(monkeypatch):
    """Two validators challenging at once: the hold stands until the last fill returns."""
    from gittensor.synapses import AttestSynapse
    from neurons import serving_miner as sm

    miner = _attest_miner(attest_inflight={'other-vali'})

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {'devices': [{'wall_ms': 700.0}], 'queued_ms': 0.0}

    monkeypatch.setattr(sm.requests, 'post', lambda url, **kw: _Resp())
    att = AttestSynapse(seed=7)
    att.dendrite = bt.TerminalInfo(hotkey='vali')
    asyncio.run(sm.handle_attest(miner, att))
    assert miner.attest_inflight == {'other-vali'} and sm.attest_holding(miner)
    miner.attest_inflight.clear()
    miner.attest_hold_until = 0.0
    assert not sm.attest_holding(miner)


def test_inference_stream_clears_the_prefill_mark_at_the_first_content_delta():
    """sparkinfer streams a role chunk (content null) before prefill finishes and may stream logprobs-only chunks
    (content ""); neither means decoding. The mark clears at the first chunk carrying text and the slot at the end."""
    from neurons import serving_miner as sm

    chunks = [
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":null},"logprobs":null}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"content":""},"logprobs":{"content":[]}}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"content":"Hi"},"logprobs":null}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    seen = []

    class Backend:
        def stream(self, messages, max_tokens, logprobs):
            for chunk in chunks:
                seen.append(sm.prefilling(miner))  # state left by the previous chunk
                yield chunk

    miner = _attest_miner(backend=Backend())
    syn = InferenceSynapse(messages=MSGS, model_id='m', max_tokens=4)
    syn.dendrite = bt.TerminalInfo(hotkey='vali')
    assert sm.claim_slot(miner, syn) and sm.prefilling(miner) == 1
    sent = []

    async def run():
        response = await sm.handle_inference(miner, syn)
        await response.token_streamer(lambda msg: sent.append(msg) or asyncio.sleep(0))

    asyncio.run(run())
    assert seen == [1, 1, 1, 0, 0]  # role and logprobs-only chunks keep the mark; "Hi" clears it
    assert [m['body'] for m in sent] == chunks
    assert miner.prefilling == {} and miner.slot_claims == {}
    reasoning = b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"th"},"logprobs":null}]}\n\n'
    assert sm._FIRST_CONTENT.search(reasoning)  # a thinking model's first text counts too
    assert not sm._FIRST_CONTENT.search(chunks[0]) and not sm._FIRST_CONTENT.search(chunks[1])


def test_shipped_curve_pays_an_honest_card_in_full_at_2_to_5_concurrent():
    """#1753: the blessed curve had points at 1 and 6 only, and the straight line between them sat far above what a
    5090 does at 2-5 streams, so an honest card read 0.32-0.48x expected there — under the floor, zero credit. The
    curve now carries measured points 2-12 and interpolates on aggregate rate. The rows are the issue's on-box
    measurement of an unshared, correctly pinned card."""
    from gittensor.validator.serving.scoring import decode_credit, expected_decode_tps

    release = load_serving_loadout().primary
    assert release.decode_per_request is not None
    miner_measured = {1: 426.8, 2: 148.2, 3: 91.8, 4: 74.7, 5: 59.9, 6: 49.6, 8: 37.5}
    for n, observed in miner_measured.items():
        expected = expected_decode_tps(release.decode_per_request, n)
        assert observed / expected >= 0.8, (n, observed, expected)  # inside the WAN tolerance: full credit
        assert decode_credit(observed, expected) == 1.0
    # and between the sparse tail points a queued stream is still expected at the last measured rate, not below it
    assert expected_decode_tps(release.decode_per_request, 20) == pytest.approx(
        (16 * 19.4 + (24 * 19.4 - 16 * 19.4) * 0.5) / 20
    )
    assert expected_decode_tps(release.decode_per_request, 64) == 19.5
    # a card genuinely shared between two hotkeys still reads under the floor at 1 in flight
    assert decode_credit(144.3, expected_decode_tps(release.decode_per_request, 1)) == 0.0
