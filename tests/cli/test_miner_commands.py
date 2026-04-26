# Entrius 2025

"""Tests for gitt miner post and gitt miner check CLI commands."""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor import __version__
from gittensor.cli.main import cli
from gittensor.cli.miner_commands.helpers import (
    _pat_check_aggregate_counts,
    _pat_post_aggregate_counts,
    _pat_post_row_category,
)


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


class TestPatCheckAggregateCounts:
    def test_splits_valid_no_pat_invalid_and_no_response(self):
        results = [
            {'pat_valid': True, 'has_pat': True},
            {'pat_valid': False, 'has_pat': False},
            {'pat_valid': False, 'has_pat': True},
            {'pat_valid': None, 'has_pat': None},
        ]
        assert _pat_check_aggregate_counts(results) == {
            'valid': 1,
            'no_pat': 1,
            'invalid_pat': 1,
            'no_response': 1,
        }


class TestPatPostRowCategory:
    def test_accepted_true_returns_accepted(self):
        assert _pat_post_row_category({'accepted': True}) == 'accepted'

    def test_accepted_false_returns_rejected(self):
        assert _pat_post_row_category({'accepted': False}) == 'rejected'

    def test_accepted_none_returns_no_response(self):
        assert _pat_post_row_category({'accepted': None}) == 'no_response'

    def test_missing_accepted_key_returns_no_response(self):
        assert _pat_post_row_category({}) == 'no_response'


class TestPatPostAggregateCounts:
    def test_splits_accepted_rejected_and_no_response(self):
        results = [
            {'accepted': True},
            {'accepted': True},
            {'accepted': False},
            {'accepted': None},
            {'accepted': None},
        ]
        assert _pat_post_aggregate_counts(results) == {
            'accepted': 2,
            'rejected': 1,
            'no_response': 2,
        }

    def test_empty_results_returns_zero_counts(self):
        assert _pat_post_aggregate_counts([]) == {
            'accepted': 0,
            'rejected': 0,
            'no_response': 0,
        }

    def test_no_response_is_not_collapsed_into_rejected(self):
        """Regression: JSON output previously reported `rejected = total - accepted`,
        silently bucketing no_response into rejected. Counts must stay distinct."""
        results = [{'accepted': False}, {'accepted': None}]
        counts = _pat_post_aggregate_counts(results)
        assert counts['rejected'] == 1
        assert counts['no_response'] == 1
