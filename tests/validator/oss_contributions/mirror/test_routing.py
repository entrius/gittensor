"""Integration test for evaluate_miners_pull_requests.

Verifies that:
- Mirror path is called with all repos
- Init failure short-circuits
- Identity-fetch transient failure short-circuits to cache fallback
"""

import asyncio
from unittest.mock import patch

import pytest

reward_module = pytest.importorskip('gittensor.validator.oss_contributions.reward')
classes = pytest.importorskip('gittensor.classes')
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')

evaluate_miners_pull_requests = reward_module.evaluate_miners_pull_requests
MinerEvaluation = classes.MinerEvaluation
RepositoryConfig = load_weights.RepositoryConfig
TokenConfig = load_weights.TokenConfig


def _make_miner_eval(uid=1, hotkey='hk', github_id='218712309', failed_reason=None):
    me = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id)
    me.failed_reason = failed_reason
    return me


def _configs():
    return {
        'entrius/gittensor-ui': RepositoryConfig(weight=0.5),
        'entrius/allways': RepositoryConfig(weight=0.5),
    }


def _run(coro):
    return asyncio.run(coro)


def test_load_and_score_run_with_all_repos():
    """load + score fire once with the full master repo set."""
    with (
        patch.object(reward_module, 'validate_response_and_initialize_miner_evaluation') as mock_init,
        patch.object(reward_module, 'load_miner_prs') as mock_load,
        patch.object(reward_module, 'score_miner_prs') as mock_score,
    ):
        mock_init.return_value = _make_miner_eval()

        _run(
            evaluate_miners_pull_requests(
                uid=1,
                hotkey='hk',
                pat='fake-pat',
                master_repositories=_configs(),
                programming_languages={},
                token_config=TokenConfig(),
            )
        )

        mock_load.assert_called_once()
        passed = mock_load.call_args.args[1]
        assert set(passed.keys()) == {'entrius/gittensor-ui', 'entrius/allways'}
        mock_score.assert_called_once()


def test_failed_init_short_circuits():
    """If validate_response fails, mirror path never runs."""
    me = _make_miner_eval(failed_reason='stale hotkey')

    with (
        patch.object(reward_module, 'validate_response_and_initialize_miner_evaluation', return_value=me),
        patch.object(reward_module, 'load_miner_prs') as mock_mirror_load,
    ):
        result = _run(
            evaluate_miners_pull_requests(
                uid=1,
                hotkey='hk',
                pat='fake-pat',
                master_repositories=_configs(),
                programming_languages={},
                token_config=TokenConfig(),
            )
        )

        assert result is me
        mock_mirror_load.assert_not_called()


def test_identity_fetch_failure_short_circuits_to_cache_fallback():
    """Transient /user failure should not trigger a mirror fetch."""
    me = _make_miner_eval(github_id='12345')
    me.github_pr_fetch_failed = True

    with (
        patch.object(reward_module, 'validate_response_and_initialize_miner_evaluation', return_value=me),
        patch.object(reward_module, 'load_miner_prs') as mock_mirror_load,
    ):
        result = _run(
            evaluate_miners_pull_requests(
                uid=1,
                hotkey='hk',
                pat='fake-pat',
                master_repositories=_configs(),
                programming_languages={},
                token_config=TokenConfig(),
            )
        )

        assert result is me
        assert result.should_use_cache_fallback is True
        mock_mirror_load.assert_not_called()
