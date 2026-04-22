# Entrius 2025

"""Tests for gitt miner post and gitt miner check CLI commands."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor import __version__
from gittensor.cli.miner_commands.check import _build_check_results
from gittensor.cli.main import cli
from gittensor.cli.miner_commands.post import _build_broadcast_results


@pytest.fixture
def runner():
    return CliRunner()


class TestMinerPost:
    def test_no_pat_prompts_interactively(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'post', '--wallet', 'test', '--hotkey', 'test'], input='')
        assert 'Enter your GitHub Personal Access Token' in result.output

    def test_no_pat_json_mode_exits(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'post', '--json-output', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output['success'] is False

    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=False)
    def test_pat_flag_used(self, mock_validate, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'post', '--pat', 'ghp_test123', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        assert 'invalid' in result.output.lower() or 'expired' in result.output.lower()
        mock_validate.assert_called_once_with('ghp_test123')

    @patch('gittensor.cli.miner_commands.post._validate_pat_locally', return_value=False)
    def test_invalid_pat_exits(self, mock_validate, runner, monkeypatch):
        monkeypatch.setenv('GITTENSOR_MINER_PAT', 'ghp_invalid')
        result = runner.invoke(cli, ['miner', 'post', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        assert 'invalid' in result.output.lower() or 'expired' in result.output.lower()

    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'post', '--help'])
        assert result.exit_code == 0
        assert 'Broadcast your GitHub PAT' in result.output

    def test_miner_alias(self, runner):
        """gitt m post should work as alias for gitt miner post."""
        result = runner.invoke(cli, ['m', 'post', '--help'])
        assert result.exit_code == 0
        assert 'Broadcast your GitHub PAT' in result.output


class TestMinerCheck:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'check', '--help'])
        assert result.exit_code == 0
        assert 'Check how many validators' in result.output

    def test_check_alias(self, runner):
        """gitt m check should work as alias for gitt miner check."""
        result = runner.invoke(cli, ['m', 'check', '--help'])
        assert result.exit_code == 0
        assert 'Check how many validators' in result.output


class TestCliVersion:
    def test_version_matches_package_version(self, runner):
        result = runner.invoke(cli, ['--version'])
        assert result.exit_code == 0
        assert result.output == f'gittensor, version {__version__}\n'


class TestMinerResponseAlignment:
    def test_build_broadcast_results_keeps_all_validators(self):
        validator_uids = [1, 2, 3]
        validator_axons = [
            SimpleNamespace(hotkey='hk1' * 16),
            SimpleNamespace(hotkey='hk2' * 16),
            SimpleNamespace(hotkey='hk3' * 16),
        ]
        responses = [SimpleNamespace(accepted=True, rejection_reason=None, dendrite=SimpleNamespace(status_code=200))]

        results, missing = _build_broadcast_results(validator_uids, validator_axons, responses)

        assert len(results) == 3
        assert missing == 2
        assert results[0]['accepted'] is True
        assert results[1]['accepted'] is None
        assert results[1]['rejection_reason'] == 'No response received from validator.'

    def test_build_check_results_keeps_all_validators(self):
        validator_uids = [10, 11]
        validator_axons = [
            SimpleNamespace(hotkey='hk10' * 12),
            SimpleNamespace(hotkey='hk11' * 12),
        ]
        responses = [SimpleNamespace(has_pat=True, pat_valid=True, rejection_reason=None)]

        results, missing = _build_check_results(validator_uids, validator_axons, responses)

        assert len(results) == 2
        assert missing == 1
        assert results[0]['pat_valid'] is True
        assert results[1]['has_pat'] is None
        assert results[1]['rejection_reason'] == 'No response received from validator.'
