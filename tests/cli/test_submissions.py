# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for submissions and predict CLI commands, and find_prs_for_issue.
"""

import json
import sys
from unittest.mock import MagicMock, call, patch

import click
import pytest
from click.testing import CliRunner

from gittensor.cli.issue_commands.helpers import (
    collect_predictions,
    format_pred_lines,
    validate_predictions,
)
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

_PATCH_FIND_PRS = 'gittensor.cli.issue_commands.submissions.find_prs_for_issue'
_PATCH_FETCH_ISSUE = 'gittensor.cli.issue_commands.submissions.fetch_issue_from_contract'
_PATCH_RESOLVE = 'gittensor.cli.issue_commands.submissions.resolve_network'
_PATCH_READ_NETUID = 'gittensor.cli.issue_commands.submissions.read_netuid_from_contract'


# =============================================================================
# Pytest fixtures for predict command
# =============================================================================


@pytest.fixture
def predict_mocks():
    """Set up all mocks needed for predict command tests.

    After the flow reorder, issue fetch and PR fetch happen before wallet loading.
    The bittensor mock is only needed for the wallet/registration step.
    """
    # Mock bittensor module (inline import in predict command)
    bt_mod = MagicMock()
    wallet = MagicMock()
    wallet.hotkey.ss58_address = '5FakeHotkey123'
    bt_mod.Wallet.return_value = wallet

    metagraph = MagicMock()
    metagraph.hotkeys = ['5FakeHotkey123', '5OtherHotkey456']
    subtensor = MagicMock()
    subtensor.metagraph.return_value = metagraph
    bt_mod.Subtensor.return_value = subtensor

    with patch(_PATCH_RESOLVE) as mock_resolve, \
         patch(_PATCH_FETCH_ISSUE) as mock_fetch, \
         patch(_PATCH_FIND_PRS) as mock_find, \
         patch(_PATCH_READ_NETUID) as mock_netuid, \
         patch.dict(sys.modules, {'bittensor': bt_mod}):

        mock_resolve.return_value = ('wss://test.endpoint', 'test')
        mock_fetch.return_value = SAMPLE_ISSUE
        mock_find.return_value = SAMPLE_OPEN_PRS
        mock_netuid.return_value = 74

        yield {
            'bt': bt_mod,
            'resolve': mock_resolve,
            'fetch_issue': mock_fetch,
            'find_prs': mock_find,
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

    def test_invalid_issue_id_zero(self):
        result = self._invoke(['--id', '0'])

        assert result.exit_code != 0
        assert 'must be between' in result.output

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
        predict_mocks['fetch_issue'].side_effect = click.ClickException(
            'Issue 1 has status "Completed" — predictions require Active status.'
        )

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
