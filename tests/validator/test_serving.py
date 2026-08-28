"""Tests for the serving beta: deterministic backend, audit verification, gateway dispatch, emission pool blending."""

import json
from collections import deque
from typing import Dict

import bittensor as bt
import pytest
from fastapi.testclient import TestClient

from gittensor.constants import SERVING_AUDIT_WINDOW, SERVING_AUDIT_WINDOW_THRESHOLDS
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
from gittensor.serving.state import ReadyMiner, RequestRecord, ServedRequest, ServingState
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
    return ReadyMiner(uid=uid, hotkey=f'hk{uid}', axon=None, score=score, model_id='echo-v0')  # type: ignore[arg-type]


def test_state_least_inflight_dispatch():
    state = ServingState()
    assert state.acquire() is None
    state.publish_round([_ready(1, 0.5), _ready(2, 1.0)], {})
    first = state.acquire()
    assert first is not None and first.uid == 2  # tie on inflight -> higher score
    second = state.acquire()
    assert second is not None and second.uid == 1
    third = state.acquire()
    assert third is not None and third.uid == 2  # both at 1 inflight -> higher score again
    state.release(2)
    state.release(2)
    again = state.acquire()
    assert again is not None and again.uid == 2
    state.publish_round([_ready(1)], {})
    only = state.acquire()
    assert only is not None and only.uid == 1
    state.record(RequestRecord(ts=0, kind='gateway', uid=1, ok=True, latency_ms=10))
    assert state.snapshot()['gateway_ok'] == 1


def test_acquire_filters_by_release():
    state = ServingState()
    other = ReadyMiner(uid=9, hotkey='hk9', axon=None, score=1.0, model_id='other-model')  # type: ignore[arg-type]
    state.publish_round([_ready(1), other], {})
    assert state.acquire('other-model') is other
    assert state.acquire('missing') is None
    picked = state.acquire('echo-v0')
    assert picked is not None and picked.uid == 1


# --- gateway ----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, result: GenerationResult, model_id: str):
        self.completion = result.completion
        self.served_model_id = model_id
        self.tokens = result.tokens
        self.token_ids = result.token_ids
        self.token_logprobs = result.token_logprobs
        self.ttft_ms = 12.0
        self.decode_tps = 99.0
        self.finish_reason = 'stop'
        self.usage = result.usage


def _gateway_client(state: ServingState, monkeypatch):
    loadout = _echo_release()

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
        json={'messages': MSGS, 'max_tokens': 4, 'stream': True},
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
    state.probe_history['hk'] = deque([180.0, 178.0], maxlen=3)
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
    assert list(loaded.probe_history['hk']) == [180.0, 178.0]
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
            if token_ids:
                return {'tokens': tokens, 'logprobs': logprobs, 'argmax': tokens, 'usage': {}}
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
    assert eos.passed and calls[-1] == ids
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


def test_emission_pools_sum_to_one():
    from gittensor.constants import EMISSION_SHARE_TOLERANCE, OSS_EMISSION_SHARE, SERVING_EMISSION_SHARE_CAP

    assert abs(OSS_EMISSION_SHARE + SERVING_EMISSION_SHARE_CAP - 1.0) < EMISSION_SHARE_TOLERANCE


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


def _round(state, dendrite, serving, release, monkeypatch, probes: int = 0):
    import asyncio

    from gittensor.validator.serving import forward as fwd

    return asyncio.run(fwd.audit_round(state, dendrite, serving, ServingLoadout(releases=[release])))  # type: ignore[arg-type]


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
    new = ReadyMiner(uid=5, hotkey='hk5', axon=axon, score=0.0, model_id='echo-v0')  # type: ignore[arg-type]
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

    loadout = _echo_release()
    state = ServingState()
    probation = ReadyMiner(uid=9, hotkey='hk9', axon=SimpleNamespace(), score=0.0, model_id='echo-v0')  # type: ignore[arg-type]
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
    assert scores['hk1'] == pytest.approx(0.8)  # 4 of 5 served requests earned full latency credit
    assert scores['hk2'] == 0.0 and scores['hk3'] == 0.0
    w1, w2 = state.audits.verdict('hk1', good.model_id), state.audits.verdict('hk2', good.model_id)
    assert w1.passed and w1.n_audits == 5 and w1.mean == 0.8
    assert not w2.passed and w2.n_audits == 0 and w2.quarantined_until > 0  # struck
    assert [m.uid for m in state.ready_miners()] == [1]
    assert state.snapshot()['probation_uids'] == [3]  # the cheater is quarantined, not on probation
    assert sum(1 for r in state.recent(50) if r.kind == 'verify') == 7

    scores = _round(state, dendrite, serving, good, monkeypatch)  # quiet round: READY on the window, credit 1.0
    assert scores['hk1'] == 1.0 and [m.uid for m in state.ready_miners()] == [1]


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
    assert scores['hk1'] == 1.0 and scores['hk2'] == 0.0 and scores['hk3'] == 0.0
    assert state.audits.verdict('hk1', good.model_id).n_audits == 2
    assert (
        state.audits.verdict('hk2', good.model_id).n_audits == 2
        and not state.audits.verdict('hk2', good.model_id).passed
    )


