"""Tests for the serving beta: deterministic backend, audit verification, gateway dispatch, emission pool blending."""

import json

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
from gittensor.serving.state import ReadyMiner, RequestRecord, ServingState
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
    state.publish_ready([_ready(1, 0.5), _ready(2, 1.0)])
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
    state.publish_ready([_ready(1)])
    only = state.acquire()
    assert only is not None and only.uid == 1
    state.record(RequestRecord(ts=0, kind='gateway', uid=1, ok=True, latency_ms=10))
    assert state.snapshot()['gateway_ok'] == 1


def test_acquire_filters_by_release():
    state = ServingState()
    other = ReadyMiner(uid=9, hotkey='hk9', axon=None, score=1.0, model_id='other-model')  # type: ignore[arg-type]
    state.publish_ready([_ready(1), other])
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

    async def fake_dispatch(dendrite, miner, messages, max_tokens, lo, timeout):
        return _FakeResponse(expected_completion(messages, max_tokens, lo.model_id), lo.model_id)

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
    state.publish_ready([_ready(7)])
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
    bad = client.post(
        '/v1/chat/completions', json={'messages': MSGS, 'stream': True}, headers={'Authorization': 'Bearer k1'}
    )
    assert bad.status_code == 400


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
    """With a deterministic runtime the bar is 0.85: a single miss in the last 20 is fine, two in a row of 4 is not."""
    w = AuditWindow()
    hk = 'hk'
    for _ in range(4):
        w.record(hk, 'm', 1.0)
    assert w.verdict(hk, 'm').passed
    w.record(hk, 'm', 0.0)  # 4/5 = 0.8 < 0.85 -> drops this round
    assert not w.verdict(hk, 'm').passed
    for _ in range(3):
        w.record(hk, 'm', 1.0)  # 7/8 = 0.875 -> back
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
    from gittensor.constants import EMISSION_SHARE_TOLERANCE, OSS_EMISSION_SHARE, SERVING_EMISSION_SHARE

    assert abs(OSS_EMISSION_SHARE + SERVING_EMISSION_SHARE - 1.0) < EMISSION_SHARE_TOLERANCE


def test_verify_rejects_malformed_reference():
    case = AuditCase(messages=[], max_tokens=4, reference_tokens=['a', 'b'], reference_logprobs=[-0.1])
    v = verify_response(case, ['a', 'b'], [-0.1, -0.2])
    assert not v.passed and v.reason == 'empty or malformed reference'


def test_gateway_400_on_user_shaped_bad_input(monkeypatch):
    state = ServingState()
    state.publish_ready([_ready(7)])
    client = _gateway_client(state, monkeypatch)
    h = {'Authorization': 'Bearer k1'}
    assert (
        client.post('/v1/chat/completions', json={'messages': MSGS, 'max_tokens': 'lots'}, headers=h).status_code == 400
    )
    array_content = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    assert client.post('/v1/chat/completions', json={'messages': array_content}, headers=h).status_code == 400
    assert client.post('/v1/chat/completions', json={'messages': [{'role': 'user'}]}, headers=h).status_code == 400
    assert state.inflight() == {7: 0}


def test_serving_round_skips_release_without_reference(monkeypatch, tmp_path):
    """A release whose reference is unreachable is skipped and logged; the round still completes."""
    import asyncio
    from types import SimpleNamespace

    from gittensor.validator.serving import forward as fwd

    good = _echo_release()
    bad = ServingRelease(
        model_id='ghost', backend='openai-compat', base_url='http://x', reference_url='http://127.0.0.1:1'
    )
    monkeypatch.setattr(fwd, 'load_serving_loadout', lambda: ServingLoadout(releases=[bad, good]))
    monkeypatch.setattr(fwd, 'SERVING_CHALLENGES_PER_ROUND', 2)

    async def dendrite(axons, synapse, deserialize, timeout):
        out = []
        for _ in axons:
            ref = expected_completion(synapse.messages, synapse.max_tokens, good.model_id)
            resp = synapse.model_copy(
                update={
                    'completion': ref.completion,
                    'served_model_id': good.model_id,
                    'tokens': ref.tokens,
                    'token_logprobs': ref.token_logprobs,
                }
            )
            resp.dendrite.process_time = 0.05  # 50 ms -> full latency credit
            out.append(resp)
        return out

    axon = SimpleNamespace(is_serving=True)
    vali = SimpleNamespace(
        uid=0,
        metagraph=SimpleNamespace(hotkeys=['v', 'hk1'], axons=[axon, axon]),
        dendrite=dendrite,
        serving_state=ServingState(),
        config=SimpleNamespace(neuron=SimpleNamespace(full_path=str(tmp_path))),
    )
    scores = asyncio.run(fwd.serving_challenges(vali, {0, 1}))  # type: ignore[arg-type]
    assert scores == {1: 1.0}
    assert [m.uid for m in vali.serving_state.ready_miners()] == [1]
    assert (tmp_path / 'serving_audits.json').exists()


def test_serving_audits_run_between_oss_rounds(monkeypatch):
    """A serving step audits and caches scores without running the OSS round; the OSS round blends the cache."""
    import asyncio
    from types import SimpleNamespace

    from gittensor.validator import forward as top

    calls = []
    monkeypatch.setattr(top, 'SERVING_ENABLED', True)
    monkeypatch.setattr(top, 'SERVING_STEPS_INTERVAL', 5)
    monkeypatch.setattr(top, 'VALIDATOR_STEPS_INTERVAL', 120)
    monkeypatch.setattr(top, 'VALIDATOR_WAIT', 0)
    monkeypatch.setattr(top, 'get_all_uids', lambda self: {1})

    async def audits(self, uids):
        calls.append(('serving', uids))
        return {1: 0.5}

    def oss(*a, **k):
        raise AssertionError('OSS round must not run on a serving-only step')

    monkeypatch.setattr(top, 'serving_challenges', audits)
    monkeypatch.setattr(top, 'load_master_repo_weights', oss)

    vali = SimpleNamespace(step=5, serving_state=ServingState(), serving_scores={})
    asyncio.run(top.forward(vali))  # type: ignore[arg-type]
    assert calls == [('serving', {1})]
    assert vali.serving_scores == {1: 0.5}

    vali.step = 7
    asyncio.run(top.forward(vali))  # type: ignore[arg-type]
    assert calls == [('serving', {1})]  # off-cadence step: no audit, cache untouched
    assert vali.serving_scores == {1: 0.5}


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
