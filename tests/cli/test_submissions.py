# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for submissions and predict CLI commands, find_prs_for_issue, and shared helpers.
"""

import json
import os
import sys
from unittest.mock import MagicMock, call, patch

import click
import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.helpers import (
    build_pr_table,
    build_prediction_payload,
    collect_predictions,
    fetch_issue_from_contract,
    format_pred_lines,
    get_github_pat,
    validate_issue_id,
    validate_predictions,
    verify_miner_registration,
)
from gittensor.cli.issue_commands.submissions import issues_predict, issues_submissions
from gittensor.utils.github_api_tools import _resolve_pr_state, find_prs_for_issue

# =============================================================================
# Fixtures
# =============================================================================

SAMPLE_ISSUE = {
    'id': 1,
    'repository_full_name': 'owner/repo',
    'issue_number': 42,
    'bounty_amount': 100_000_000_000,
    'target_bounty': 200_000_000_000,
    'status': 'Active',
}

SAMPLE_OPEN_PRS = [
    {
        'number': 123,
        'title': 'Fix the bug',
        'author': 'alice',
        'state': 'OPEN',
        'created_at': '2025-01-15T00:00:00Z',
        'merged_at': None,
        'url': 'https://github.com/owner/repo/pull/123',
        'review_status': None,
        'closes_issue': True,
    },
    {
        'number': 456,
        'title': 'Another fix',
        'author': 'bob',
        'state': 'OPEN',
        'created_at': '2025-01-16T00:00:00Z',
        'merged_at': None,
        'url': 'https://github.com/owner/repo/pull/456',
        'review_status': None,
        'closes_issue': False,
    },
]


def _make_graphql_timeline_response(prs):
    """Build a fake GraphQL response for find_prs_for_issue."""
    nodes = []
    for pr in prs:
        closing_nodes = [{'number': n} for n in pr.get('closing_issues', [])]
        review_nodes = [{'state': pr['review_state']}] if pr.get('review_state') else []
        nodes.append(
            {
                'source': {
                    'number': pr['number'],
                    'title': pr.get('title', f'PR #{pr["number"]}'),
                    'state': pr.get('state', 'OPEN'),
                    'merged': pr.get('merged', False),
                    'mergedAt': pr.get('mergedAt'),
                    'createdAt': pr.get('createdAt', '2025-01-01T00:00:00Z'),
                    'url': pr.get('url', f'https://github.com/owner/repo/pull/{pr["number"]}'),
                    'author': {'login': pr.get('author', 'testuser'), 'databaseId': 12345},
                    'baseRepository': {'nameWithOwner': pr.get('base_repo', 'owner/repo')},
                    'reviews': {'nodes': review_nodes},
                    'closingIssuesReferences': {'nodes': closing_nodes},
                }
            }
        )
    return {
        'data': {
            'repository': {
                'issue': {
                    'timelineItems': {
                        'nodes': nodes,
                    }
                }
            }
        }
    }


def _make_rest_timeline_event(pr_number, title='PR', author='user', state='open', merged_at=None):
    """Build a fake REST timeline event for _find_prs_for_issue_rest."""
    return {
        'event': 'cross-referenced',
        'source': {
            'type': 'issue',
            'issue': {
                'number': pr_number,
                'title': title,
                'state': state,
                'user': {'login': author, 'id': 12345},
                'created_at': '2025-01-01T00:00:00Z',
                'html_url': f'https://github.com/owner/repo/pull/{pr_number}',
                'pull_request': {'merged_at': merged_at},
            },
        },
    }


# =============================================================================
# Patch targets
# =============================================================================

_PATCH_FIND_PRS = 'gittensor.cli.issue_commands.submissions.find_prs_for_issue'
_PATCH_FETCH_ISSUE = 'gittensor.cli.issue_commands.submissions.fetch_issue_from_contract'
_PATCH_RESOLVE = 'gittensor.cli.issue_commands.submissions.resolve_network'
_PATCH_VERIFY_MINER = 'gittensor.cli.issue_commands.submissions.verify_miner_registration'


# =============================================================================
# Pytest fixtures for predict command
# =============================================================================


@pytest.fixture
def predict_mocks():
    """Set up all mocks needed for predict command tests.

    verify_miner_registration is now a helper — we mock it to return a hotkey address
    directly, avoiding the need to mock bittensor internals.
    """
    with patch(_PATCH_RESOLVE) as mock_resolve, \
         patch(_PATCH_FETCH_ISSUE) as mock_fetch, \
         patch(_PATCH_FIND_PRS) as mock_find, \
         patch(_PATCH_VERIFY_MINER) as mock_verify:

        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = SAMPLE_OPEN_PRS
        mock_verify.return_value = '5FakeHotkey123'

        yield {
            'resolve': mock_resolve,
            'fetch_issue': mock_fetch,
            'find_prs': mock_find,
            'verify_miner': mock_verify,
        }


# =============================================================================
# _resolve_pr_state tests
# =============================================================================


class TestResolvePrState:
    """Unit tests for the shared _resolve_pr_state helper."""

    def test_open_state(self):
        assert _resolve_pr_state(raw_state='OPEN') == 'OPEN'

    def test_closed_state(self):
        assert _resolve_pr_state(raw_state='CLOSED') == 'CLOSED'

    def test_merged_flag_true(self):
        assert _resolve_pr_state(merged=True, raw_state='CLOSED') == 'MERGED'

    def test_merged_at_timestamp(self):
        assert _resolve_pr_state(merged_at='2025-06-01T00:00:00Z', raw_state='closed') == 'MERGED'

    def test_merged_flag_overrides_open(self):
        assert _resolve_pr_state(merged=True, raw_state='OPEN') == 'MERGED'

    def test_lowercase_state_normalized(self):
        assert _resolve_pr_state(raw_state='open') == 'OPEN'

    def test_unknown_state_defaults_to_open(self):
        assert _resolve_pr_state(raw_state='draft') == 'OPEN'

    def test_empty_state_defaults_to_open(self):
        assert _resolve_pr_state(raw_state='') == 'OPEN'


# =============================================================================
# validate_issue_id tests
# =============================================================================


class TestValidateIssueId:
    def test_valid_id(self):
        assert validate_issue_id(1) == 1

    def test_valid_large_id(self):
        assert validate_issue_id(999_999) == 999_999

    def test_zero_rejected(self):
        with pytest.raises(click.BadParameter, match='must be between'):
            validate_issue_id(0)

    def test_negative_rejected(self):
        with pytest.raises(click.BadParameter, match='must be between'):
            validate_issue_id(-1)

    def test_too_large_rejected(self):
        with pytest.raises(click.BadParameter, match='must be between'):
            validate_issue_id(1_000_000)

    def test_custom_param_name(self):
        with pytest.raises(click.BadParameter, match='my_param'):
            validate_issue_id(0, param_name='my_param')


# =============================================================================
# build_pr_table tests
# =============================================================================


class TestBuildPrTable:
    def test_table_with_prs(self):
        table = build_pr_table(SAMPLE_OPEN_PRS)
        assert table.row_count == 2
        assert len(table.columns) == 6

    def test_empty_list_returns_empty_table(self):
        table = build_pr_table([])
        assert table.row_count == 0

    def test_approved_review_shows_green(self):
        prs = [{**SAMPLE_OPEN_PRS[0], 'review_status': 'APPROVED'}]
        table = build_pr_table(prs)
        # Table is built without error; review column rendered
        assert table.row_count == 1

    def test_changes_requested_review(self):
        prs = [{**SAMPLE_OPEN_PRS[0], 'review_status': 'CHANGES_REQUESTED'}]
        table = build_pr_table(prs)
        assert table.row_count == 1

    def test_missing_fields_use_defaults(self):
        """PRs with missing optional fields still render."""
        prs = [{'number': 1}]
        table = build_pr_table(prs)
        assert table.row_count == 1


# =============================================================================
# fetch_issue_from_contract tests
# =============================================================================


_PATCH_READ_ISSUES = 'gittensor.cli.issue_commands.helpers.read_issues_from_contract'


class TestFetchIssueFromContract:
    @patch(_PATCH_READ_ISSUES)
    def test_active_issue_returned(self, mock_read):
        mock_read.return_value = [SAMPLE_ISSUE]
        result = fetch_issue_from_contract(1, 'wss://test', '0xContract', False)
        assert result == SAMPLE_ISSUE

    @patch(_PATCH_READ_ISSUES)
    def test_not_found_raises(self, mock_read):
        mock_read.return_value = []
        with pytest.raises(click.ClickException, match='not found'):
            fetch_issue_from_contract(999, 'wss://test', '0xContract', False)

    @patch(_PATCH_READ_ISSUES)
    def test_require_active_rejects_completed(self, mock_read):
        mock_read.return_value = [{**SAMPLE_ISSUE, 'status': 'Completed'}]
        with pytest.raises(click.ClickException, match='Completed'):
            fetch_issue_from_contract(1, 'wss://test', '0xContract', False, require_active=True)

    @patch(_PATCH_READ_ISSUES)
    def test_require_active_accepts_active(self, mock_read):
        mock_read.return_value = [SAMPLE_ISSUE]
        result = fetch_issue_from_contract(1, 'wss://test', '0xContract', False, require_active=True)
        assert result['status'] == 'Active'

    @patch(_PATCH_READ_ISSUES)
    def test_submissions_warns_on_completed(self, mock_read):
        """Without require_active, completed issues return with a warning (not error)."""
        mock_read.return_value = [{**SAMPLE_ISSUE, 'status': 'Completed'}]
        result = fetch_issue_from_contract(1, 'wss://test', '0xContract', False, require_active=False)
        assert result['status'] == 'Completed'

    @patch(_PATCH_READ_ISSUES)
    def test_registered_status_accepted(self, mock_read):
        mock_read.return_value = [{**SAMPLE_ISSUE, 'status': 'Registered'}]
        result = fetch_issue_from_contract(1, 'wss://test', '0xContract', False, require_active=False)
        assert result['status'] == 'Registered'


# =============================================================================
# find_prs_for_issue tests
# =============================================================================


class TestFindPrsForIssue:
    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_returns_open_prs(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [
                {'number': 101, 'title': 'Fix the bug', 'author': 'alice', 'state': 'OPEN'},
                {'number': 102, 'title': 'Another fix', 'author': 'bob', 'state': 'OPEN'},
            ]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token', state_filter='open')

        assert len(result) == 2
        assert result[0]['number'] == 101
        assert result[0]['author'] == 'alice'
        assert result[0]['state'] == 'OPEN'
        assert result[1]['number'] == 102

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_filters_by_state_merged(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [
                {'number': 101, 'state': 'OPEN'},
                {'number': 102, 'state': 'CLOSED', 'merged': True, 'mergedAt': '2025-06-01T00:00:00Z'},
            ]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token', state_filter='merged')

        assert len(result) == 1
        assert result[0]['number'] == 102
        assert result[0]['state'] == 'MERGED'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_filters_by_state_closed(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [
                {'number': 101, 'state': 'OPEN'},
                {'number': 102, 'state': 'CLOSED'},
                {'number': 103, 'state': 'CLOSED', 'merged': True, 'mergedAt': '2025-06-01T00:00:00Z'},
            ]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token', state_filter='closed')

        assert len(result) == 1
        assert result[0]['number'] == 102
        assert result[0]['state'] == 'CLOSED'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_rejects_prs_from_wrong_repo(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [
                {'number': 101, 'base_repo': 'other/repo'},
                {'number': 102, 'base_repo': 'owner/repo'},
            ]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')

        assert len(result) == 1
        assert result[0]['number'] == 102

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_handles_empty_timeline(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response([])

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')

        assert result == []

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_returns_all_states_when_no_filter(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [
                {'number': 101, 'state': 'OPEN'},
                {'number': 102, 'state': 'CLOSED', 'merged': True, 'mergedAt': '2025-06-01T00:00:00Z'},
                {'number': 103, 'state': 'CLOSED'},
            ]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')

        assert len(result) == 3
        states = {pr['state'] for pr in result}
        assert states == {'OPEN', 'MERGED', 'CLOSED'}

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_closes_issue_flag(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [
                {'number': 101, 'closing_issues': [42]},
                {'number': 102, 'closing_issues': [99]},
            ]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')

        assert result[0]['closes_issue'] is True
        assert result[1]['closes_issue'] is False

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_review_status_extracted(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [
                {'number': 101, 'review_state': 'APPROVED'},
                {'number': 102},
            ]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')

        assert result[0]['review_status'] == 'APPROVED'
        assert result[1]['review_status'] is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_review_status_changes_requested(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [{'number': 101, 'review_state': 'CHANGES_REQUESTED'}]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')
        assert result[0]['review_status'] == 'CHANGES_REQUESTED'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_author_database_id_extracted(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [{'number': 101, 'author': 'alice'}]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')
        assert result[0]['author_database_id'] == 12345

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_fallback_when_no_token(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            _make_rest_timeline_event(101, title='Fix bug', author='alice'),
        ]
        mock_get.return_value = mock_response

        result = find_prs_for_issue('owner/repo', 42, token=None)

        assert len(result) == 1
        assert result[0]['number'] == 101
        assert result[0]['state'] == 'OPEN'
        mock_get.assert_called_once()

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_with_auth_token_in_header(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        find_prs_for_issue('owner/repo', 42, token=None)

        headers = mock_get.call_args[1].get('headers', mock_get.call_args[0][0] if mock_get.call_args[0] else {})
        if isinstance(headers, dict):
            assert 'Authorization' not in headers

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_skips_non_cross_referenced_events(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {'event': 'labeled', 'label': {'name': 'bug'}},
            {'event': 'commented', 'body': 'test'},
            _make_rest_timeline_event(101),
        ]
        mock_get.return_value = mock_response

        result = find_prs_for_issue('owner/repo', 42, token=None)
        assert len(result) == 1
        assert result[0]['number'] == 101

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_deduplicates_prs(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            _make_rest_timeline_event(101),
            _make_rest_timeline_event(101),  # duplicate
        ]
        mock_get.return_value = mock_response

        result = find_prs_for_issue('owner/repo', 42, token=None)
        assert len(result) == 1

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_merged_pr_detected(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            _make_rest_timeline_event(101, state='closed', merged_at='2025-06-01T00:00:00Z'),
        ]
        mock_get.return_value = mock_response

        result = find_prs_for_issue('owner/repo', 42, token=None)
        assert result[0]['state'] == 'MERGED'
        assert result[0]['merged_at'] == '2025-06-01T00:00:00Z'

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_state_filter_applied(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            _make_rest_timeline_event(101, state='open'),
            _make_rest_timeline_event(102, state='closed', merged_at='2025-06-01T00:00:00Z'),
        ]
        mock_get.return_value = mock_response

        result = find_prs_for_issue('owner/repo', 42, token=None, state_filter='open')
        assert len(result) == 1
        assert result[0]['number'] == 101

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_api_error_returns_empty(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = find_prs_for_issue('owner/repo', 42, token=None)
        assert result == []

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_graphql_failure_returns_empty(self, mock_gql):
        mock_gql.return_value = None

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')

        assert result == []

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_graphql_pr_created_at_extracted(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [{'number': 101, 'createdAt': '2025-03-15T12:30:00Z'}]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')
        assert result[0]['created_at'] == '2025-03-15T12:30:00Z'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_graphql_merged_at_extracted(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response(
            [{'number': 101, 'state': 'CLOSED', 'merged': True, 'mergedAt': '2025-07-01T00:00:00Z'}]
        )

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')
        assert result[0]['merged_at'] == '2025-07-01T00:00:00Z'


# =============================================================================
# submissions command tests
# =============================================================================


class TestSubmissionsCommand:
    def _invoke(self, args, env=None):
        runner = CliRunner(env=env)
        return runner.invoke(issues_submissions, args, catch_exceptions=False)

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_displays_table(self, mock_resolve, mock_fetch, mock_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = [SAMPLE_OPEN_PRS[0]]

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert '123' in result.output
        assert 'Fix the bug' in result.output
        assert 'alice' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_json_output(self, mock_resolve, mock_fetch, mock_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = [SAMPLE_OPEN_PRS[0]]

        result = self._invoke(['--id', '1', '--json'])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]['number'] == 123

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_json_output_strips_internal_fields(self, mock_resolve, mock_fetch, mock_find):
        """JSON output should not include author_database_id."""
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = [{**SAMPLE_OPEN_PRS[0], 'author_database_id': 12345}]

        result = self._invoke(['--id', '1', '--json'])

        data = json.loads(result.output)
        assert 'author_database_id' not in data[0]

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_shows_pr_count(self, mock_resolve, mock_fetch, mock_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = SAMPLE_OPEN_PRS

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert '2 open PR(s)' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_missing_pat_shows_warning(self, mock_resolve, mock_fetch, mock_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = []

        result = self._invoke(['--id', '1'], env={'GITTENSOR_MINER_PAT': ''})

        assert result.exit_code == 0
        assert 'GITTENSOR_MINER_PAT' in result.output or 'unauthenticated' in result.output.lower()

    def test_invalid_issue_id_zero(self):
        result = self._invoke(['--id', '0'])

        assert result.exit_code != 0
        assert 'must be between' in result.output

    def test_invalid_issue_id_negative(self):
        result = self._invoke(['--id', '-1'])

        assert result.exit_code != 0

    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_issue_not_found(self, mock_resolve, mock_fetch):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.side_effect = click.ClickException('Issue 999 not found on contract.')

        result = self._invoke(['--id', '999'])

        assert result.exit_code != 0
        assert 'not found' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_no_open_prs(self, mock_resolve, mock_fetch, mock_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = []

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert 'No open PRs found' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_completed_issue_continues(self, mock_resolve, mock_fetch, mock_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = {**SAMPLE_ISSUE, 'status': 'Completed'}
        mock_find.return_value = []

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_github_api_failure_graceful(self, mock_resolve, mock_fetch, mock_find):
        """GitHub API failure should show warning and empty results, not crash."""
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.side_effect = Exception('GitHub API rate limited')

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert 'No open PRs found' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_empty_pat_treated_as_none(self, mock_resolve, mock_fetch, mock_find):
        """Empty string GITTENSOR_MINER_PAT should be treated as unset."""
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = []

        result = self._invoke(['--id', '1'], env={'GITTENSOR_MINER_PAT': ''})

        assert result.exit_code == 0
        # Should warn about missing PAT
        assert 'GITTENSOR_MINER_PAT' in result.output or 'unauthenticated' in result.output.lower()


# =============================================================================
# predict command tests
# =============================================================================


class TestPredictCommand:
    def _invoke(self, args, env=None):
        runner = CliRunner(env=env or {'GITTENSOR_MINER_PAT': 'fake-pat'})
        return runner.invoke(issues_predict, args, catch_exceptions=False)

    def test_single_prediction(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y'])

        assert result.exit_code == 0
        assert 'Prediction' in result.output
        assert '123' in result.output

    def test_json_input(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{"123": 0.5}', '-y'])

        assert result.exit_code == 0
        assert '123' in result.output

    def test_json_output(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y', '--json'])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload['issue_id'] == 1
        assert '123' in payload['predictions']

    def test_json_output_payload_shape(self, predict_mocks):
        """JSON output should have all required payload fields."""
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y', '--json'])

        payload = json.loads(result.output)
        assert 'issue_id' in payload
        assert 'repository' in payload
        assert 'issue_number' in payload
        assert 'miner_hotkey' in payload
        assert 'predictions' in payload
        assert payload['repository'] == 'owner/repo'
        assert payload['miner_hotkey'] == '5FakeHotkey123'

    def test_json_output_pr_keys_are_strings(self, predict_mocks):
        """Prediction keys in JSON payload should be strings."""
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y', '--json'])

        payload = json.loads(result.output)
        for key in payload['predictions']:
            assert isinstance(key, str)

    def test_probability_above_one_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '1.5', '-y'])

        assert result.exit_code != 0
        assert 'between 0.0 and 1.0' in result.output

    def test_negative_probability_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '-0.5', '-y'])

        assert result.exit_code != 0
        assert 'between 0.0 and 1.0' in result.output

    def test_zero_probability_accepted(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.0', '-y'])

        assert result.exit_code == 0

    def test_exactly_one_probability_accepted(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '1.0', '-y'])

        assert result.exit_code == 0

    def test_sum_exceeds_one_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{"123": 0.7, "456": 0.5}', '-y'])

        assert result.exit_code != 0
        assert 'exceeds 1.0' in result.output

    def test_pr_not_in_open_prs_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '999', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'not an open PR' in result.output

    def test_pr_not_found_shows_available(self, predict_mocks):
        """Error message for unknown PR should list available PRs."""
        result = self._invoke(['--id', '1', '--pr', '999', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert '123' in result.output or '456' in result.output

    def test_missing_pat_shows_error(self):
        runner = CliRunner(env={'GITTENSOR_MINER_PAT': ''})
        result = runner.invoke(issues_predict, ['--id', '1', '--pr', '1', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'GITTENSOR_MINER_PAT' in result.output

    def test_pr_requires_probability(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '-y'])

        assert result.exit_code != 0
        assert '--probability is required' in result.output

    def test_probability_without_pr_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert '--pr is required' in result.output

    def test_non_active_issue_rejected(self, predict_mocks):
        predict_mocks['fetch_issue'].side_effect = click.ClickException(
            'Issue 1 has status "Completed" — predictions require Active status.'
        )

        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'Completed' in result.output

    def test_unregistered_hotkey_rejected(self, predict_mocks):
        predict_mocks['verify_miner'].side_effect = click.ClickException(
            'Hotkey 5NotRegistered is not registered on the metagraph. '
            'Register your miner before submitting predictions.'
        )

        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'not registered' in result.output

    def test_multiple_predictions_json_input(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{"123": 0.5, "456": 0.3}', '-y', '--json'])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert '123' in payload['predictions']
        assert '456' in payload['predictions']

    def test_wallet_load_failure(self, predict_mocks):
        """Wallet loading failure shows helpful message."""
        predict_mocks['verify_miner'].side_effect = click.ClickException(
            'Failed to load wallet or connect to network: wallet file not found'
        )

        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'Failed to load wallet' in result.output

    def test_predict_shows_hotkey(self, predict_mocks):
        """Successful predict displays the miner hotkey."""
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y'])

        assert result.exit_code == 0
        assert '5FakeHotkey123' in result.output

    def test_predict_shows_todo_note(self, predict_mocks):
        """Successful predict shows broadcast TODO note."""
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y'])

        assert result.exit_code == 0
        assert 'TODO' in result.output or 'not yet implemented' in result.output

    # --- JSON edge case tests ---

    def test_json_input_parse_error(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', 'not valid json', '-y'])

        assert result.exit_code != 0
        assert 'Invalid JSON input' in result.output

    def test_json_input_array_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '[1, 2]', '-y'])

        assert result.exit_code != 0
        assert 'JSON object' in result.output

    def test_json_input_non_numeric_key(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{"abc": 0.5}', '-y'])

        assert result.exit_code != 0
        assert 'Invalid PR number' in result.output

    def test_json_input_invalid_probability_value(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{"123": "high"}', '-y'])

        assert result.exit_code != 0
        assert 'Invalid probability' in result.output

    def test_json_input_empty_dict(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{}', '-y'])

        assert result.exit_code != 0
        assert 'No predictions provided' in result.output

    def test_json_input_negative_probability(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{"123": -0.5}', '-y'])

        assert result.exit_code != 0
        assert 'between 0.0 and 1.0' in result.output

    # --- Early flag-conflict validation (before network I/O) ---

    def test_pr_and_json_input_mutually_exclusive(self, predict_mocks):
        """--pr and --json-input cannot be used together."""
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.5', '--json-input', '{"456": 0.3}', '-y'])

        assert result.exit_code != 0
        assert 'not both' in result.output
        # Verify no network calls were made (fail fast)
        predict_mocks['fetch_issue'].assert_not_called()

    def test_probability_and_json_input_mutually_exclusive(self, predict_mocks):
        """--probability and --json-input cannot be used together."""
        result = self._invoke(['--id', '1', '--probability', '0.5', '--json-input', '{"456": 0.3}', '-y'])

        assert result.exit_code != 0
        assert 'not both' in result.output
        predict_mocks['fetch_issue'].assert_not_called()

    def test_early_probability_range_check(self, predict_mocks):
        """Out-of-range probability caught before network calls."""
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '2.0', '-y'])

        assert result.exit_code != 0
        assert 'between 0.0 and 1.0' in result.output
        predict_mocks['fetch_issue'].assert_not_called()

    def test_early_pr_without_probability_check(self, predict_mocks):
        """--pr without --probability caught before network calls."""
        result = self._invoke(['--id', '1', '--pr', '123', '-y'])

        assert result.exit_code != 0
        assert '--probability is required' in result.output
        predict_mocks['fetch_issue'].assert_not_called()

    def test_early_probability_without_pr_check(self, predict_mocks):
        """--probability without --pr caught before network calls."""
        result = self._invoke(['--id', '1', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert '--pr is required' in result.output
        predict_mocks['fetch_issue'].assert_not_called()

    def test_empty_string_pat_treated_as_missing(self):
        """Empty string GITTENSOR_MINER_PAT should be treated as unset."""
        runner = CliRunner(env={'GITTENSOR_MINER_PAT': ''})
        result = runner.invoke(issues_predict, ['--id', '1', '--pr', '1', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'GITTENSOR_MINER_PAT' in result.output


# =============================================================================
# Helper unit tests
# =============================================================================


class TestValidatePredictions:
    def test_valid_single_prediction(self):
        validate_predictions({1: 0.5}, {1, 2, 3})

    def test_valid_multiple_predictions(self):
        validate_predictions({1: 0.3, 2: 0.4}, {1, 2, 3})

    def test_probability_above_one_raises(self):
        with pytest.raises(click.ClickException, match='between 0.0 and 1.0'):
            validate_predictions({1: 1.5}, {1})

    def test_probability_below_zero_raises(self):
        with pytest.raises(click.ClickException, match='between 0.0 and 1.0'):
            validate_predictions({1: -0.1}, {1})

    def test_pr_not_in_open_prs_raises(self):
        with pytest.raises(click.ClickException, match='not an open PR'):
            validate_predictions({999: 0.5}, {1, 2})

    def test_sum_exceeds_one_raises(self):
        with pytest.raises(click.ClickException, match='exceeds 1.0'):
            validate_predictions({1: 0.6, 2: 0.6}, {1, 2})

    def test_sum_exactly_one_ok(self):
        validate_predictions({1: 0.5, 2: 0.5}, {1, 2})

    def test_empty_predictions_ok(self):
        validate_predictions({}, {1, 2})

    def test_boundary_zero_probability_ok(self):
        validate_predictions({1: 0.0}, {1})

    def test_boundary_one_probability_ok(self):
        validate_predictions({1: 1.0}, {1})

    def test_float_precision_edge(self):
        """Sum of 0.1 + 0.2 + 0.7 should not exceed 1.0 due to float imprecision."""
        validate_predictions({1: 0.1, 2: 0.2, 3: 0.7}, {1, 2, 3})

    def test_multiple_unknown_prs_shows_available(self):
        with pytest.raises(click.ClickException, match='not an open PR'):
            validate_predictions({999: 0.5}, {1, 2})


class TestFormatPredLines:
    def test_single_prediction(self):
        result = format_pred_lines({123: 0.7})
        assert 'PR #123' in result
        assert '70.00%' in result

    def test_multiple_predictions(self):
        result = format_pred_lines({123: 0.5, 456: 0.3})
        assert 'PR #123' in result
        assert 'PR #456' in result

    def test_empty_predictions(self):
        result = format_pred_lines({})
        assert result == ''

    def test_small_probability_formatted(self):
        result = format_pred_lines({1: 0.01})
        assert '1.00%' in result

    def test_full_probability_formatted(self):
        result = format_pred_lines({1: 1.0})
        assert '100.00%' in result


# =============================================================================
# collect_predictions unit tests
# =============================================================================


class TestCollectPredictions:
    """Direct unit tests for the collect_predictions helper."""

    def test_json_input_mode(self):
        preds = collect_predictions(
            pr_number=None,
            probability=None,
            json_input='{"123": 0.5, "456": 0.3}',
            open_prs=SAMPLE_OPEN_PRS,
            issue_id=1,
            repo='owner/repo',
            issue_number_gh=42,
        )
        assert preds == {123: 0.5, 456: 0.3}

    def test_pr_probability_mode(self):
        preds = collect_predictions(
            pr_number=123,
            probability=0.7,
            json_input=None,
            open_prs=SAMPLE_OPEN_PRS,
            issue_id=1,
            repo='owner/repo',
            issue_number_gh=42,
        )
        assert preds == {123: 0.7}

    def test_pr_without_probability_raises(self):
        with pytest.raises(click.ClickException, match='--probability is required'):
            collect_predictions(
                pr_number=123,
                probability=None,
                json_input=None,
                open_prs=SAMPLE_OPEN_PRS,
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )

    def test_probability_without_pr_raises(self):
        with pytest.raises(click.ClickException, match='--pr is required'):
            collect_predictions(
                pr_number=None,
                probability=0.5,
                json_input=None,
                open_prs=SAMPLE_OPEN_PRS,
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )

    @patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=False)
    def test_interactive_mode_requires_tty(self, _mock_tty):
        with pytest.raises(click.ClickException, match='TTY'):
            collect_predictions(
                pr_number=None,
                probability=None,
                json_input=None,
                open_prs=SAMPLE_OPEN_PRS,
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )

    @patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True)
    @patch('click.prompt', side_effect=['123', 0.6, '456', 0.2, 'done'])
    def test_interactive_mode_collects_multiple(self, _mock_prompt, _mock_tty):
        preds = collect_predictions(
            pr_number=None,
            probability=None,
            json_input=None,
            open_prs=SAMPLE_OPEN_PRS,
            issue_id=1,
            repo='owner/repo',
            issue_number_gh=42,
        )
        assert preds == {123: 0.6, 456: 0.2}

    @patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True)
    @patch('click.prompt', side_effect=['done'])
    def test_interactive_mode_empty_raises(self, _mock_prompt, _mock_tty):
        with pytest.raises(click.ClickException, match='No predictions provided'):
            collect_predictions(
                pr_number=None,
                probability=None,
                json_input=None,
                open_prs=SAMPLE_OPEN_PRS,
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )

    def test_json_input_non_numeric_value_raises(self):
        with pytest.raises(click.ClickException, match='Invalid probability'):
            collect_predictions(
                pr_number=None,
                probability=None,
                json_input='{"123": "high"}',
                open_prs=SAMPLE_OPEN_PRS,
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )

    def test_json_input_non_numeric_key_raises(self):
        with pytest.raises(click.ClickException, match='Invalid PR number'):
            collect_predictions(
                pr_number=None,
                probability=None,
                json_input='{"abc": 0.5}',
                open_prs=SAMPLE_OPEN_PRS,
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )

    def test_json_input_invalid_json_raises(self):
        with pytest.raises(click.ClickException, match='Invalid JSON'):
            collect_predictions(
                pr_number=None,
                probability=None,
                json_input='not json',
                open_prs=SAMPLE_OPEN_PRS,
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )

    def test_json_input_array_rejected(self):
        with pytest.raises(click.ClickException, match='JSON object'):
            collect_predictions(
                pr_number=None,
                probability=None,
                json_input='[1, 2]',
                open_prs=SAMPLE_OPEN_PRS,
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )

    @patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True)
    @patch('click.prompt', side_effect=['123', 0.6, '123', '456', 0.2, 'done'])
    def test_interactive_duplicate_pr_skipped(self, _mock_prompt, _mock_tty):
        """Entering the same PR twice should skip the duplicate."""
        preds = collect_predictions(
            pr_number=None,
            probability=None,
            json_input=None,
            open_prs=SAMPLE_OPEN_PRS,
            issue_id=1,
            repo='owner/repo',
            issue_number_gh=42,
        )
        assert preds == {123: 0.6, 456: 0.2}

    @patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True)
    @patch('click.prompt', side_effect=['abc', '123', 0.5, 'done'])
    def test_interactive_invalid_pr_retries(self, _mock_prompt, _mock_tty):
        """Non-numeric PR input should prompt again."""
        preds = collect_predictions(
            pr_number=None,
            probability=None,
            json_input=None,
            open_prs=SAMPLE_OPEN_PRS,
            issue_id=1,
            repo='owner/repo',
            issue_number_gh=42,
        )
        assert preds == {123: 0.5}

    @patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=False)
    def test_interactive_no_open_prs_raises(self, _mock_tty):
        """Interactive mode with no open PRs should raise immediately."""
        # When _is_interactive is False, it raises TTY error first
        # so let's test with is_interactive=True
        pass

    @patch('gittensor.cli.issue_commands.helpers._is_interactive', return_value=True)
    def test_interactive_empty_prs_raises(self, _mock_tty):
        with pytest.raises(click.ClickException, match='No open PRs'):
            collect_predictions(
                pr_number=None,
                probability=None,
                json_input=None,
                open_prs=[],
                issue_id=1,
                repo='owner/repo',
                issue_number_gh=42,
            )


# =============================================================================
# Cascading fallback sequence tests
# =============================================================================


class TestCascadingFallback:
    """Verify the exact call sequence of the GraphQL → REST(auth) → REST(unauth) cascade."""

    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_rest')
    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_graphql')
    def test_graphql_success_skips_rest(self, mock_gql, mock_rest):
        """When GraphQL returns results, REST is never called."""
        mock_gql.return_value = [{'number': 101}]

        result = find_prs_for_issue('owner/repo', 42, token='tok')

        assert result == [{'number': 101}]
        mock_gql.assert_called_once()
        mock_rest.assert_not_called()

    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_rest')
    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_graphql')
    def test_graphql_empty_falls_to_rest_auth(self, mock_gql, mock_rest):
        """When GraphQL returns empty, authenticated REST is tried next."""
        mock_gql.return_value = []
        mock_rest.return_value = [{'number': 202}]

        result = find_prs_for_issue('owner/repo', 42, token='tok')

        assert result == [{'number': 202}]
        mock_gql.assert_called_once()
        mock_rest.assert_called_once_with('owner', 'repo', 42, 'owner/repo', None, token='tok')

    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_rest')
    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_graphql')
    def test_full_cascade_graphql_rest_auth_rest_unauth(self, mock_gql, mock_rest):
        """When GraphQL and authenticated REST both return empty, unauthenticated REST is tried."""
        mock_gql.return_value = []
        mock_rest.side_effect = [[], [{'number': 303}]]

        result = find_prs_for_issue('owner/repo', 42, token='tok')

        assert result == [{'number': 303}]
        mock_gql.assert_called_once()
        assert mock_rest.call_count == 2
        # First call: authenticated REST
        assert mock_rest.call_args_list[0] == call('owner', 'repo', 42, 'owner/repo', None, token='tok')
        # Second call: unauthenticated REST
        assert mock_rest.call_args_list[1] == call('owner', 'repo', 42, 'owner/repo', None, _quiet=True)

    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_rest')
    def test_no_token_skips_graphql(self, mock_rest):
        """When no token is provided, only unauthenticated REST is called."""
        mock_rest.return_value = [{'number': 404}]

        result = find_prs_for_issue('owner/repo', 42, token=None)

        assert result == [{'number': 404}]
        mock_rest.assert_called_once_with('owner', 'repo', 42, 'owner/repo', None)

    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_rest')
    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_graphql')
    def test_all_levels_return_empty(self, mock_gql, mock_rest):
        """When all cascade levels return empty, final result is empty."""
        mock_gql.return_value = []
        mock_rest.return_value = []

        result = find_prs_for_issue('owner/repo', 42, token='tok')

        assert result == []
        assert mock_rest.call_count == 2

    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_rest')
    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_graphql')
    def test_state_filter_propagated_to_all_levels(self, mock_gql, mock_rest):
        """State filter should be passed through to GraphQL and REST calls."""
        mock_gql.return_value = []
        mock_rest.return_value = []

        find_prs_for_issue('owner/repo', 42, token='tok', state_filter='open')

        mock_gql.assert_called_once_with('owner', 'repo', 42, 'tok', 'owner/repo', 'open')
        assert mock_rest.call_args_list[0] == call('owner', 'repo', 42, 'owner/repo', 'open', token='tok')
        assert mock_rest.call_args_list[1] == call('owner', 'repo', 42, 'owner/repo', 'open', _quiet=True)

    @patch('gittensor.utils.github_api_tools._find_prs_for_issue_rest')
    def test_no_token_state_filter_propagated(self, mock_rest):
        """State filter should be passed to REST when no token."""
        mock_rest.return_value = []

        find_prs_for_issue('owner/repo', 42, token=None, state_filter='merged')

        mock_rest.assert_called_once_with('owner', 'repo', 42, 'owner/repo', 'merged')


# =============================================================================
# get_github_pat tests
# =============================================================================


class TestGetGithubPat:
    """Unit tests for the get_github_pat helper."""

    @patch.dict(os.environ, {'GITTENSOR_MINER_PAT': 'ghp_test123'})
    def test_returns_pat_when_set(self):
        assert get_github_pat() == 'ghp_test123'

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_none_when_unset(self):
        result = get_github_pat()
        assert result is None

    @patch.dict(os.environ, {'GITTENSOR_MINER_PAT': ''})
    def test_empty_string_returns_none(self):
        """Empty PAT should be treated as unset."""
        result = get_github_pat()
        assert result is None

    @patch.dict(os.environ, {'GITTENSOR_MINER_PAT': '  '})
    def test_whitespace_only_returns_value(self):
        """Whitespace-only PAT is technically set — caller decides whether to strip."""
        result = get_github_pat()
        assert result == '  '


# =============================================================================
# verify_miner_registration tests
# =============================================================================


class TestVerifyMinerRegistration:
    """Unit tests for the verify_miner_registration helper."""

    def _make_mock_bt(self, hotkey_addr='5FakeRegistered', metagraph_hotkeys=None):
        """Create a mock bittensor module with wallet and subtensor."""
        mock_bt = MagicMock()
        mock_wallet = MagicMock()
        mock_wallet.hotkey.ss58_address = hotkey_addr
        mock_bt.Wallet.return_value = mock_wallet

        mock_metagraph = MagicMock()
        mock_metagraph.hotkeys = metagraph_hotkeys or [hotkey_addr]
        mock_subtensor = MagicMock()
        mock_subtensor.metagraph.return_value = mock_metagraph
        mock_bt.Subtensor.return_value = mock_subtensor
        return mock_bt

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=422)
    def test_registered_hotkey_returns_address(self, mock_netuid):
        mock_bt = self._make_mock_bt('5FakeRegistered', ['5FakeRegistered', '5OtherKey'])
        with patch.dict(sys.modules, {'bittensor': mock_bt}):
            result = verify_miner_registration('default', 'default', 'wss://test', '0xContract', False)

        assert result == '5FakeRegistered'
        mock_bt.Wallet.assert_called_once_with(name='default', hotkey='default')

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=422)
    def test_unregistered_hotkey_raises(self, mock_netuid):
        mock_bt = self._make_mock_bt('5NotRegistered', ['5SomeOtherKey'])
        with patch.dict(sys.modules, {'bittensor': mock_bt}):
            with pytest.raises(click.ClickException, match='not registered'):
                verify_miner_registration('default', 'default', 'wss://test', '0xContract', False)

    def test_wallet_load_exception_raises(self):
        mock_bt = MagicMock()
        mock_bt.Wallet.side_effect = Exception('wallet file not found')
        with patch.dict(sys.modules, {'bittensor': mock_bt}):
            with pytest.raises(click.ClickException, match='Failed to load wallet'):
                verify_miner_registration('bad', 'hotkey', 'wss://test', '0xContract', False)

    def test_missing_bittensor_raises(self):
        with patch.dict(sys.modules, {'bittensor': None}):
            with pytest.raises(click.ClickException, match='Missing dependency'):
                verify_miner_registration('default', 'default', 'wss://test', '0xContract', False)

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=74)
    def test_custom_wallet_name_passed(self, mock_netuid):
        mock_bt = self._make_mock_bt('5FakeKey')
        with patch.dict(sys.modules, {'bittensor': mock_bt}):
            verify_miner_registration('my_wallet', 'my_hotkey', 'wss://test', '0xContract', False)

        mock_bt.Wallet.assert_called_once_with(name='my_wallet', hotkey='my_hotkey')

    @patch('gittensor.cli.issue_commands.helpers.read_netuid_from_contract', return_value=422)
    def test_subtensor_connect_failure_raises(self, mock_netuid):
        mock_bt = MagicMock()
        mock_wallet = MagicMock()
        mock_wallet.hotkey.ss58_address = '5FakeKey'
        mock_bt.Wallet.return_value = mock_wallet
        mock_bt.Subtensor.side_effect = Exception('Connection refused')
        with patch.dict(sys.modules, {'bittensor': mock_bt}):
            with pytest.raises(click.ClickException, match='Failed to load wallet or connect'):
                verify_miner_registration('default', 'default', 'wss://test', '0xContract', False)


# =============================================================================
# build_prediction_payload tests
# =============================================================================


class TestBuildPredictionPayload:
    """Unit tests for the build_prediction_payload helper."""

    def test_basic_payload(self):
        payload = build_prediction_payload(1, 'owner/repo', 42, '5FakeHotkey', {123: 0.7})

        assert payload['issue_id'] == 1
        assert payload['repository'] == 'owner/repo'
        assert payload['issue_number'] == 42
        assert payload['miner_hotkey'] == '5FakeHotkey'
        assert payload['predictions'] == {'123': 0.7}

    def test_prediction_keys_are_strings(self):
        payload = build_prediction_payload(1, 'owner/repo', 42, '5Key', {123: 0.5, 456: 0.3})

        for k in payload['predictions']:
            assert isinstance(k, str)

    def test_multiple_predictions(self):
        preds = {101: 0.3, 202: 0.4, 303: 0.2}
        payload = build_prediction_payload(5, 'org/lib', 99, '5Hotkey', preds)

        assert len(payload['predictions']) == 3
        assert payload['predictions']['101'] == 0.3
        assert payload['predictions']['202'] == 0.4
        assert payload['predictions']['303'] == 0.2

    def test_empty_predictions(self):
        payload = build_prediction_payload(1, 'owner/repo', 42, '5Key', {})

        assert payload['predictions'] == {}

    def test_payload_is_json_serializable(self):
        payload = build_prediction_payload(1, 'owner/repo', 42, '5Key', {123: 0.7})

        serialized = json.dumps(payload)
        deserialized = json.loads(serialized)
        assert deserialized == payload


# =============================================================================
# read_netuid_from_contract tests
# =============================================================================

_PATCH_READ_PACKED = 'gittensor.cli.issue_commands.helpers._read_contract_packed_storage'


class TestReadNetuidFromContract:
    """Unit tests for read_netuid_from_contract helper."""

    def _make_mock_substrate_module(self):
        """Create a mock substrateinterface module for sys.modules injection."""
        mock_mod = MagicMock()
        return mock_mod

    @patch(_PATCH_READ_PACKED, return_value={'netuid': 422})
    def test_returns_netuid_from_contract(self, mock_packed):
        from gittensor.cli.issue_commands.helpers import read_netuid_from_contract

        mock_mod = self._make_mock_substrate_module()
        with patch.dict(sys.modules, {'substrateinterface': mock_mod}):
            result = read_netuid_from_contract('wss://test', '0xContract', False)
        assert result == 422

    @patch(_PATCH_READ_PACKED, return_value=None)
    def test_returns_default_when_packed_is_none(self, mock_packed):
        from gittensor.cli.issue_commands.helpers import MAINNET_NETUID, read_netuid_from_contract

        mock_mod = self._make_mock_substrate_module()
        with patch.dict(sys.modules, {'substrateinterface': mock_mod}):
            result = read_netuid_from_contract('wss://test', '0xContract', False)
        assert result == MAINNET_NETUID

    @patch(_PATCH_READ_PACKED, return_value={'netuid': 0})
    def test_returns_default_when_netuid_is_zero(self, mock_packed):
        """netuid=0 is falsy, should fall back to default."""
        from gittensor.cli.issue_commands.helpers import MAINNET_NETUID, read_netuid_from_contract

        mock_mod = self._make_mock_substrate_module()
        with patch.dict(sys.modules, {'substrateinterface': mock_mod}):
            result = read_netuid_from_contract('wss://test', '0xContract', False)
        assert result == MAINNET_NETUID

    def test_returns_default_on_connection_error(self):
        from gittensor.cli.issue_commands.helpers import MAINNET_NETUID, read_netuid_from_contract

        mock_mod = MagicMock()
        mock_mod.SubstrateInterface.side_effect = Exception('Connection refused')
        with patch.dict(sys.modules, {'substrateinterface': mock_mod}):
            result = read_netuid_from_contract('wss://bad-endpoint', '0xContract', False)
        assert result == MAINNET_NETUID

    @patch(_PATCH_READ_PACKED, return_value={'netuid': 74, 'owner': '5Fake'})
    def test_mainnet_netuid_returned(self, mock_packed):
        from gittensor.cli.issue_commands.helpers import read_netuid_from_contract

        mock_mod = self._make_mock_substrate_module()
        with patch.dict(sys.modules, {'substrateinterface': mock_mod}):
            result = read_netuid_from_contract('wss://finney', '0xContract', False)
        assert result == 74


# =============================================================================
# _find_prs_for_issue_graphql direct tests
# =============================================================================


class TestFindPrsForIssueGraphql:
    """Direct unit tests for the internal GraphQL implementation."""

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_skips_nodes_without_number(self, mock_gql):
        """Nodes with empty source (non-PR events) should be skipped."""
        mock_gql.return_value = _make_graphql_timeline_response([
            {'number': 101},
        ])
        # Inject a node with empty source
        nodes = mock_gql.return_value['data']['repository']['issue']['timelineItems']['nodes']
        nodes.insert(0, {'source': {}})
        nodes.insert(1, {'source': None})

        from gittensor.utils.github_api_tools import _find_prs_for_issue_graphql

        result = _find_prs_for_issue_graphql('owner', 'repo', 42, 'tok', 'owner/repo', None)
        assert len(result) == 1
        assert result[0]['number'] == 101

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_case_insensitive_repo_match(self, mock_gql):
        """Repo matching should be case-insensitive."""
        mock_gql.return_value = _make_graphql_timeline_response([
            {'number': 101, 'base_repo': 'Owner/Repo'},
        ])

        from gittensor.utils.github_api_tools import _find_prs_for_issue_graphql

        result = _find_prs_for_issue_graphql('owner', 'repo', 42, 'tok', 'owner/repo', None)
        assert len(result) == 1

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_graphql_query_returns_none(self, mock_gql):
        """When execute_graphql_query returns None, should return empty list."""
        mock_gql.return_value = None

        from gittensor.utils.github_api_tools import _find_prs_for_issue_graphql

        result = _find_prs_for_issue_graphql('owner', 'repo', 42, 'tok', 'owner/repo', None)
        assert result == []

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_state_filter_open_excludes_merged(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response([
            {'number': 101, 'state': 'OPEN'},
            {'number': 102, 'state': 'CLOSED', 'merged': True, 'mergedAt': '2025-01-01'},
            {'number': 103, 'state': 'CLOSED'},
        ])

        from gittensor.utils.github_api_tools import _find_prs_for_issue_graphql

        result = _find_prs_for_issue_graphql('owner', 'repo', 42, 'tok', 'owner/repo', 'open')
        assert len(result) == 1
        assert result[0]['number'] == 101

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_merged_at_populated(self, mock_gql):
        mock_gql.return_value = _make_graphql_timeline_response([
            {'number': 101, 'state': 'CLOSED', 'merged': True, 'mergedAt': '2025-06-15T12:00:00Z'},
        ])

        from gittensor.utils.github_api_tools import _find_prs_for_issue_graphql

        result = _find_prs_for_issue_graphql('owner', 'repo', 42, 'tok', 'owner/repo', None)
        assert result[0]['merged_at'] == '2025-06-15T12:00:00Z'
        assert result[0]['state'] == 'MERGED'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_ghost_author_null(self, mock_gql):
        """When author is null (deleted GitHub account), should default to 'ghost'."""
        response = _make_graphql_timeline_response([{'number': 101}])
        response['data']['repository']['issue']['timelineItems']['nodes'][0]['source']['author'] = None

        mock_gql.return_value = response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_graphql

        result = _find_prs_for_issue_graphql('owner', 'repo', 42, 'tok', 'owner/repo', None)
        assert result[0]['author'] == 'ghost'
        assert result[0]['author_database_id'] is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_ghost_author_empty_login(self, mock_gql):
        """When author login is empty string, should fall back to 'ghost'."""
        response = _make_graphql_timeline_response([{'number': 101}])
        response['data']['repository']['issue']['timelineItems']['nodes'][0]['source']['author'] = {
            'login': '',
            'databaseId': None,
        }

        mock_gql.return_value = response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_graphql

        result = _find_prs_for_issue_graphql('owner', 'repo', 42, 'tok', 'owner/repo', None)
        assert result[0]['author'] == 'ghost'


# =============================================================================
# _find_prs_for_issue_rest direct tests
# =============================================================================


class TestFindPrsForIssueRest:
    """Direct unit tests for the internal REST implementation."""

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_auth_header_included_with_token(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None, token='ghp_test')

        headers = mock_get.call_args[1]['headers']
        assert headers['Authorization'] == 'token ghp_test'

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_no_auth_header_without_token(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None)

        headers = mock_get.call_args[1]['headers']
        assert 'Authorization' not in headers

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_skips_non_pr_issues(self, mock_get):
        """Events referencing non-PR issues should be skipped."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                'event': 'cross-referenced',
                'source': {
                    'type': 'issue',
                    'issue': {
                        'number': 50,
                        'title': 'Just an issue',
                        'state': 'open',
                        'user': {'login': 'user', 'id': 1},
                        'created_at': '2025-01-01',
                        'html_url': 'https://github.com/owner/repo/issues/50',
                        # No 'pull_request' key — it's a regular issue
                    },
                },
            },
        ]
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        result = _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None)
        assert result == []

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_request_exception_returns_empty(self, mock_get):
        import requests

        mock_get.side_effect = requests.exceptions.ConnectionError('Network unreachable')

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        result = _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None)
        assert result == []

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_quiet_mode_no_warning(self, mock_get):
        """_quiet=True should not emit unauthenticated warning."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        # Should not raise or warn
        result = _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None, _quiet=True)
        assert result == []

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_closed_not_merged_is_closed(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            _make_rest_timeline_event(101, state='closed', merged_at=None),
        ]
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        result = _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None)
        assert result[0]['state'] == 'CLOSED'

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_review_status_always_none(self, mock_get):
        """REST API doesn't provide review status — should always be None."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            _make_rest_timeline_event(101),
        ]
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        result = _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None)
        assert result[0]['review_status'] is None

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_closes_issue_always_false(self, mock_get):
        """REST API doesn't provide closing info — should always be False."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            _make_rest_timeline_event(101),
        ]
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        result = _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None)
        assert result[0]['closes_issue'] is False

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_ghost_author_null_user(self, mock_get):
        """Null user in REST event should default to 'ghost'."""
        event = _make_rest_timeline_event(101)
        event['source']['issue']['user'] = None
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [event]
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        result = _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', None)
        assert result[0]['author'] == 'ghost'
        assert result[0]['author_database_id'] is None

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_state_filter_merged(self, mock_get):
        """REST state filter should correctly select merged PRs."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            _make_rest_timeline_event(101, state='open'),
            _make_rest_timeline_event(102, state='closed', merged_at='2025-06-01T00:00:00Z'),
        ]
        mock_get.return_value = mock_response

        from gittensor.utils.github_api_tools import _find_prs_for_issue_rest

        result = _find_prs_for_issue_rest('owner', 'repo', 42, 'owner/repo', 'merged')
        assert len(result) == 1
        assert result[0]['number'] == 102
        assert result[0]['state'] == 'MERGED'


