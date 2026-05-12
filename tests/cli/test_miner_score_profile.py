"""Tests for --profile flag on `gitt miner score`."""

from unittest.mock import patch

import pytest

from gittensor.cli.miner_commands.score import _DEV_UID


@pytest.fixture
def runner():
    from click.testing import CliRunner

    return CliRunner()


def _patch_pipeline():
    import numpy as np

    from gittensor.classes import MinerEvaluation

    evaluation = MinerEvaluation(uid=_DEV_UID, hotkey='dev')
    miner_evaluations = {_DEV_UID: evaluation}
    oss_rewards = np.array([0.5])
    issue_rewards = np.array([0.1])
    final_rewards = np.array([0.3])
    return [
        patch(
            'gittensor.validator.forward.oss_contributions', return_value=(oss_rewards, miner_evaluations, set(), set())
        ),
        patch('gittensor.validator.forward.issue_discovery', return_value=issue_rewards),
        patch('gittensor.validator.forward.blend_emission_pools', return_value=final_rewards),
        patch('gittensor.validator.utils.load_weights.load_master_repo_weights', return_value={}),
        patch('gittensor.validator.utils.load_weights.load_programming_language_weights', return_value={}),
        patch('gittensor.validator.utils.load_weights.load_token_config'),
    ]


class TestProfileFlag:
    def test_profile_writes_valid_pstats(self, runner, tmp_path):
        """--profile PATH writes a valid pstats binary."""
        import pstats

        prof_file = tmp_path / 'run.prof'
        with _multi_patch(_patch_pipeline()):
            from gittensor.cli.main import cli

            result = runner.invoke(
                cli, ['miner', 'score', '--profile', str(prof_file)], env={'GITTENSOR_MINER_PAT': 'ghp_d'}
            )

        assert result.exit_code == 0, result.output
        assert prof_file.exists()
        pstats.Stats(str(prof_file))
        assert prof_file.stat().st_size > 0

    def test_default_no_profile_side_effects(self, runner):
        """Default invocation has no profile output."""
        with _multi_patch(_patch_pipeline()):
            from gittensor.cli.main import cli

            result = runner.invoke(cli, ['miner', 'score'], env={'GITTENSOR_MINER_PAT': 'ghp_d'})

        assert result.exit_code == 0, result.output
        assert 'pstats' not in result.output
        assert 'pstats' not in (result.stderr or '')


def _multi_patch(patches):
    import contextlib

    stack = contextlib.ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack
