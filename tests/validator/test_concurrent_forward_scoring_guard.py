# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for concurrent validator forwards sharing one step."""

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import numpy as np
import pytest

from gittensor.classes import MinerEvaluation
from gittensor.validator import forward as forward_module
from neurons.base.validator import BaseValidatorNeuron

if TYPE_CHECKING:
    from neurons.validator import Validator


class _DummyValidator:
    def __init__(self, step: int = 0, num_concurrent_forwards: int = 1):
        self.step = step
        self.config = SimpleNamespace(neuron=SimpleNamespace(num_concurrent_forwards=num_concurrent_forwards))
        self.lock = asyncio.Lock()
        self._active_scoring_round_steps: set[int] = set()
        self._last_completed_scoring_round_step: int | None = None
        self.evaluation_cache = object()
        self.stored_evaluations = []
        self.score_updates = []

    async def forward(self):
        return await forward_module.forward(cast('Validator', self))

    async def bulk_store_evaluation(self, miner_evals, master_repositories, skip_uids=None):
        self.stored_evaluations.append(
            SimpleNamespace(
                miner_evals=dict(miner_evals),
                master_repositories=master_repositories,
                skip_uids=set(skip_uids or set()),
            )
        )

    def update_scores(self, rewards, uids, blacklisted_uids=None):
        self.score_updates.append(
            SimpleNamespace(
                rewards=np.asarray(rewards).copy(),
                uids=set(uids),
                blacklisted_uids=list(blacklisted_uids or []),
            )
        )


def _install_scoring_stubs(monkeypatch, oss_side_effect=None, penalized_uids=None):
    miner_uids = {0, 1}
    miner_evaluations = {
        uid: MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}', github_id=str(uid)) for uid in miner_uids
    }
    rewards = np.array([0.25, 0.75])
    oss_calls = []

    async def fake_oss_contributions(*args):
        oss_calls.append(args)
        await asyncio.sleep(0)
        if oss_side_effect is not None:
            return await oss_side_effect(miner_evaluations)
        return miner_evaluations, set(), set(penalized_uids or {1})

    issue_discovery = AsyncMock(return_value=None)
    issue_competitions = AsyncMock(return_value=None)

    monkeypatch.setattr(forward_module, 'VALIDATOR_WAIT', 0)
    monkeypatch.setattr(forward_module, 'get_all_uids', lambda _validator: miner_uids)
    monkeypatch.setattr(forward_module, 'load_master_repo_weights', lambda: {})
    monkeypatch.setattr(forward_module, 'load_programming_language_weights', lambda: {})
    monkeypatch.setattr(forward_module, 'load_token_config', lambda: SimpleNamespace(language_configs={}))
    monkeypatch.setattr(forward_module, 'oss_contributions', fake_oss_contributions)
    monkeypatch.setattr(forward_module, 'issue_discovery', issue_discovery)
    monkeypatch.setattr(forward_module, 'issue_competitions', issue_competitions)
    monkeypatch.setattr(forward_module, 'build_maintainer_uids_by_repo', lambda *_args: {})
    monkeypatch.setattr(forward_module, 'blend_emission_pools', lambda *_args: rewards)

    return SimpleNamespace(
        miner_uids=miner_uids,
        miner_evaluations=miner_evaluations,
        rewards=rewards,
        oss_calls=oss_calls,
        issue_discovery=issue_discovery,
        issue_competitions=issue_competitions,
    )


def test_concurrent_forward_runs_scoring_round_once_per_step(monkeypatch):
    validator = _DummyValidator(step=0, num_concurrent_forwards=3)
    stubs = _install_scoring_stubs(monkeypatch)

    asyncio.run(BaseValidatorNeuron.concurrent_forward(cast(BaseValidatorNeuron, validator)))

    assert len(stubs.oss_calls) == 1
    assert stubs.issue_discovery.await_count == 1
    assert stubs.issue_competitions.await_count == 1
    assert len(validator.stored_evaluations) == 1
    assert len(validator.score_updates) == 1

    score_update = validator.score_updates[0]
    assert score_update.uids == stubs.miner_uids
    assert score_update.blacklisted_uids == [1]
    np.testing.assert_allclose(score_update.rewards, stubs.rewards)
    assert validator._active_scoring_round_steps == set()
    assert validator._last_completed_scoring_round_step == 0


def test_completed_scoring_round_does_not_block_later_interval(monkeypatch):
    validator = _DummyValidator(step=0)
    stubs = _install_scoring_stubs(monkeypatch)

    async def run_forwards():
        await validator.forward()
        await validator.forward()

        validator.step = forward_module.VALIDATOR_STEPS_INTERVAL
        await validator.forward()

    asyncio.run(run_forwards())

    assert len(stubs.oss_calls) == 2
    assert len(validator.score_updates) == 2
    assert validator._active_scoring_round_steps == set()
    assert validator._last_completed_scoring_round_step == forward_module.VALIDATOR_STEPS_INTERVAL


def test_failed_scoring_round_can_be_retried_for_same_step(monkeypatch):
    validator = _DummyValidator(step=0)
    attempts = 0

    async def flaky_oss_contributions(miner_evaluations):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('scoring failed')
        return miner_evaluations, set(), set()

    _install_scoring_stubs(monkeypatch, oss_side_effect=flaky_oss_contributions, penalized_uids=set())

    async def run_attempts():
        with pytest.raises(RuntimeError, match='scoring failed'):
            await validator.forward()
        assert validator._active_scoring_round_steps == set()
        assert validator._last_completed_scoring_round_step is None

        await validator.forward()

    asyncio.run(run_attempts())

    assert attempts == 2
    assert len(validator.score_updates) == 1
    assert validator._active_scoring_round_steps == set()
    assert validator._last_completed_scoring_round_step == 0
