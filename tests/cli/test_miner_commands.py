# Entrius 2025

"""Tests for gitt miner post and gitt miner check CLI commands."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gittensor import __version__
from gittensor.cli.main import cli
from gittensor.cli.miner_commands.helpers import _get_validator_axons


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


def _make_metagraph(vtrusts, servings):
    """Build a MagicMock metagraph with the given per-UID vtrust and serving flags."""
    mg = MagicMock()
    mg.n = len(vtrusts)
    mg.validator_trust = vtrusts
    axons = []
    for s in servings:
        ax = MagicMock()
        ax.is_serving = s
        axons.append(ax)
    mg.axons = axons
    return mg


class TestGetValidatorAxons:
    def test_default_threshold_filters_low_trust(self):
        """Default threshold (0.1) excludes validators with vtrust <= 0.1."""
        mg = _make_metagraph(vtrusts=[0.0, 0.05, 0.2], servings=[True, True, True])
        _, uids = _get_validator_axons(mg)
        assert uids == [2]

    def test_default_threshold_excludes_non_serving(self):
        """Non-serving axons are excluded even when vtrust passes."""
        mg = _make_metagraph(vtrusts=[0.5, 0.5], servings=[True, False])
        _, uids = _get_validator_axons(mg)
        assert uids == [0]

    def test_negative_threshold_bypasses_filter(self):
        """Passing min_vtrust=-1.0 admits any serving validator.

        This is the testnet escape hatch: on networks where consensus has
        not yet assigned vtrust, the default 0.1 threshold would exclude
        every validator.
        """
        mg = _make_metagraph(vtrusts=[0.0, 0.0, 0.0], servings=[True, True, False])
        _, uids = _get_validator_axons(mg, min_vtrust=-1.0)
        assert uids == [0, 1]

    def test_custom_threshold(self):
        """Caller can request an arbitrary threshold."""
        mg = _make_metagraph(vtrusts=[0.05, 0.15, 0.25, 0.35], servings=[True] * 4)
        _, uids = _get_validator_axons(mg, min_vtrust=0.2)
        assert uids == [2, 3]


class TestMinerPostIgnoreVtrust:
    def test_help_mentions_flag(self, runner):
        result = runner.invoke(cli, ['miner', 'post', '--help'])
        assert result.exit_code == 0
        assert '--ignore-vtrust' in result.output


class TestMinerCheckIgnoreVtrust:
    def test_help_mentions_flag(self, runner):
        result = runner.invoke(cli, ['miner', 'check', '--help'])
        assert result.exit_code == 0
        assert '--ignore-vtrust' in result.output
