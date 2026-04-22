# Entrius 2025

"""Tests for `gitt miner score` (validator-pipeline e2e for one miner)."""

import json
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.cli.miner_commands.score import _DEV_HOTKEY, _DEV_UID


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def miner_eval_factory():
    """Build a populated MinerEvaluation for a UID without running real scoring."""

    def _make(uid: int = 5, hotkey: str = 'dev', github_id: str = '12345', failed_reason=None, **overrides):
        from gittensor.classes import MinerEvaluation

        evaluation = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id, failed_reason=failed_reason)
        for key, value in overrides.items():
            setattr(evaluation, key, value)
        return evaluation

    return _make


def _patch_pipeline(
    uid: int,
    miner_evaluation,
    oss_value: float = 0.4,
    issue_value: float = 0.1,
    blended: float = 0.3,
    oss_side_effect=None,
):
    """Mock the three forward entry points to return controlled values.

    Pass `oss_side_effect=...` to inject a custom oss_contributions impl (used by
    tests that want to capture call args); otherwise a default AsyncMock returns
    the supplied `oss_value`/`miner_evaluation` tuple.
    """
    miner_evaluations = {uid: miner_evaluation}

    oss_rewards = np.array([oss_value])
    issue_rewards = np.array([issue_value])
    final_rewards = np.array([blended])

    if oss_side_effect is not None:
        oss_patch = patch('gittensor.validator.forward.oss_contributions', side_effect=oss_side_effect)
    else:
        oss_patch = patch(
            'gittensor.validator.forward.oss_contributions',
            new=AsyncMock(return_value=(oss_rewards, miner_evaluations, set(), set())),
        )

    return [
        oss_patch,
        patch('gittensor.validator.forward.issue_discovery', new=AsyncMock(return_value=issue_rewards)),
        patch('gittensor.validator.forward.blend_emission_pools', return_value=final_rewards),
        patch('gittensor.validator.utils.load_weights.load_master_repo_weights', return_value={}),
        patch('gittensor.validator.utils.load_weights.load_programming_language_weights', return_value={}),
        patch('gittensor.validator.utils.load_weights.load_token_config', return_value=_stub_token_config()),
    ]


def _stub_token_config():
    from gittensor.validator.utils.load_weights import TokenConfig

    return TokenConfig(structural_bonus={}, leaf_tokens={}, language_configs={})


class TestScoreCommand:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'score', '--help'])
        assert result.exit_code == 0
        assert 'validator scoring pipeline' in result.output

    def test_missing_pat_exits(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'score'])
        assert result.exit_code == 1
        assert 'GITTENSOR_MINER_PAT' in result.output

    def test_e2e_table_output(self, runner, miner_eval_factory):
        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            is_eligible=True,
            credibility=0.9,
            base_total_score=20.0,
            total_score=18.0,
            total_token_score=42.0,
            unique_repos_count=2,
        )
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        assert f'Miner UID {_DEV_UID}' in result.output
        assert 'Total earned score' in result.output
        assert 'Final blended reward' in result.output

    def test_e2e_json_output(self, runner, miner_eval_factory):
        evaluation = miner_eval_factory(
            uid=_DEV_UID,
            is_eligible=True,
            credibility=0.85,
            base_total_score=20.0,
            total_score=18.0,
        )
        with _multi_patch(
            _patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation, oss_value=0.4, issue_value=0.1, blended=0.3)
        ):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--json-output'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload['success'] is True
        assert payload['miner_evaluation']['uid'] == _DEV_UID
        assert payload['miner_evaluation']['is_eligible'] is True
        assert payload['miner_evaluation']['credibility'] == 0.85
        assert payload['rewards']['oss_normalized'] == 0.4
        assert payload['rewards']['issue_discovery_normalized'] == 0.1
        assert payload['rewards']['blended_final'] == 0.3

    def test_pat_never_appears_in_json(self, runner, miner_eval_factory):
        """The introspection-based serializer relies on _EVAL_SKIP to redact secrets;
        guard against it accidentally leaking github_pat into JSON output."""
        evaluation = miner_eval_factory(uid=_DEV_UID, github_pat='ghp_should_not_leak')
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--pat', 'ghp_should_not_leak', '--json-output'],
                env={},
            )
        assert result.exit_code == 0, result.output
        assert 'ghp_should_not_leak' not in result.output
        payload = json.loads(result.output)
        assert 'github_pat' not in payload['miner_evaluation']

    def test_failed_reason_renders_in_table(self, runner, miner_eval_factory):
        evaluation = miner_eval_factory(uid=_DEV_UID, failed_reason=f'No stored PAT for miner {_DEV_UID}')
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        assert 'failed_reason' in result.output
        assert 'No stored PAT' in result.output

    def test_failed_reason_in_json(self, runner, miner_eval_factory):
        evaluation = miner_eval_factory(uid=_DEV_UID, failed_reason='whatever')
        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation)):
            result = runner.invoke(
                cli,
                ['miner', 'score', '--json-output'],
                env={'GITTENSOR_MINER_PAT': 'ghp_dummy'},
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload['miner_evaluation']['failed_reason'] == 'whatever'

    def test_pipeline_called_with_stub_validator(self, runner, miner_eval_factory):
        """The dev tool must wire stub `self.metagraph.hotkeys[uid]` for get_rewards."""
        evaluation = miner_eval_factory(uid=_DEV_UID, hotkey=_DEV_HOTKEY, github_id='99')
        captured = {}

        async def _capture_oss(self, miner_uids, *args, **kwargs):
            captured['hotkey_at_uid'] = self.metagraph.hotkeys[_DEV_UID]
            captured['miner_uids'] = miner_uids
            return np.array([0.0]), {_DEV_UID: evaluation}, set(), set()

        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation, oss_side_effect=_capture_oss)):
            result = runner.invoke(cli, ['miner', 'score'], env={'GITTENSOR_MINER_PAT': 'ghp_dummy'})
        assert result.exit_code == 0, result.output
        assert captured['hotkey_at_uid'] == _DEV_HOTKEY
        assert captured['miner_uids'] == {_DEV_UID}

    def test_pat_storage_load_all_pats_is_patched(self, runner, miner_eval_factory):
        """The injected PAT snapshot must override pat_storage.load_all_pats()."""
        evaluation = miner_eval_factory(uid=_DEV_UID)
        captured = {}

        async def _capture_oss(self, miner_uids, *args, **kwargs):
            from gittensor.validator.oss_contributions.reward import pat_storage

            captured['pats'] = pat_storage.load_all_pats()
            return np.array([0.0]), {_DEV_UID: evaluation}, set(), set()

        with _multi_patch(_patch_pipeline(uid=_DEV_UID, miner_evaluation=evaluation, oss_side_effect=_capture_oss)):
            result = runner.invoke(cli, ['miner', 'score', '--pat', 'ghp_injected'], env={})
        assert result.exit_code == 0, result.output
        assert captured['pats'] == [{'uid': _DEV_UID, 'hotkey': _DEV_HOTKEY, 'pat': 'ghp_injected'}]


def _multi_patch(patches):
    """contextlib.ExitStack-style stack for a flat list of patch context managers."""
    import contextlib

    stack = contextlib.ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack
