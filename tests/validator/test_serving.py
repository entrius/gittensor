"""Tests for the serving beta: deterministic backend, audit verification, gateway dispatch, emission pool blending."""

import json
import random
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gittensor.constants import SERVING_AUDIT_WINDOW
from gittensor.serving.api import build_app, parse_api_keys
from gittensor.serving.audit import (
    AuditCase,
    AuditWindow,
    BankReference,
    EchoReference,
    overlap_threshold,
    reference_for,
    verify_response,
)
from gittensor.serving.backends import EchoBackend, GenerationResult, expected_completion
from gittensor.serving.loadout import ECHO_LOADOUT_PATH, ServingLoadout, ServingRelease, load_serving_loadout
from gittensor.serving.state import ReadyMiner, RequestRecord, ServingState
from gittensor.validator import emission_allocation
from gittensor.validator.emission_allocation import blend_emission_pools
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
    assert release.base_url and release.audit_bank and release.reference_url
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
    assert client.get('/v1/models', headers={'Authorization': 'Bearer k2'}).status_code == 200
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


def test_overlap_threshold_interpolates_calibrated_table():
    table = ((1, 0.1), (5, 0.3), (20, 0.5))
    assert overlap_threshold(0, table) == float('inf')
    assert overlap_threshold(1, table) == 0.1
    assert overlap_threshold(3, table) == pytest.approx(0.2)
    assert overlap_threshold(20, table) == 0.5
    assert overlap_threshold(50, table) == 0.5  # saturates at the largest window


def test_audit_window_rolls_per_hotkey_and_release():
    w = AuditWindow(size=3, thresholds=((1, 0.5),))
    assert not w.verdict('hk', 'm').passed and w.verdict('hk', 'm').n_audits == 0
    for x in (1.0, 1.0, 1.0, 0.0):  # oldest 1.0 rolls out -> mean 2/3
        w.record('hk', 'm', x)
    v = w.verdict('hk', 'm')
    assert v.n_audits == 3 and v.mean_overlap == pytest.approx(2 / 3) and v.passed
    w.record('hk', 'm', 0.0)  # mean 1/3 < 0.5
    assert not w.verdict('hk', 'm').passed
    w.record('hk', 'other', 1.0)  # releases are tracked separately
    assert w.verdict('hk', 'other').passed
    assert w.verdict('hk2', 'm').n_audits == 0  # a new hotkey on the same UID starts clean


EXPERIMENT = Path(__file__).resolve().parents[2] / 'docs' / 'serving-experiments' / '2026-08-22-planted-cheater'


def _replay(rows: list, window: int, trials: int, seed: int) -> float:
    """Fraction of trials in which a miner drawing `window` audits from `rows` passes the default thresholds."""
    rng = random.Random(seed)
    passed = 0
    for t in range(trials):
        w = AuditWindow()
        for _ in range(window):
            w.record('hk', 'qwen3.6-35b-a3b', rng.choice(rows)['positional_overlap'])
        passed += w.verdict('hk', 'qwen3.6-35b-a3b').passed
    return passed / trials


@pytest.mark.skipif(not EXPERIMENT.exists(), reason='experiment data not checked out')
def test_audit_window_calibrated_on_experiment_data():
    """The shipped thresholds must keep honest miners in and the planted cheaters out (docs/serving-experiments)."""
    load = lambda name: json.load(open(EXPERIMENT / name))['rows']  # noqa: E731
    honest = load('honest-rerun.json') + load('honest-under-load.json')
    q2 = load('cheat-q2kxl-llamacpp.json')
    llama_q4 = load('honestweights-llamacpp.json')
    full = SERVING_AUDIT_WINDOW
    assert _replay(honest, full, 2000, 1) >= 0.97  # ~1% FP by construction, slack for sampling
    assert _replay(honest, 4, 2000, 2) >= 0.95  # first round (4 audits) is not a trap for honest miners
    assert _replay(q2, full, 2000, 3) <= 0.01
    assert _replay(q2, 10, 2000, 4) <= 0.02
    assert _replay(llama_q4, full, 2000, 5) <= 0.12


def test_blend_pays_serving_pool_pro_rata(monkeypatch):
    monkeypatch.setattr(emission_allocation, 'SERVING_EMISSION_SHARE', 0.05)
    miner_uids = {0, 1, 2}
    rewards = blend_emission_pools({}, {}, miner_uids, serving_scores={1: 1.0, 2: 3.0})
    # Empty registry: the full OSS pool recycles to UID 0; serving pool splits 1:3.
    oss = emission_allocation.OSS_EMISSION_SHARE
    assert np.isclose(rewards[1], 0.05 * 0.25)
    assert np.isclose(rewards[2], 0.05 * 0.75)
    assert np.isclose(rewards[0], oss)
    assert np.isclose(rewards.sum(), oss + 0.05)


def test_blend_recycles_serving_pool_without_scorers(monkeypatch):
    monkeypatch.setattr(emission_allocation, 'SERVING_EMISSION_SHARE', 0.05)
    miner_uids = {0, 1, 2}
    rewards = blend_emission_pools({}, {}, miner_uids, serving_scores={})
    oss = emission_allocation.OSS_EMISSION_SHARE
    assert np.isclose(rewards[0], oss + 0.05)
    assert np.isclose(rewards.sum(), oss + 0.05)


def test_blend_default_share_is_shadow_mode():
    # SERVING_EMISSION_SHARE = 0.0 in constants: serving scores must not move rewards.
    miner_uids = {0, 1}
    with_scores = blend_emission_pools({}, {}, miner_uids, serving_scores={1: 1.0})
    without_scores = blend_emission_pools({}, {}, miner_uids)
    assert np.allclose(with_scores, without_scores)


def test_score_response_feeds_window_metrics():
    from gittensor.synapses import InferenceSynapse
    from gittensor.validator.serving.forward import score_response

    release = _echo_release()
    case = EchoReference(release).sample()
    miss = InferenceSynapse(messages=case.messages, model_id=release.model_id, max_tokens=case.max_tokens)
    verdict, ms, rec = score_response(3, miss, case, release)
    assert verdict.positional_overlap == 0.0 and ms == float('inf') and not rec.ok

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
