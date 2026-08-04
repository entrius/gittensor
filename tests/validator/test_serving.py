"""Tests for the serving beta: deterministic backend, challenge scoring, emission pool blending."""

import numpy as np

from gittensor.serving.backends import EchoBackend, expected_completion
from gittensor.serving.loadout import ServingLoadout, load_serving_loadout
from gittensor.validator import emission_allocation
from gittensor.validator.emission_allocation import blend_emission_pools
from gittensor.validator.serving.scoring import challenge_score, latency_credit


def _echo_loadout() -> ServingLoadout:
    return ServingLoadout(model_id='echo-v0', backend='echo', max_tokens=8)


def test_expected_completion_is_deterministic():
    a = expected_completion('prompt', 8, 'echo-v0')
    b = expected_completion('prompt', 8, 'echo-v0')
    assert a == b
    assert len(a.split(' ')) == 8


def test_expected_completion_varies_by_inputs():
    base = expected_completion('prompt', 8, 'echo-v0')
    assert expected_completion('other', 8, 'echo-v0') != base
    assert expected_completion('prompt', 8, 'other-model') != base


def test_echo_backend_matches_expected_completion():
    backend = EchoBackend(_echo_loadout())
    result = backend.generate('prompt', 8)
    assert result.completion == expected_completion('prompt', 8, 'echo-v0')
    assert result.model_id == 'echo-v0'


def test_default_loadout_file_loads():
    loadout = load_serving_loadout()
    assert loadout.backend == 'echo'
    assert loadout.model_id
    assert loadout.max_tokens > 0


def test_latency_credit_bands(monkeypatch):
    monkeypatch.setattr('gittensor.validator.serving.scoring.SERVING_LATENCY_FULL_CREDIT_MS', 1_000.0)
    monkeypatch.setattr('gittensor.validator.serving.scoring.SERVING_LATENCY_ZERO_CREDIT_MS', 3_000.0)
    assert latency_credit(500.0) == 1.0
    assert latency_credit(1_000.0) == 1.0
    assert latency_credit(2_000.0) == 0.5
    assert latency_credit(3_000.0) == 0.0
    assert latency_credit(10_000.0) == 0.0


def test_challenge_score_requires_correctness():
    assert challenge_score(False, 10.0) == 0.0
    assert challenge_score(True, 0.0) == 1.0


def test_blend_pays_serving_pool_pro_rata(monkeypatch):
    monkeypatch.setattr(emission_allocation, 'SERVING_EMISSION_SHARE', 0.05)
    miner_uids = {0, 1, 2}
    rewards = blend_emission_pools({}, {}, miner_uids, serving_scores={1: 1.0, 2: 3.0})
    # Empty registry: the full OSS pool (0.90) recycles to UID 0; serving pool splits 1:3.
    assert np.isclose(rewards[1], 0.05 * 0.25)
    assert np.isclose(rewards[2], 0.05 * 0.75)
    assert np.isclose(rewards[0], 0.90)
    assert np.isclose(rewards.sum(), 0.95)


def test_blend_recycles_serving_pool_without_scorers(monkeypatch):
    monkeypatch.setattr(emission_allocation, 'SERVING_EMISSION_SHARE', 0.05)
    miner_uids = {0, 1, 2}
    rewards = blend_emission_pools({}, {}, miner_uids, serving_scores={})
    assert np.isclose(rewards[0], 0.95)
    assert np.isclose(rewards.sum(), 0.95)


def test_blend_default_share_is_shadow_mode():
    # SERVING_EMISSION_SHARE = 0.0 in constants: serving scores must not move rewards.
    miner_uids = {0, 1}
    with_scores = blend_emission_pools({}, {}, miner_uids, serving_scores={1: 1.0})
    without_scores = blend_emission_pools({}, {}, miner_uids)
    assert np.allclose(with_scores, without_scores)
