# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for merge prediction CLI commands (submissions and predict).

Covers: REST fallback, contract resolution, prediction validation,
and end-to-end CLI invocation via CliRunner.
"""

import json
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.predictions import (
    _fetch_prs_rest,
    _resolve_issue_from_contract,
    _validate_predictions,
)

# =============================================================================
# Sample data
# =============================================================================

SAMPLE_PRS = [
    {
        'number': 10,
        'title': 'Fix authentication bug',
        'state': 'OPEN',
        'createdAt': '2025-12-01T10:00:00Z',
        'url': 'https://github.com/owner/repo/pull/10',
        'author': 'alice',
        'baseRepository': 'owner/repo',
        'reviewDecision': 'APPROVED',
        'reviewCount': 2,
    },
    {
        'number': 20,
        'title': 'Refactor user module',
        'state': 'OPEN',
        'createdAt': '2025-12-05T10:00:00Z',
        'url': 'https://github.com/owner/repo/pull/20',
        'author': 'bob',
        'baseRepository': 'owner/repo',
        'reviewDecision': None,
        'reviewCount': 0,
    },
]

SAMPLE_ISSUE = {
    'id': 1,
    'repository_full_name': 'owner/repo',
    'issue_number': 42,
    'bounty_amount': 100_000_000_000,
    'target_bounty': 200_000_000_000,
    'status': 'Active',
}

CONTRACT_ADDR = '5FWNdk8YNtNcHKrAx2krqenFrFAZG7vmsd2XN2isJSew3MrD'


# =============================================================================
# _fetch_prs_rest
# =============================================================================


class TestFetchPrsRest:
    def test_parses_search_results(self):
        search_response = {
            'items': [
                {
                    'number': 10,
                    'title': 'Fix bug',
                    'created_at': '2025-12-01',
                    'html_url': 'https://github.com/o/r/pull/10',
                    'user': {'login': 'alice'},
                    'pull_request': {'url': '...'},
                },
                {
                    'number': 99,
                    'title': 'Not a PR - just an issue',
                    'created_at': '2025-12-02',
                    'html_url': 'https://github.com/o/r/issues/99',
                    'user': {'login': 'bob'},
                    # no 'pull_request' key -> should be filtered out
                },
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(search_response).encode()

        with patch('urllib.request.urlopen', return_value=mock_resp):
            prs = _fetch_prs_rest('owner/repo', 42)

        assert len(prs) == 1
        assert prs[0]['number'] == 10
        assert prs[0]['author'] == 'alice'

    def test_http_error_returns_empty(self):
        import urllib.error

        with patch(
            'urllib.request.urlopen',
            side_effect=urllib.error.HTTPError(None, 403, 'Forbidden', {}, None),
        ):
            prs = _fetch_prs_rest('owner/repo', 42)
        assert prs == []

    def test_network_error_returns_empty(self):
        import urllib.error

        with patch(
            'urllib.request.urlopen',
            side_effect=urllib.error.URLError('timeout'),
        ):
            prs = _fetch_prs_rest('owner/repo', 42)
        assert prs == []


# =============================================================================
# _resolve_issue_from_contract
# =============================================================================


class TestResolveIssueFromContract:
    def test_missing_contract_raises(self):
        with patch(
            'gittensor.cli.issue_commands.predictions.get_contract_address',
            return_value='',
        ):
            with pytest.raises(click.ClickException, match='Contract address not configured'):
                _resolve_issue_from_contract(1, None, None, '', False, False)

    def test_issue_not_found_returns_none(self):
        with (
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=CONTRACT_ADDR,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=[],
            ),
        ):
            result = _resolve_issue_from_contract(999, None, None, '', False, False)
        assert result is None

    def test_active_issue_returned(self):
        with (
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=CONTRACT_ADDR,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=[SAMPLE_ISSUE],
            ),
        ):
            result = _resolve_issue_from_contract(1, None, None, '', False, False)
        assert result is not None
        assert result['id'] == 1
        assert result['status'] == 'Active'

    def test_non_active_issue_warns(self):
        completed_issue = {**SAMPLE_ISSUE, 'status': 'Completed'}
        with (
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=CONTRACT_ADDR,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=[completed_issue],
            ),
            patch('gittensor.cli.issue_commands.predictions.console') as mock_console,
        ):
            result = _resolve_issue_from_contract(1, None, None, '', False, False)
        assert result is not None
        # Should have printed a warning
        printed = str(mock_console.print.call_args_list)
        assert 'Completed' in printed or 'Warning' in printed


# =============================================================================
# _validate_predictions
# =============================================================================


class TestValidatePredictions:
    def test_valid_predictions_pass(self):
        _validate_predictions({10: 0.6, 20: 0.3}, SAMPLE_PRS)

    def test_probability_below_zero_rejected(self):
        with pytest.raises(click.ClickException, match='between 0.0 and 1.0'):
            _validate_predictions({10: -0.1}, SAMPLE_PRS)

    def test_probability_above_one_rejected(self):
        with pytest.raises(click.ClickException, match='between 0.0 and 1.0'):
            _validate_predictions({10: 1.5}, SAMPLE_PRS)

    def test_sum_exceeds_one_rejected(self):
        with pytest.raises(click.ClickException, match='exceeds 1.0'):
            _validate_predictions({10: 0.7, 20: 0.5}, SAMPLE_PRS)

    def test_pr_not_in_list_rejected(self):
        with pytest.raises(click.ClickException, match='not in the list'):
            _validate_predictions({999: 0.5}, SAMPLE_PRS)

    def test_boundary_sum_exactly_one_passes(self):
        _validate_predictions({10: 0.5, 20: 0.5}, SAMPLE_PRS)

    def test_single_zero_prediction_passes(self):
        _validate_predictions({10: 0.0}, SAMPLE_PRS)


# =============================================================================
# CLI: submissions
# =============================================================================


def _get_cli_root():
    """Return the root Click group with issues commands registered."""
    try:
        from gittensor.cli.main import cli

        return cli
    except ImportError:
        from gittensor.cli.issue_commands import register_commands

        root = click.Group()
        register_commands(root)
        return root


@pytest.fixture
def cli_root():
    return _get_cli_root()


@pytest.fixture
def runner():
    return CliRunner()


class TestCliSubmissions:
    def test_table_output(self, cli_root, runner):
        with (
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=CONTRACT_ADDR,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=[SAMPLE_ISSUE],
            ),
            patch(
                'gittensor.cli.issue_commands.predictions._fetch_open_prs_for_issue',
                return_value=SAMPLE_PRS,
            ),
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '1'],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert 'PR #' in result.output or '10' in result.output

    def test_json_output(self, cli_root, runner):
        with (
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=CONTRACT_ADDR,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=[SAMPLE_ISSUE],
            ),
            patch(
                'gittensor.cli.issue_commands.predictions._fetch_open_prs_for_issue',
                return_value=SAMPLE_PRS,
            ),
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '1', '--json'],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['issue_id'] == 1
        assert len(data['open_prs']) == 2

    def test_no_prs_found(self, cli_root, runner):
        with (
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=CONTRACT_ADDR,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=[SAMPLE_ISSUE],
            ),
            patch(
                'gittensor.cli.issue_commands.predictions._fetch_open_prs_for_issue',
                return_value=[],
            ),
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '1'],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert 'No open PRs' in result.output

    def test_missing_contract_fails(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.predictions.get_contract_address',
            return_value='',
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '1'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Contract address not configured' in result.output


# =============================================================================
# CLI: predict
# =============================================================================


class TestCliPredict:
    def _mock_predict_deps(self):
        """Return a context manager stack that mocks all predict dependencies."""
        mock_wallet = MagicMock()
        mock_wallet.hotkey.ss58_address = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'

        return (
            patch.dict('os.environ', {'GITTENSOR_MINER_PAT': 'ghp_test'}),
            patch(
                'gittensor.cli.issue_commands.predictions._verify_miner_registered',
                return_value=mock_wallet,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=CONTRACT_ADDR,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=[SAMPLE_ISSUE],
            ),
            patch(
                'gittensor.cli.issue_commands.predictions._fetch_open_prs_for_issue',
                return_value=SAMPLE_PRS,
            ),
        )

    def test_single_pr_mode(self, cli_root, runner):
        patches = self._mock_predict_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                cli_root,
                ['issues', 'predict', '--id', '1', '--pr', '10', '--probability', '0.8', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert 'Prediction prepared' in result.output or '"10"' in result.output

    def test_json_input_mode(self, cli_root, runner):
        patches = self._mock_predict_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                cli_root,
                [
                    'issues',
                    'predict',
                    '--id',
                    '1',
                    '--json-input',
                    '{"10": 0.6, "20": 0.3}',
                    '-y',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert '"10"' in result.output

    def test_pr_and_json_input_mutual_exclusion(self, cli_root, runner):
        patches = self._mock_predict_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                cli_root,
                [
                    'issues',
                    'predict',
                    '--id',
                    '1',
                    '--pr',
                    '10',
                    '--probability',
                    '0.5',
                    '--json-input',
                    '{"10": 0.5}',
                    '-y',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Cannot use' in result.output

    def test_pr_without_probability_fails(self, cli_root, runner):
        patches = self._mock_predict_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                cli_root,
                ['issues', 'predict', '--id', '1', '--pr', '10', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'must be used together' in result.output

    def test_probability_validation_rejects_sum_over_one(self, cli_root, runner):
        patches = self._mock_predict_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                cli_root,
                [
                    'issues',
                    'predict',
                    '--id',
                    '1',
                    '--json-input',
                    '{"10": 0.7, "20": 0.5}',
                    '-y',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'exceeds 1.0' in result.output

    def test_missing_pat_fails(self, cli_root, runner):
        with (
            patch.dict('os.environ', {}, clear=True),
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=CONTRACT_ADDR,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions._get_github_token',
                return_value=None,
            ),
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'predict', '--id', '1', '--pr', '10', '--probability', '0.5', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'GITTENSOR_MINER_PAT' in result.output

    def test_json_output_mode(self, cli_root, runner):
        patches = self._mock_predict_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                cli_root,
                [
                    'issues',
                    'predict',
                    '--id',
                    '1',
                    '--pr',
                    '10',
                    '--probability',
                    '0.5',
                    '-y',
                    '--json',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['issue_id'] == 1
        assert '10' in data['predictions']

    def test_stub_payload_contains_expected_fields(self, cli_root, runner):
        patches = self._mock_predict_deps()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(
                cli_root,
                [
                    'issues',
                    'predict',
                    '--id',
                    '1',
                    '--pr',
                    '10',
                    '--probability',
                    '0.75',
                    '-y',
                    '--json',
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['issue_id'] == 1
        assert data['repository'] == 'owner/repo'
        assert data['predictions']['10'] == 0.75
