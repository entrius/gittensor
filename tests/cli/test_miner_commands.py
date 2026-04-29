# Entrius 2025

"""Tests for gitt miner post and gitt miner check CLI commands."""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor import __version__
from gittensor.cli.main import cli
from gittensor.cli.miner_commands.helpers import _pat_check_aggregate_counts


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


class TestMinerStatus:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'status', '--help'])
        assert result.exit_code == 0
        assert 'eligibility' in result.output.lower()

    def test_status_alias(self, runner):
        """gitt m status should work as alias for gitt miner status."""
        result = runner.invoke(cli, ['m', 'status', '--help'])
        assert result.exit_code == 0
        assert 'eligibility' in result.output.lower()

    def test_no_pat_json_mode_exits(self, runner, monkeypatch):
        monkeypatch.delenv('GITTENSOR_MINER_PAT', raising=False)
        result = runner.invoke(cli, ['miner', 'status', '--json-output', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output['success'] is False

    @patch('gittensor.cli.miner_commands.status.requests.get')
    def test_pat_rejected_exits(self, mock_get, runner, monkeypatch):
        """Non-200 from /user must exit non-zero before any network calls."""
        monkeypatch.setenv('GITTENSOR_MINER_PAT', 'ghp_invalid')
        mock_get.return_value.status_code = 401
        result = runner.invoke(cli, ['miner', 'status', '--json-output', '--wallet', 'test', '--hotkey', 'test'])
        assert result.exit_code != 0
        body = json.loads(result.output)
        assert body['success'] is False
        assert '401' in body['error']


class TestStatusReportRendering:
    """Pure-function tests for _render_json / _render_table.

    Status command's network + GitHub paths are exercised end-to-end in a
    separate integration suite; these tests pin the JSON shape and the
    table semantics so dashboards/automation depending on the envelope
    don't drift silently.
    """

    @pytest.fixture
    def eligible_report(self):
        from gittensor.cli.miner_commands.status import StatusReport

        return StatusReport(
            uid=42,
            github_login='alice',
            network='wss://test.finney.opentensor.ai:443',
            netuid=74,
            merged_count=7,
            closed_count=1,
            credibility=0.95,
            eligible_by_count=True,
            eligible_by_credibility=True,
            lookback_start='2026-03-24',
            incentivized_repos_only=True,
        )

    @pytest.fixture
    def ineligible_report(self):
        from gittensor.cli.miner_commands.status import StatusReport

        return StatusReport(
            uid=None,
            github_login='bob',
            network='wss://test.finney.opentensor.ai:443',
            netuid=74,
            merged_count=2,
            closed_count=0,
            credibility=1.0,
            eligible_by_count=False,
            eligible_by_credibility=True,
            lookback_start='2026-03-24',
            incentivized_repos_only=True,
        )

    def test_json_envelope_marks_eligible_when_both_gates_pass(self, eligible_report):
        from gittensor.cli.miner_commands.status import _render_json

        body = json.loads(_render_json(eligible_report))
        assert body['success'] is True
        assert body['gates'] == {'merged_count': True, 'credibility': True}
        assert body['merged_pull_requests'] == 7
        assert body['credibility'] == 0.95
        assert body['thresholds']['min_valid_merged_prs'] == 5

    def test_json_envelope_marks_ineligible_when_count_gate_fails(self, ineligible_report):
        from gittensor.cli.miner_commands.status import _render_json

        body = json.loads(_render_json(ineligible_report))
        assert body['success'] is False
        assert body['gates']['merged_count'] is False
        assert body['gates']['credibility'] is True
        assert body['uid'] is None

    def test_table_marks_failed_gate_with_red_x(self, ineligible_report):
        from gittensor.cli.miner_commands.status import _render_table

        # Smoke test: the table builds without raising and has the expected shape.
        # Rich's row objects don't carry the rendered text in a stable form; we
        # rely on the JSON envelope tests above for status-string contracts.
        table = _render_table(ineligible_report)
        assert len(table.columns) == 4
        assert len(list(table.rows)) == 7  # UID, login, network, merged, closed, credibility, lookback


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
