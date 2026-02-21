# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for submissions and predict CLI commands, and find_prs_for_issue.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.submissions import issues_predict, issues_submissions
from gittensor.utils.github_api_tools import find_prs_for_issue

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


# =============================================================================
# Patch targets
# =============================================================================

_PATCH_FIND_PRS = 'gittensor.cli.issue_commands.submissions._load_find_prs'
_PATCH_READ_ISSUES = 'gittensor.cli.issue_commands.submissions.read_issues_from_contract'
_PATCH_RESOLVE = 'gittensor.cli.issue_commands.submissions.resolve_network'
_PATCH_LOAD_BT = 'gittensor.cli.issue_commands.submissions._load_bittensor'
_PATCH_READ_NETUID = 'gittensor.cli.issue_commands.submissions.read_netuid_from_contract'


# =============================================================================
# Pytest fixtures for predict command
# =============================================================================


@pytest.fixture
def predict_mocks():
    """Set up all mocks needed for predict command tests.

    Yields a namespace with all mock objects for easy access and overriding.
    """
    with patch(_PATCH_LOAD_BT) as mock_load_bt, \
         patch(_PATCH_RESOLVE) as mock_resolve, \
         patch(_PATCH_READ_ISSUES) as mock_read, \
         patch(_PATCH_FIND_PRS) as mock_load_find, \
         patch(_PATCH_READ_NETUID) as mock_netuid:

        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_read.return_value = [SAMPLE_ISSUE]
        mock_netuid.return_value = 74

        # Mock find_prs_for_issue (returned by _load_find_prs)
        find_prs_fn = MagicMock(return_value=SAMPLE_OPEN_PRS)
        mock_load_find.return_value = find_prs_fn

        # Mock bittensor module (returned by _load_bittensor)
        bt_mod = MagicMock()
        wallet = MagicMock()
        wallet.hotkey.ss58_address = '5FakeHotkey123'
        bt_mod.Wallet.return_value = wallet

        metagraph = MagicMock()
        metagraph.hotkeys = ['5FakeHotkey123', '5OtherHotkey456']
        subtensor = MagicMock()
        subtensor.metagraph.return_value = metagraph
        bt_mod.Subtensor.return_value = subtensor

        mock_load_bt.return_value = bt_mod

        yield {
            'load_bt': mock_load_bt,
            'bt': bt_mod,
            'resolve': mock_resolve,
            'read': mock_read,
            'load_find': mock_load_find,
            'find_prs': find_prs_fn,
            'netuid': mock_netuid,
            'wallet': wallet,
            'metagraph': metagraph,
            'subtensor': subtensor,
        }


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

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_rest_fallback_when_no_token(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                'event': 'cross-referenced',
                'source': {
                    'type': 'issue',
                    'issue': {
                        'number': 101,
                        'title': 'Fix bug',
                        'state': 'open',
                        'user': {'login': 'alice'},
                        'created_at': '2025-01-01T00:00:00Z',
                        'html_url': 'https://github.com/owner/repo/pull/101',
                        'pull_request': {'merged_at': None},
                    },
                },
            },
        ]
        mock_get.return_value = mock_response

        result = find_prs_for_issue('owner/repo', 42, token=None)

        assert len(result) == 1
        assert result[0]['number'] == 101
        assert result[0]['state'] == 'OPEN'
        mock_get.assert_called_once()

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_graphql_failure_returns_empty(self, mock_gql):
        mock_gql.return_value = None

        result = find_prs_for_issue('owner/repo', 42, token='fake-token')

        assert result == []


# =============================================================================
# submissions command tests
# =============================================================================


class TestSubmissionsCommand:
    def _invoke(self, args, env=None):
        runner = CliRunner(env=env)
        return runner.invoke(issues_submissions, args, catch_exceptions=False)

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_READ_ISSUES)
    @patch(_PATCH_RESOLVE)
    def test_displays_table(self, mock_resolve, mock_read, mock_load_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_read.return_value = [SAMPLE_ISSUE]
        mock_find = MagicMock(return_value=[SAMPLE_OPEN_PRS[0]])
        mock_load_find.return_value = mock_find

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert '123' in result.output
        assert 'Fix the bug' in result.output
        assert 'alice' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_READ_ISSUES)
    @patch(_PATCH_RESOLVE)
    def test_json_output(self, mock_resolve, mock_read, mock_load_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_read.return_value = [SAMPLE_ISSUE]
        mock_find = MagicMock(return_value=[SAMPLE_OPEN_PRS[0]])
        mock_load_find.return_value = mock_find

        result = self._invoke(['--id', '1', '--json'])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]['number'] == 123

    def test_invalid_issue_id_zero(self):
        result = self._invoke(['--id', '0'])

        assert result.exit_code != 0
        assert 'must be between' in result.output

    @patch(_PATCH_READ_ISSUES)
    @patch(_PATCH_RESOLVE)
    def test_issue_not_found(self, mock_resolve, mock_read):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_read.return_value = []

        result = self._invoke(['--id', '999'])

        assert result.exit_code != 0
        assert 'not found' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_READ_ISSUES)
    @patch(_PATCH_RESOLVE)
    def test_no_open_prs(self, mock_resolve, mock_read, mock_load_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_read.return_value = [SAMPLE_ISSUE]
        mock_load_find.return_value = MagicMock(return_value=[])

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert 'No open PRs found' in result.output

    @patch(_PATCH_FIND_PRS)
    @patch(_PATCH_READ_ISSUES)
    @patch(_PATCH_RESOLVE)
    def test_warns_on_non_active_status(self, mock_resolve, mock_read, mock_load_find):
        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_read.return_value = [{**SAMPLE_ISSUE, 'status': 'Completed'}]
        mock_load_find.return_value = MagicMock(return_value=[])

        result = self._invoke(['--id', '1'])

        assert result.exit_code == 0
        assert 'Completed' in result.output


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

    def test_probability_above_one_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '1.5', '-y'])

        assert result.exit_code != 0
        assert 'between 0.0 and 1.0' in result.output

    def test_negative_probability_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '-0.5', '-y'])

        assert result.exit_code != 0
        assert 'between 0.0 and 1.0' in result.output

    def test_sum_exceeds_one_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{"123": 0.7, "456": 0.5}', '-y'])

        assert result.exit_code != 0
        assert 'exceeds 1.0' in result.output

    def test_pr_not_in_open_prs_rejected(self, predict_mocks):
        result = self._invoke(['--id', '1', '--pr', '999', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'not an open PR' in result.output

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
        predict_mocks['read'].return_value = [{**SAMPLE_ISSUE, 'status': 'Completed'}]

        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'Completed' in result.output

    def test_unregistered_hotkey_rejected(self, predict_mocks):
        predict_mocks['wallet'].hotkey.ss58_address = '5NotRegistered'

        result = self._invoke(['--id', '1', '--pr', '123', '--probability', '0.5', '-y'])

        assert result.exit_code != 0
        assert 'not registered' in result.output

    def test_multiple_predictions_json_input(self, predict_mocks):
        result = self._invoke(['--id', '1', '--json-input', '{"123": 0.5, "456": 0.3}', '-y', '--json'])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert '123' in payload['predictions']
        assert '456' in payload['predictions']
