"""Tests for the serving beta: deterministic backend, audit verification, gateway dispatch, emission pool blending."""

import json
from typing import Dict

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


def test_score_response_feeds_window_metrics():
    from gittensor.synapses import InferenceSynapse
    from gittensor.validator.serving.forward import score_response

    release = _echo_release()
    case = EchoReference(release).sample()
    miss = InferenceSynapse(messages=case.messages, model_id=release.model_id, max_tokens=case.max_tokens)
    verdict, ms, rec = score_response(3, miss, case, release)
    assert verdict.positional_overlap == 0.0 and ms == float('inf') and not rec.ok
    assert rec.latency_ms is None  # inf is not JSON; /v1/serving/status must serialize the record

    honest = InferenceSynapse(
        messages=case.messages,
        model_id=release.model_id,
        max_tokens=case.max_tokens,
        completion=case.reference_completion,
        served_model_id=release.model_id,
        tokens=list(case.reference_tokens),
        token_logprobs=list(case.reference_logprobs),
    )
    verdict, _, rec = score_response(3, honest, case, release)
    assert verdict.positional_overlap == 1.0 and verdict.passed and rec.ok

    wrong = honest.model_copy(update={'served_model_id': 'other'})
    verdict, _, rec = score_response(3, wrong, case, release)
    assert verdict.positional_overlap == 0.0 and rec.detail.startswith('wrong model')


def test_latency_credit_matches_measured_latencies():
    """Honest on-box 64-token audit p95 was 166 ms (2026-08-22/24 measurements); llama.cpp ~600 ms."""
    honest_p95, intercontinental_rtt = 166.0, 250.0
    assert latency_credit(honest_p95 + intercontinental_rtt) == 1.0
    proxied = honest_p95 + intercontinental_rtt + 2 * 150.0  # validator -> miner -> remote GPU in another region
    assert latency_credit(proxied) < 1.0
    assert latency_credit(600.0 + intercontinental_rtt + 2 * 150.0) < 0.4  # slow runtime behind the proxy


def test_audit_window_persists_across_restart(tmp_path):
    path = tmp_path / 'serving_audits.json'
    assert AuditWindow.load(path).verdict('hk', 'm').n_audits == 0  # missing file -> empty window
    w = AuditWindow(size=3)
    for x in (0.2, 0.9, 0.8, 0.7):  # 0.2 rolls out
        w.record('hk', 'm', x)
    w.record('hk2', 'm', 1.0)
    w.save(path)
    loaded = AuditWindow.load(path, size=3)
    assert loaded.verdict('hk', 'm').as_dict() == w.verdict('hk', 'm').as_dict()
    assert loaded.verdict('hk2', 'm').n_audits == 1
    path.write_text('not json')
    assert AuditWindow.load(path).verdict('hk', 'm').n_audits == 0  # corrupt file -> empty window, no crash


def test_audit_window_path_follows_neuron_state_dir(tmp_path):
    from types import SimpleNamespace

    from gittensor.validator.serving.forward import audit_window_path

    vali = SimpleNamespace(config=SimpleNamespace(neuron=SimpleNamespace(full_path=str(tmp_path))))
    assert audit_window_path(vali) == tmp_path / 'serving_audits.json'  # type: ignore[arg-type]
    assert audit_window_path(SimpleNamespace()) is None  # type: ignore[arg-type]


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
    assert out['usage']['ttft_ms'] == 3.0


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

    from gittensor.serving.stream import result_to_sse

    calls: Dict[int, int] = {}  # id(axon) -> requests sent
    inflight: Dict[object, int] = {}

    async def call_stream(target_axon, synapse, timeout, deserialize):
        calls[id(target_axon)] = calls.get(id(target_axon), 0) + 1
        final = synapse.model_copy()
        if not any(target_axon is d for d in dead_axons):
            gpu = (gpu_of or {}).get(id(target_axon))
            if gpu is not None:
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

    monkeypatch.setattr(fwd, 'SERVING_PROBE_REQUESTS', probes)
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
    path = tmp_path / 'audits.json'
    w.save(path)
    again = AuditWindow.load(path, quarantine_s=100.0)
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
    monkeypatch.setattr('gittensor.validator.serving.forward.SERVING_PROBE_REQUESTS', 0)
    import asyncio

    from gittensor.validator.serving import forward as fwd

    scores = asyncio.run(
        fwd.audit_round(state, dendrite, [(1, 'hk1', axon)], ServingLoadout(releases=[bad, good]))  # type: ignore[arg-type]
    )
    assert scores == {'hk1': 1.0}
    assert [m.uid for m in state.ready_miners()] == [1]
    assert state.scores_for(['v', 'hk1']) == {1: 1.0}
    assert state.scores_for(['v', 'other']) == {}  # UID 1's hotkey changed since the round: nothing carries over