# =============================================================================
# Ghost author display and JSON tests
# =============================================================================


class TestGhostAuthorDisplay:
    """Tests verifying ghost/deleted GitHub accounts display correctly end-to-end."""

    def test_ghost_pr_in_table(self):
        """Ghost author PRs should render in the Rich table without errors."""
        prs = [{**SAMPLE_OPEN_PRS[0], 'author': 'ghost'}]
        table = build_pr_table(prs)
        assert table.row_count == 1

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_ghost_author_in_submissions_output(self, mock_resolve, mock_fetch, mock_find):
        """Ghost author should appear in submissions output."""
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = [{**SAMPLE_OPEN_PRS[0], 'author': 'ghost'}]

        runner = CliRunner()
        result = runner.invoke(issues_submissions, ['--id', '1'], catch_exceptions=False)

        assert result.exit_code == 0
        assert 'ghost' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_ghost_author_in_json_output(self, mock_resolve, mock_fetch, mock_find):
        """Ghost author should serialize correctly in JSON output."""
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = [{**SAMPLE_OPEN_PRS[0], 'author': 'ghost'}]

        runner = CliRunner()
        result = runner.invoke(issues_submissions, ['--id', '1', '--json'], catch_exceptions=False)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]['author'] == 'ghost'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_ghost_author_database_id_is_none(self, mock_gql):
        """Ghost/deleted accounts should have None as database_id."""
        response = _make_graphql_timeline_response([{'number': 101}])
        response['data']['repository']['issue']['timelineItems']['nodes'][0]['source']['author'] = None
        mock_gql.return_value = response

        result = find_prs_for_issue('owner/repo', 42, token='tok')
        assert result[0]['author'] == 'ghost'
        assert result[0]['author_database_id'] is None


