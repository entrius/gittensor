"""Integration test for evaluate_miners_pull_requests with mirror routing.

Verifies that:
- Legacy path is called with legacy-only repos
- Mirror path is called with mirror-only repos
- Both paths populate the same MinerEvaluation after combine
- Either path can be empty without breaking the other
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
        'entrius/gittensor-ui': RepositoryConfig(weight=0.5, mirror_enabled=True),
        'entrius/allways': RepositoryConfig(weight=0.5, mirror_enabled=True),
        'other/legacy-repo': RepositoryConfig(weight=0.3, mirror_enabled=False),
        'third/legacy-repo': RepositoryConfig(weight=0.2),  # default False
    }


def _run(coro):
    return asyncio.run(coro)


def test_partitions_and_calls_both_paths():
    """Both sides fire with their correct subset of repos."""
    with (
        patch.object(reward_module, 'validate_response_and_initialize_miner_evaluation') as mock_init,
        patch.object(reward_module, 'load_miners_prs') as mock_legacy_load,
        patch.object(reward_module, 'score_miner_prs') as mock_legacy_score,
        patch.object(reward_module, 'load_mirror_miner_prs') as mock_mirror_load,
        patch.object(reward_module, 'score_mirror_miner_prs') as mock_mirror_score,
        patch.object(reward_module, 'combine') as mock_combine,
    ):
        mock_init.return_value = _make_miner_eval()
        mock_combine.side_effect = lambda legacy_eval, mirror_eval: legacy_eval

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

        mock_legacy_load.assert_called_once()
        legacy_repos_passed = mock_legacy_load.call_args.args[1]
        assert set(legacy_repos_passed.keys()) == {'other/legacy-repo', 'third/legacy-repo'}
        mock_legacy_score.assert_called_once()

        mock_mirror_load.assert_called_once()
        mirror_repos_passed = mock_mirror_load.call_args.args[1]
        assert set(mirror_repos_passed.keys()) == {'entrius/gittensor-ui', 'entrius/allways'}
        mock_mirror_score.assert_called_once()

        mock_combine.assert_called_once()
        assert result.github_pat is None


def test_all_legacy_repos_skips_mirror_path():
    """If no repos are mirror_enabled, mirror load/score/combine are never called."""
    configs = {
        'a/legacy': RepositoryConfig(weight=0.5, mirror_enabled=False),
        'b/legacy': RepositoryConfig(weight=0.3),
    }

    with (
        patch.object(reward_module, 'validate_response_and_initialize_miner_evaluation') as mock_init,
        patch.object(reward_module, 'load_miners_prs') as mock_legacy_load,
        patch.object(reward_module, 'score_miner_prs'),
        patch.object(reward_module, 'load_mirror_miner_prs') as mock_mirror_load,
        patch.object(reward_module, 'score_mirror_miner_prs') as mock_mirror_score,
        patch.object(reward_module, 'combine') as mock_combine,
    ):
        mock_init.return_value = _make_miner_eval()

        _run(
            evaluate_miners_pull_requests(
                uid=1,
                hotkey='hk',
                pat='fake-pat',
                master_repositories=configs,
                programming_languages={},
                token_config=TokenConfig(),
            )
        )

        mock_legacy_load.assert_called_once()
        mock_mirror_load.assert_not_called()
        mock_mirror_score.assert_not_called()
        mock_combine.assert_not_called()


def test_all_mirror_repos_skips_legacy_path():
    """If every repo is mirror_enabled, legacy load/score are never called."""
    configs = {
        'entrius/a': RepositoryConfig(weight=0.5, mirror_enabled=True),
        'entrius/b': RepositoryConfig(weight=0.5, mirror_enabled=True),
    }

    with (
        patch.object(reward_module, 'validate_response_and_initialize_miner_evaluation') as mock_init,
        patch.object(reward_module, 'load_miners_prs') as mock_legacy_load,
        patch.object(reward_module, 'score_miner_prs') as mock_legacy_score,
        patch.object(reward_module, 'load_mirror_miner_prs') as mock_mirror_load,
        patch.object(reward_module, 'score_mirror_miner_prs'),
        patch.object(reward_module, 'combine') as mock_combine,
    ):
        mock_init.return_value = _make_miner_eval()
        mock_combine.side_effect = lambda legacy_eval, mirror_eval: legacy_eval

        _run(
            evaluate_miners_pull_requests(
                uid=1,
                hotkey='hk',
                pat='fake-pat',
                master_repositories=configs,
                programming_languages={},
                token_config=TokenConfig(),
            )
        )

        mock_legacy_load.assert_not_called()
        mock_legacy_score.assert_not_called()
        mock_mirror_load.assert_called_once()
        mock_combine.assert_called_once()


def test_failed_init_short_circuits():
    """If validate_response fails, neither path runs."""
    me = _make_miner_eval(failed_reason='stale hotkey')

    with (
        patch.object(reward_module, 'validate_response_and_initialize_miner_evaluation', return_value=me),
        patch.object(reward_module, 'load_miners_prs') as mock_legacy_load,
        patch.object(reward_module, 'load_mirror_miner_prs') as mock_mirror_load,
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
        mock_legacy_load.assert_not_called()
        mock_mirror_load.assert_not_called()