def test_capacity_probe_splits_a_shared_gpu(monkeypatch):
    """Two hotkeys on one card each get about half the capacity a lone card gets; a lone card gets ~1."""
    from types import SimpleNamespace

    from gittensor.validator.serving import forward as fwd

    good = _echo_release()
    lone, a, b = (SimpleNamespace(is_serving=True) for _ in range(3))
    gpu_of = {id(lone): 'gpu-1', id(a): 'gpu-2', id(b): 'gpu-2'}
    dendrite, _ = _dendrite_echoing(good, gpu_of=gpu_of, token_s=0.0005)
    rates = []
    real_probe = fwd.probe_axon

    async def spy(*args, **kwargs):
        rate = await real_probe(*args, **kwargs)
        rates.append(rate)
        return rate

    monkeypatch.setattr(fwd, 'probe_axon', spy)
    monkeypatch.setattr(fwd, 'SERVING_PROBE_TARGET_TPS', 1e-9)
    state = ServingState(settlement_rounds=1)
    state.enqueue_served(_served(1, good))
    _round(state, dendrite, [(1, 'hk1', lone)], good, monkeypatch, probes=4)
    probe = [r for r in state.recent(50) if r.kind == 'probe']
    assert len(probe) == 4 and all(r.ok for r in probe)
    lone_tps = rates[0]

    monkeypatch.setattr(fwd, 'SERVING_PROBE_TARGET_TPS', lone_tps)
    state = ServingState(settlement_rounds=1)
    for uid in (1, 2, 3):
        state.enqueue_served(_served(uid, good))
    serving = [(1, 'hk1', lone), (2, 'hk2', a), (3, 'hk3', b)]
    scores = _round(state, dendrite, serving, good, monkeypatch, probes=4)
    assert scores['hk1'] == pytest.approx(1.0, abs=0.15)
    assert scores['hk2'] == pytest.approx(0.5, abs=0.15) and scores['hk3'] == pytest.approx(0.5, abs=0.15)
    assert scores['hk2'] + scores['hk3'] == pytest.approx(scores['hk1'], abs=0.2)


def test_probe_misses_cost_capacity_not_the_window(monkeypatch):
    """A miner that serves traffic honestly but chokes on the burst keeps its window and is probed again."""
    from types import SimpleNamespace

    good = _echo_release()
    axon = SimpleNamespace(is_serving=True)
    dendrite, calls = _dendrite_echoing(good, dead_axons=(axon,))  # every probe request dropped
    state = ServingState(settlement_rounds=1)
    for _ in range(2):
        state.enqueue_served(_served(1, good))
        scores = _round(state, dendrite, [(1, 'hk1', axon)], good, monkeypatch, probes=6)
        assert scores == {'hk1': 0.0}
    assert calls == {id(axon): 12}
    window = state.audits.verdict('hk1', good.model_id)
    assert window.passed and window.n_audits == 2 and window.mean == 1.0
    assert sum(1 for r in state.recent(50) if r.kind == 'probe' and not r.ok) == 12


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
    miner = SimpleNamespace(metagraph=SimpleNamespace(hotkeys=['vali', 'builder', 'small'], S=[5000.0, 100.0, 99.0]))

    def call(hotkey):
        syn = InferenceSynapse(messages=MSGS, model_id='m')
        assert syn.dendrite is not None
        syn.dendrite.hotkey = hotkey
        return asyncio.run(blacklist_inference(miner, syn))  # type: ignore[arg-type]

    assert call('vali') == (False, 'Staked caller')
    assert call('builder') == (False, 'Staked caller')
    assert call('small')[0] and 'Stake 99 below 100' in call('small')[1]
    assert call('stranger') == (True, 'Unrecognized hotkey')