def test_budget_refusal_is_neutral_not_a_miss(monkeypatch):
    from types import SimpleNamespace

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    dendrite, _ = _dendrite_echoing(good)
    state = ServingState(settlement_rounds=1)
    state.enqueue_served(_served(1, good))
    refused = _served(1, good, ok=False)
    refused.detail = 'Validator audit budget spent (50000 tokens per tempo)'
    state.enqueue_served(refused)
    state.enqueue_served(_served(1, good, ok=False))
    _round(state, dendrite, [(1, 'hk1', axon)], good, monkeypatch)
    w = state.audits.verdict('hk1', good.model_id)
    assert w.n_audits == 2 and w.mean == 0.5  # one pass, one real miss, the refusal ignored


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
    assert scores['hk1'] == 1.0 and scores['hk2'] == pytest.approx(0.5)


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
        fwd.audit_round(state, dendrite, [(1, 'hk1', axon)], ServingLoadout(releases=[bad, good]))  # type: ignore[arg-type]
    )
    assert scores == {'hk1': 1.0}
    assert [m.uid for m in state.ready_miners()] == [1]
    assert state.scores_for(['v', 'hk1']) == {1: 1.0}
    assert state.scores_for(['v', 'other']) == {}  # UID 1's hotkey changed since the round: nothing carries over


def test_decode_speed_prices_served_requests_against_the_blessing_curve():
    from gittensor.validator.serving.scoring import decode_credit, expected_decode_tps, request_speed_credit

    curve = {1: 440.0, 6: 46.0, 16: 19.0}
    assert expected_decode_tps(curve, 1) == 440.0 and expected_decode_tps(curve, 0) == 440.0
    assert expected_decode_tps(curve, 16) == 19.0 and expected_decode_tps(curve, 40) == 19.0
    assert expected_decode_tps(curve, 11) == pytest.approx(32.5)  # linear between 6 and 16
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
    assert request_speed_credit(honest, release) == pytest.approx(1.0)
    busy = req(64, 100.0, 100.0 + 64 / 19.0 * 1000.0, inflight=16)  # 19 tok/s is what one card does at 16 in flight
    assert request_speed_credit(busy, release) == pytest.approx(1.0)
    shared = req(64, 100.0, 100.0 + 64 / 19.0 * 1000.0, inflight=1)  # 19 tok/s while we sent it one request
    assert request_speed_credit(shared, release) == 0.0
    slowish = req(64, 100.0, 100.0 + 64 / 264.0 * 1000.0)
    assert request_speed_credit(slowish, release) == pytest.approx(0.75)
    short = req(8, 100.0, 5_000.0)  # too few tokens to measure decode: TTFT band only
    assert request_speed_credit(short, release) == 1.0
    slow_ttft = req(64, 1_000.0, 1_000.0 + 64 / 440.0 * 1000.0)
    assert request_speed_credit(slow_ttft, release) == pytest.approx(0.5)


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
    assert ea.serving_share(1.0, None) == 0.17  # no pricing (testnet): pay the cap pro-rata
    assert ea.serving_share(1.0, ServingPricing(0.0, 0.847)) == 0.17


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


def test_serving_pricing_reads_chain_and_loadout(monkeypatch):
    from types import SimpleNamespace

    from gittensor.validator.serving import pricing as pr

    vali = SimpleNamespace(
        metagraph=SimpleNamespace(E=[100.0, 200.0], netuid=74),
        subtensor=SimpleNamespace(subnet=lambda netuid: SimpleNamespace(price=0.004)),
    )
    monkeypatch.setattr(pr, 'load_serving_loadout', lambda: SimpleNamespace(tao_usd=250.0))
    p = pr.serving_pricing(vali)  # type: ignore[arg-type]
    assert p is not None and p.alpha_per_hour_to_miners == pytest.approx(150.0 * 60 / 72) and p.alpha_usd == 1.0
    monkeypatch.setattr(pr, 'load_serving_loadout', lambda: SimpleNamespace(tao_usd=None))
    assert pr.serving_pricing(vali) is None  # type: ignore[arg-type]
    vali.subtensor = SimpleNamespace(subnet=lambda netuid: (_ for _ in ()).throw(RuntimeError('rpc')))
    assert pr.serving_pricing(vali) is None  # type: ignore[arg-type]


def test_ready_set_expires_after_ttl():
    from types import SimpleNamespace

    state = ServingState(ready_ttl_s=10.0, settlement_rounds=1)
    miner = ReadyMiner(uid=1, hotkey='hk1', axon=SimpleNamespace(), score=1.0, model_id='m')  # type: ignore[arg-type]
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

    def blend(evals, repos, uids, maintainers, serving_scores, pricing=None):
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

    async def consume(_dendrite, _axon, synapse, _timeout):
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

    """No verdict -> probation; a failing cohort member scores 0; a passing one keeps its speed credit; non-cohort
    members keep their last verdict; duplicate GPU UUIDs fail both; status survives the store."""
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
    assert out == {'hk1': False, 'hk2': False, 'hk3': False}  # hk2 wrong digest; hk1/hk3 share GPU-1
    assert state.attest_status['hk2']['reason'] == 'digest mismatch'
    assert 'duplicate GPU' in state.attest_status['hk1']['reason']

    replies[id(axons['hk3'])] = _attest_reply(uuid='GPU-3')
    out = asyncio.run(att.attest_round(state, dendrite, candidates, release, rng=random.Random(0)))  # type: ignore[arg-type]
    assert out['hk1'] and out['hk3'] and not out['hk2']  # all three were re-challenged (none had passed)
    store = ServingStore(tmp_path / 'serving.db')
    store.save(state)
    again = store.load(ServingState())
    assert again.attest_status['hk1']['uuid'] == 'GPU-1' and again.attest_status['hk2']['passed'] is False

    monkeypatch.setattr(att, 'reference_challenge', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('ref down')))
    neutral = asyncio.run(att.attest_round(state, dendrite, candidates, release))  # type: ignore[arg-type]
    assert neutral == {'hk1': True, 'hk2': False, 'hk3': True}  # reference down: nothing changes


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
    assert scores == {'hk1': 1.0, 'hk2': 0.0}
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
