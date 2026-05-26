"""get_rewards scores miners concurrently, bounded by MINER_EVALUATION_CONCURRENCY."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

reward_module = pytest.importorskip('gittensor.validator.oss_contributions.reward')
classes = pytest.importorskip('gittensor.classes')

get_rewards = reward_module.get_rewards
MinerEvaluation = classes.MinerEvaluation


def _fake_validator(uids):
    return SimpleNamespace(
        metagraph=SimpleNamespace(hotkeys={uid: f'hk{uid}' for uid in uids}),
        store_or_use_cached_evaluation=lambda evals: set(),
        evaluation_cache=SimpleNamespace(evict_many=lambda u: None),
    )


def _run_with_tracking(uids, cap):
    """Run get_rewards with a fake per-miner scorer that records how many
    evaluations are in flight at once. Returns (evaluations, max_in_flight)."""
    state = {'current': 0, 'max': 0}

    async def fake_score(uid, hotkey, *args, **kwargs):
        state['current'] += 1
        state['max'] = max(state['max'], state['current'])
        await asyncio.sleep(0.02)
        state['current'] -= 1
        return MinerEvaluation(uid=uid, hotkey=hotkey, github_id=str(uid))

    with (
        patch.object(reward_module.pat_storage, 'load_all_pats', return_value=[]),
        patch.object(reward_module, 'MINER_EVALUATION_CONCURRENCY', cap),
        patch.object(reward_module, 'evaluate_miners_pull_requests', side_effect=fake_score),
        patch.object(reward_module, 'detect_and_penalize_miners_sharing_github', return_value=set()),
        patch.object(reward_module, 'finalize_miner_scores'),
    ):
        evaluations, _cached, _penalized = asyncio.run(
            get_rewards(
                _fake_validator(uids), uids, master_repositories={}, programming_languages={}, token_config=None
            )
        )
    return evaluations, state['max']


def test_every_miner_is_evaluated():
    uids = set(range(6))
    evaluations, _ = _run_with_tracking(uids, cap=3)
    assert set(evaluations) == uids
    assert all(evaluations[uid].uid == uid for uid in uids)


def test_miners_run_in_parallel():
    """With headroom, more than one evaluation is in flight at once."""
    _, max_in_flight = _run_with_tracking(set(range(6)), cap=4)
    assert max_in_flight > 1


def test_concurrency_is_bounded_by_the_cap():
    """In-flight evaluations never exceed the configured cap."""
    _, max_in_flight = _run_with_tracking(set(range(8)), cap=2)
    assert max_in_flight == 2
