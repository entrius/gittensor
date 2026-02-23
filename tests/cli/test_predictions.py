# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for the submissions and predict CLI commands.

Covers: submissions display, JSON output, predict validation (interactive,
non-interactive, batch), probability validation, and CLI wiring.
All tests mock network/contract calls — no live APIs.
"""

import json
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.helpers import (
    _get_pr_review_status,
    _github_headers,
    fetch_open_prs_for_issue,
)
from gittensor.cli.issue_commands.predictions import (
    _build_pr_table,
    _resolve_issue_and_prs,
    _validate_pr_belongs_to_issue,
    _validate_probability,
    issues_predict,
    issues_submissions,
)


# =============================================================================
# Fixtures
# =============================================================================

FAKE_CONTRACT = '5FakeContractAddress1234567890123456789012'

FAKE_ISSUES = [
    {
        'id': 42,
        'repository_full_name': 'owner/repo',
        'issue_number': 100,
        'bounty_amount': 100_000_000_000,
        'target_bounty': 100_000_000_000,
        'status': 'Active',
    },
    {
        'id': 99,
        'repository_full_name': 'owner/repo',
        'issue_number': 200,
        'bounty_amount': 50_000_000_000,
        'target_bounty': 100_000_000_000,
        'status': 'Completed',
    },
]

FAKE_PRS = [
    {
        'number': 101,
        'title': 'Fix the bug',
        'author': 'alice',
        'created_at': '2025-06-01T12:00:00Z',
        'review_status': 'APPROVED',
        'url': 'https://github.com/owner/repo/pull/101',
        'html_url': 'https://github.com/owner/repo/pull/101',
    },
    {
        'number': 103,
        'title': 'Another approach',
        'author': 'bob',
        'created_at': '2025-06-02T12:00:00Z',
        'review_status': 'PENDING',
        'url': 'https://github.com/owner/repo/pull/103',
        'html_url': 'https://github.com/owner/repo/pull/103',
    },
]


def _get_cli_root():
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


def _patch_resolve():
    """Return context managers that mock contract + GitHub calls."""
    return (
        patch(
            'gittensor.cli.issue_commands.predictions.get_contract_address',
            return_value=FAKE_CONTRACT,
        ),
        patch(
            'gittensor.cli.issue_commands.predictions.resolve_network',
            return_value=('wss://fake', 'test'),
        ),
        patch(
            'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
            return_value=FAKE_ISSUES,
        ),
        patch(
            'gittensor.cli.issue_commands.predictions.fetch_open_prs_for_issue',
            return_value=FAKE_PRS,
        ),
    )


# =============================================================================
# _validate_probability
# =============================================================================


class TestValidateProbability:
    def test_valid_zero(self):
        assert _validate_probability(0.0) == 0.0

    def test_valid_one(self):
        assert _validate_probability(1.0) == 1.0

    def test_valid_mid(self):
        assert _validate_probability(0.5) == 0.5

    def test_string_input(self):
        assert _validate_probability('0.85') == 0.85

    def test_negative_rejected(self):
        with pytest.raises(click.BadParameter) as exc_info:
            _validate_probability(-0.1)
        assert '0.0' in str(exc_info.value) or '1.0' in str(exc_info.value)

    def test_over_one_rejected(self):
        with pytest.raises(click.BadParameter):
            _validate_probability(1.1)

    def test_invalid_string_rejected(self):
        with pytest.raises(click.BadParameter):
            _validate_probability('abc')


# =============================================================================
# _validate_pr_belongs_to_issue
# =============================================================================


class TestValidatePrBelongsToIssue:
    def test_valid_pr(self):
        _validate_pr_belongs_to_issue(101, FAKE_PRS)  # should not raise

    def test_invalid_pr(self):
        with pytest.raises(click.ClickException) as exc_info:
            _validate_pr_belongs_to_issue(999, FAKE_PRS)
        assert '999' in str(exc_info.value)


# =============================================================================
# _build_pr_table
# =============================================================================


class TestBuildPrTable:
    def test_builds_table_with_rows(self):
        table = _build_pr_table(FAKE_PRS)
        assert table.row_count == 2

    def test_empty_prs(self):
        table = _build_pr_table([])
        assert table.row_count == 0


# =============================================================================
# _github_headers
# =============================================================================


class TestGithubHeaders:
    def test_with_pat(self):
        with patch.dict('os.environ', {'GITTENSOR_MINER_PAT': 'ghp_test123'}):
            headers = _github_headers()
        assert headers['Authorization'] == 'token ghp_test123'
        assert 'Accept' in headers

    def test_without_pat(self):
        with patch.dict('os.environ', {}, clear=True):
            headers = _github_headers()
        assert 'Authorization' not in headers
        assert 'Accept' in headers


# =============================================================================
# submissions CLI command
# =============================================================================


class TestSubmissionsCommand:
    def test_submissions_displays_prs(self, cli_root, runner):
        p1, p2, p3, p4 = _patch_resolve()
        with p1, p2, p3, p4:
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '42'],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert '101' in result.output
        assert '103' in result.output
        assert 'Fix the bug' in result.output

    def test_submissions_json_output(self, cli_root, runner):
        p1, p2, p3, p4 = _patch_resolve()
        with p1, p2, p3, p4:
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '42', '--json'],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['issue_id'] == 42
        assert data['repository'] == 'owner/repo'
        assert len(data['submissions']) == 2

    def test_submissions_alias_i(self, cli_root, runner):
        p1, p2, p3, p4 = _patch_resolve()
        with p1, p2, p3, p4:
            result = runner.invoke(
                cli_root,
                ['i', 'submissions', '--id', '42'],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert '101' in result.output

    def test_submissions_issue_not_found(self, cli_root, runner):
        p1, p2, p3, p4 = _patch_resolve()
        with p1, p2, p3, p4:
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '999'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'not found' in result.output

    def test_submissions_completed_issue_rejected(self, cli_root, runner):
        p1, p2, p3, p4 = _patch_resolve()
        with p1, p2, p3, p4:
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '99'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'not in an active' in result.output

    def test_submissions_missing_contract(self, cli_root, runner):
        with patch(
            'gittensor.cli.issue_commands.predictions.get_contract_address',
            return_value='',
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '42'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'Contract address not configured' in result.output

    def test_submissions_no_prs_found(self, cli_root, runner):
        with (
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=FAKE_CONTRACT,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.resolve_network',
                return_value=('wss://fake', 'test'),
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=FAKE_ISSUES,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.fetch_open_prs_for_issue',
                return_value=[],
            ),
        ):
            result = runner.invoke(
                cli_root,
                ['issues', 'submissions', '--id', '42'],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert 'No open PRs' in result.output


# =============================================================================
# predict CLI command - non-interactive modes
# =============================================================================


class TestPredictCommandNonInteractive:
    def _invoke_predict(self, runner, cli_root, extra_args):
        p1, p2, p3, p4 = _patch_resolve()
        with (
            p1, p2, p3, p4,
            patch.dict('os.environ', {'GITTENSOR_MINER_PAT': 'ghp_fake'}),
            patch('gittensor.cli.issue_commands.predictions.bt', create=True) as mock_bt,
        ):
            # Mock bittensor wallet + subtensor
            mock_wallet = MagicMock()
            mock_wallet.hotkey.ss58_address = '5FakeHotkey'
            mock_bt.Wallet.return_value = mock_wallet
            mock_subtensor = MagicMock()
            mock_metagraph = MagicMock()
            mock_metagraph.hotkeys = ['5FakeHotkey']
            mock_subtensor.metagraph.return_value = mock_metagraph
            mock_bt.Subtensor.return_value = mock_subtensor

            result = runner.invoke(
                cli_root,
                ['issues', 'predict'] + extra_args,
                catch_exceptions=False,
            )
        return result

    def test_single_pr_prediction(self, cli_root, runner):
        result = self._invoke_predict(
            runner, cli_root,
            ['--id', '42', '--pr', '101', '--probability', '0.85', '-y'],
        )
        assert result.exit_code == 0
        assert 'Prediction validated' in result.output
        assert '101' in result.output
        assert '0.85' in result.output

    def test_batch_json_input(self, cli_root, runner):
        result = self._invoke_predict(
            runner, cli_root,
            ['--id', '42', '--json-input', '{"101": 0.6, "103": 0.3}', '-y'],
        )
        assert result.exit_code == 0
        assert 'Prediction validated' in result.output

    def test_json_output(self, cli_root, runner):
        result = self._invoke_predict(
            runner, cli_root,
            ['--id', '42', '--pr', '101', '--probability', '0.5', '--json'],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data['issue_id'] == 42
        assert data['predictions']['101'] == 0.5

    def test_missing_probability_rejected(self, cli_root, runner):
        result = self._invoke_predict(
            runner, cli_root,
            ['--id', '42', '--pr', '101', '-y'],
        )
        assert result.exit_code != 0
        assert 'probability' in result.output.lower()

    def test_invalid_pr_number_rejected(self, cli_root, runner):
        result = self._invoke_predict(
            runner, cli_root,
            ['--id', '42', '--pr', '999', '--probability', '0.5', '-y'],
        )
        assert result.exit_code != 0
        assert '999' in result.output

    def test_sum_exceeds_one_rejected(self, cli_root, runner):
        result = self._invoke_predict(
            runner, cli_root,
            ['--id', '42', '--json-input', '{"101": 0.8, "103": 0.3}', '-y'],
        )
        assert result.exit_code != 0
        assert 'exceeds 1.0' in result.output

    def test_probability_over_one_rejected(self, cli_root, runner):
        result = self._invoke_predict(
            runner, cli_root,
            ['--id', '42', '--pr', '101', '--probability', '1.5', '-y'],
        )
        assert result.exit_code != 0

    def test_invalid_json_input_rejected(self, cli_root, runner):
        result = self._invoke_predict(
            runner, cli_root,
            ['--id', '42', '--json-input', 'not-json', '-y'],
        )
        assert result.exit_code != 0
        assert 'Invalid JSON' in result.output

    def test_no_open_prs_rejected(self, cli_root, runner):
        with (
            patch(
                'gittensor.cli.issue_commands.predictions.get_contract_address',
                return_value=FAKE_CONTRACT,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.resolve_network',
                return_value=('wss://fake', 'test'),
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.read_issues_from_contract',
                return_value=FAKE_ISSUES,
            ),
            patch(
                'gittensor.cli.issue_commands.predictions.fetch_open_prs_for_issue',
                return_value=[],
            ),
            patch.dict('os.environ', {'GITTENSOR_MINER_PAT': 'ghp_fake'}),
            patch('gittensor.cli.issue_commands.predictions.bt', create=True) as mock_bt,
        ):
            mock_wallet = MagicMock()
            mock_wallet.hotkey.ss58_address = '5FakeHotkey'
            mock_bt.Wallet.return_value = mock_wallet
            mock_subtensor = MagicMock()
            mock_metagraph = MagicMock()
            mock_metagraph.hotkeys = ['5FakeHotkey']
            mock_subtensor.metagraph.return_value = mock_metagraph
            mock_bt.Subtensor.return_value = mock_subtensor

            result = runner.invoke(
                cli_root,
                ['issues', 'predict', '--id', '42', '--pr', '101', '--probability', '0.5', '-y'],
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert 'No open PRs' in result.output


# =============================================================================
# predict CLI - interactive mode
# =============================================================================


class TestPredictCommandInteractive:
    def test_interactive_with_input(self, cli_root, runner):
        p1, p2, p3, p4 = _patch_resolve()
        with (
            p1, p2, p3, p4,
            patch.dict('os.environ', {'GITTENSOR_MINER_PAT': 'ghp_fake'}),
            patch('gittensor.cli.issue_commands.predictions.bt', create=True) as mock_bt,
        ):
            mock_wallet = MagicMock()
            mock_wallet.hotkey.ss58_address = '5FakeHotkey'
            mock_bt.Wallet.return_value = mock_wallet
            mock_subtensor = MagicMock()
            mock_metagraph = MagicMock()
            mock_metagraph.hotkeys = ['5FakeHotkey']
            mock_subtensor.metagraph.return_value = mock_metagraph
            mock_bt.Subtensor.return_value = mock_subtensor

            # Input: 0.6 for first PR, skip second, then confirm
            result = runner.invoke(
                cli_root,
                ['issues', 'predict', '--id', '42'],
                input='0.6\n\ny\n',
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert 'Prediction validated' in result.output

    def test_interactive_skip_all_aborts(self, cli_root, runner):
        p1, p2, p3, p4 = _patch_resolve()
        with (
            p1, p2, p3, p4,
            patch.dict('os.environ', {'GITTENSOR_MINER_PAT': 'ghp_fake'}),
            patch('gittensor.cli.issue_commands.predictions.bt', create=True) as mock_bt,
        ):
            mock_wallet = MagicMock()
            mock_wallet.hotkey.ss58_address = '5FakeHotkey'
            mock_bt.Wallet.return_value = mock_wallet
            mock_subtensor = MagicMock()
            mock_metagraph = MagicMock()
            mock_metagraph.hotkeys = ['5FakeHotkey']
            mock_subtensor.metagraph.return_value = mock_metagraph
            mock_bt.Subtensor.return_value = mock_subtensor

            # Skip both PRs
            result = runner.invoke(
                cli_root,
                ['issues', 'predict', '--id', '42'],
                input='\n\n',
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert 'No predictions entered' in result.output


# =============================================================================
# fetch_open_prs_for_issue (helper)
# =============================================================================


class TestFetchOpenPrsForIssue:
    def test_returns_prs(self):
        search_response = json.dumps({
            'items': [
                {
                    'number': 10,
                    'title': 'PR title',
                    'user': {'login': 'alice'},
                    'created_at': '2025-01-01',
                    'html_url': 'https://github.com/o/r/pull/10',
                    'pull_request': {'html_url': 'https://github.com/o/r/pull/10'},
                },
                {
                    'number': 11,
                    'title': 'Not a PR',
                    'user': {'login': 'bob'},
                    'created_at': '2025-01-02',
                    'html_url': 'https://github.com/o/r/issues/11',
                    # no 'pull_request' key — this is an issue
                },
            ]
        }).encode()

        reviews_response = json.dumps([]).encode()

        call_count = {'n': 0}
        def mock_urlopen(req, timeout=None):
            call_count['n'] += 1
            resp = MagicMock()
            if call_count['n'] == 1:
                resp.read.return_value = search_response
            else:
                resp.read.return_value = reviews_response
            return resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            result = fetch_open_prs_for_issue('o', 'r', 5)

        assert len(result) == 1
        assert result[0]['number'] == 10
        assert result[0]['author'] == 'alice'

    def test_handles_api_error(self):
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError(
            'url', 403, 'Forbidden', {}, None
        )):
            result = fetch_open_prs_for_issue('o', 'r', 5)
        assert result == []


# =============================================================================
# _get_pr_review_status
# =============================================================================


class TestGetPrReviewStatus:
    def test_approved(self):
        reviews = json.dumps([
            {'state': 'APPROVED', 'user': {'login': 'reviewer'}},
        ]).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = reviews
        with patch('urllib.request.urlopen', return_value=mock_resp):
            status = _get_pr_review_status('o', 'r', 1, {})
        assert status == 'APPROVED'

    def test_changes_requested(self):
        reviews = json.dumps([
            {'state': 'CHANGES_REQUESTED', 'user': {'login': 'reviewer'}},
        ]).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = reviews
        with patch('urllib.request.urlopen', return_value=mock_resp):
            status = _get_pr_review_status('o', 'r', 1, {})
        assert status == 'CHANGES_REQUESTED'

    def test_no_reviews(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([]).encode()
        with patch('urllib.request.urlopen', return_value=mock_resp):
            status = _get_pr_review_status('o', 'r', 1, {})
        assert status == 'REVIEW_REQUIRED'

    def test_api_error_returns_unknown(self):
        with patch('urllib.request.urlopen', side_effect=Exception('timeout')):
            status = _get_pr_review_status('o', 'r', 1, {})
        assert status == 'UNKNOWN'