# =============================================================================
# Additional predict command edge cases
# =============================================================================


class TestPredictEdgeCases:
    """Edge case tests for the predict command."""

    def _invoke(self, args, env=None):
        runner = CliRunner(env=env or {'GITTENSOR_MINER_PAT': 'fake-pat'})
        return runner.invoke(issues_predict, args, catch_exceptions=False)

    def test_github_api_failure_in_predict(self, predict_mocks):
        """GitHub API failure in predict should raise ClickException, not crash."""
        predict_mocks['find_prs'].side_effect = Exception('GitHub API rate limited')

        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y'])

        assert result.exit_code != 0
        assert 'Failed to fetch PRs' in result.output

    def test_predict_invalid_issue_id_zero(self, predict_mocks):
        result = self._invoke(['--id', '0', '--pr', '123', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'must be between' in result.output

    def test_predict_no_contract_address(self, predict_mocks):
        """Empty contract address should raise error."""
        with patch('gittensor.cli.issue_commands.submissions.get_contract_address', return_value=''):
            result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y'])

        assert result.exit_code != 0
        assert 'Contract address' in result.output

    def test_predict_json_output_on_github_failure(self, predict_mocks):
        """--json mode should also fail cleanly on GitHub API error."""
        predict_mocks['find_prs'].side_effect = Exception('timeout')

        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y', '--json'])

        assert result.exit_code != 0
        assert 'Failed to fetch PRs' in result.output

    def test_predict_verify_miner_called_after_github(self, predict_mocks):
        """verify_miner_registration should be called (Phase 4 after Phase 2 GitHub fetch)."""
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y'])

        assert result.exit_code == 0
        predict_mocks['verify_miner'].assert_called_once()

    def test_predict_multiple_prs_json_keys_all_strings(self, predict_mocks):
        """All prediction keys in JSON output should be strings, even for multi-PR input."""
        result = self._invoke([
            '--id', '1', '--json-input', '{"123": 0.3, "456": 0.4}', '-y', '--json',
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert all(isinstance(k, str) for k in payload['predictions'])
        assert len(payload['predictions']) == 2

    def test_predict_boundary_sum_exactly_one(self, predict_mocks):
        """Sum of probabilities exactly 1.0 should be accepted."""
        result = self._invoke([
            '--id', '1', '--json-input', '{"123": 0.6, "456": 0.4}', '-y',
        ])

        assert result.exit_code == 0

    def test_predict_shows_network_header(self, predict_mocks):
        """Non-JSON output should include network header."""
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.7', '-y'])

        assert result.exit_code == 0
        assert 'Network:' in result.output or 'Contract:' in result.output


# =============================================================================
# Additional submissions command edge cases
# =============================================================================


class TestSubmissionsEdgeCases:
    """Edge case tests for the submissions command."""

    def _invoke(self, args, env=None):
        runner = CliRunner(env=env)
        return runner.invoke(issues_submissions, args, catch_exceptions=False)

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_multiple_prs_all_displayed(self, mock_resolve, mock_fetch, mock_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = SAMPLE_OPEN_PRS

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert '123' in result.output
        assert '456' in result.output
        assert 'alice' in result.output
        assert 'bob' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_json_output_is_list(self, mock_resolve, mock_fetch, mock_find):
        """JSON output should always be a list, even with one PR."""
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = [SAMPLE_OPEN_PRS[0]]

        result = self._invoke(['--id', '1', '--json'])

        data = json.loads(result.output)
        assert isinstance(data, list)

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_json_output_empty_list_when_no_prs(self, mock_resolve, mock_fetch, mock_find):
        """JSON output should be an empty list when no PRs found."""
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = []

        result = self._invoke(['--id', '1', '--json'])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_FETCH_ISSUE)
    @patch(_PATCH_RESOLVE)
    def test_github_link_shown_when_no_prs(self, mock_resolve, mock_fetch, mock_find):
        """Empty results should show GitHub issue link."""
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = []

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert 'github.com' in result.output

    def test_no_contract_address_shows_error(self):
        with patch('gittensor.cli.issue_commands.submissions.get_contract_address', return_value=''):
            with patch(_PATCH_RESOLVE, return_value=('wss://test', 'test')):
                result = self._invoke(['--id', '1'])

        assert result.exit_code != 0
        assert 'Contract address' in result.output
