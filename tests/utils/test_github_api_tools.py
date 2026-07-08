#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for github_api_tools module.

Tests the GitHub API interaction functions, particularly focusing on:
- Retry logic for transient failures (502, 503, 504)
- Exponential backoff behavior
- Error handling for various response codes
- Successful request scenarios

Note: These tests require the full gittensor package to be importable.
Run with: python run_tests.py tests/utils/
"""

from unittest.mock import Mock, call, patch

import pytest

# Use importorskip to gracefully handle import issues
github_api_tools = pytest.importorskip(
    'gittensor.utils.github_api_tools', reason='Requires gittensor package with all dependencies'
)

get_github_identity = github_api_tools.get_github_identity
find_prs_for_issue = github_api_tools.find_prs_for_issue
execute_graphql_query = github_api_tools.execute_graphql_query
check_github_issue_closed = github_api_tools.check_github_issue_closed


class TestOtherGitHubAPIFunctions:
    """Test suite for other GitHub API functions with existing retry logic."""

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_get_github_identity_retry_logic(self, mock_logging, mock_sleep, mock_get):
        """get_github_identity retries on transient failure then returns the id on success."""
        mock_response_success = Mock()
        mock_response_success.status_code = 200
        mock_response_success.json.return_value = {'id': 12345}

        mock_get.side_effect = [
            Exception('Timeout'),
            Exception('Timeout'),
            mock_response_success,
        ]

        result = get_github_identity('fake_token').github_id

        assert result == '12345'
        assert mock_get.call_count == 3

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_get_github_identity_marks_5xx_as_transient(self, mock_logging, mock_sleep, mock_get):
        mock_response = Mock(status_code=500)
        mock_get.return_value = mock_response

        result = get_github_identity('fake_token')

        assert result.github_id is None
        assert result.status is github_api_tools.GitHubIdentityStatus.TRANSIENT_FAILURE
        assert mock_get.call_count == 6

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_get_github_identity_marks_rate_limit_as_transient(self, mock_logging, mock_sleep, mock_get):
        mock_response = Mock(status_code=429)
        mock_get.return_value = mock_response

        result = get_github_identity('fake_token')

        assert result.github_id is None
        assert result.status is github_api_tools.GitHubIdentityStatus.TRANSIENT_FAILURE
        assert mock_get.call_count == 6

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_get_github_identity_marks_403_rate_limit_as_transient(self, mock_logging, mock_sleep, mock_get):
        mock_response = Mock(status_code=403)
        mock_response.headers = {'x-ratelimit-remaining': '0'}
        mock_get.return_value = mock_response

        result = get_github_identity('fake_token')

        assert result.github_id is None
        assert result.status is github_api_tools.GitHubIdentityStatus.TRANSIENT_FAILURE
        assert mock_get.call_count == 6

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_get_github_identity_fails_closed_on_auth_status(self, mock_logging, mock_sleep, mock_get):
        mock_response = Mock(status_code=403)
        mock_response.headers = {}
        mock_response.json.return_value = {'message': 'Resource not accessible by personal access token'}
        mock_get.return_value = mock_response

        result = get_github_identity('fake_token')

        assert result.github_id is None
        assert result.status is github_api_tools.GitHubIdentityStatus.INVALID_AUTH
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_get_github_identity_marks_bad_json_as_transient(self, mock_logging, mock_sleep, mock_get):
        mock_response = Mock(status_code=200)
        mock_response.json.side_effect = ValueError('bad json')
        mock_get.return_value = mock_response

        result = get_github_identity('fake_token')

        assert result.github_id is None
        assert result.status is github_api_tools.GitHubIdentityStatus.TRANSIENT_FAILURE
        assert mock_get.call_count == 6


class TestExecuteGraphQLQueryRetryLogic:
    """Test suite for retry logic in execute_graphql_query."""

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_exception_backoff_capped_at_30s(self, mock_logging, mock_sleep, mock_post):
        """Test that exception handler backoff delay is capped at 30 seconds."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError('Connection refused')

        result = execute_graphql_query('query {}', {}, 'fake_token', max_attempts=8)

        assert result is None
        # Verify delays are capped at 30: 5, 10, 20, 30, 30, 30, 30
        expected_delays = [call(5), call(10), call(20), call(30), call(30), call(30), call(30)]
        mock_sleep.assert_has_calls(expected_delays)
        assert mock_sleep.call_count == 7


# ============================================================================
# PR Discovery Fallback Tests
# ============================================================================


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_returns_graphql_results(mock_graphql):
    graphql_prs = [{'number': 101, 'state': 'OPEN'}]
    mock_graphql.return_value = graphql_prs

    result = find_prs_for_issue('owner/repo', 12, open_only=True, token='fake_token')

    assert result == graphql_prs
    mock_graphql.assert_called_once_with('owner/repo', 12, 'fake_token', open_only=True)


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_returns_empty_when_graphql_empty(mock_graphql):
    mock_graphql.return_value = []

    result = find_prs_for_issue('owner/repo', 12, open_only=True, token='fake_token')

    assert result == []
    mock_graphql.assert_called_once_with('owner/repo', 12, 'fake_token', open_only=True)


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_returns_none_when_graphql_errors(mock_graphql):
    # An exception during the GraphQL lookup is a failure, not "no PRs" — it must
    # surface as the None sentinel so callers can distinguish it from [].
    mock_graphql.side_effect = RuntimeError('boom')

    result = find_prs_for_issue('owner/repo', 12, open_only=True, token='fake_token')

    assert result is None
    mock_graphql.assert_called_once_with('owner/repo', 12, 'fake_token', open_only=True)


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_returns_none_when_graphql_lookup_fails(mock_graphql):
    # The GraphQL helper returns None on rate limit / network / GraphQL errors;
    # find_prs_for_issue must propagate it rather than collapsing to [].
    mock_graphql.return_value = None

    result = find_prs_for_issue('owner/repo', 12, open_only=True, token='fake_token')

    assert result is None
    mock_graphql.assert_called_once_with('owner/repo', 12, 'fake_token', open_only=True)


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_passes_open_only_false_to_graphql(mock_graphql):
    mock_graphql.return_value = []

    result = find_prs_for_issue('owner/repo', 12, open_only=False, token='fake_token')

    assert result == []
    mock_graphql.assert_called_once_with('owner/repo', 12, 'fake_token', open_only=False)


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_without_token_returns_empty(mock_graphql):
    result = find_prs_for_issue('owner/repo', 12, open_only=True, token=None)

    assert result == []
    mock_graphql.assert_not_called()


find_solver_from_closure_event = github_api_tools.find_solver_from_closure_event


def _graphql_response(nodes):
    """Helper to build a GraphQL cross-reference response."""
    return {
        'data': {
            'repository': {
                'issue': {
                    'timelineItems': {
                        'nodes': nodes,
                    },
                },
            },
        },
    }


def _closing_issue_node(number, repo='owner/repo'):
    node = {'number': number}
    if repo is not None:
        node['repository'] = {'nameWithOwner': repo}
    return node


def _pr_node(
    number,
    merged=True,
    merged_at='2025-06-01T00:00:00Z',
    user_id=42,
    base_repo='owner/repo',
    closing_issues=None,
    closing_repo='owner/repo',
):
    """Helper to build a single cross-referenced PR node."""
    return {
        'source': {
            'number': number,
            'merged': merged,
            'mergedAt': merged_at,
            'author': {'databaseId': user_id},
            'baseRepository': {'nameWithOwner': base_repo},
            'closingIssuesReferences': {
                'nodes': [_closing_issue_node(n, closing_repo) for n in (closing_issues or [])],
            },
        },
    }


class TestSearchIssueReferencingPrsGraphql:
    """Regression tests for parsing cross-referencing PR nodes.

    GitHub's GraphQL schema makes both the timeline ``nodes`` elements and
    ``PullRequest.baseRepository`` nullable: a ``node`` is ``null`` when the
    cross-reference lives in a repo the token can't see (redacted item), and
    ``baseRepository`` is ``null`` when the PR's base repo has been deleted.
    The sibling functions ``_select_current_close_event`` /
    ``_closing_issue_numbers_for_repo`` already skip null nodes and
    ``_solver_from_closed_event`` already guards a null ``baseRepository``;
    these tests pin the equivalent handling on the referencing path so a single
    null node or null-base PR cannot abort the whole submission lookup.
    """

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_null_timeline_node_is_skipped_not_crash(self, mock_logging, mock_graphql):
        mock_graphql.return_value = _graphql_response(
            [
                None,  # redacted cross-reference -> null node element
                _pr_node(number=101, base_repo='owner/repo'),
            ]
        )

        result = find_prs_for_issue('owner/repo', 12, open_only=False, token='fake_token')

        assert result is not None
        assert [pr['number'] for pr in result] == [101]

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_null_nodes_list_returns_empty(self, mock_logging, mock_graphql):
        mock_graphql.return_value = {'data': {'repository': {'issue': {'timelineItems': {'nodes': None}}}}}

        result = find_prs_for_issue('owner/repo', 12, open_only=False, token='fake_token')

        assert result == []

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_null_base_repository_node_is_skipped_not_crash(self, mock_logging, mock_graphql):
        null_base_node = {
            'source': {
                'number': 77,
                'merged': False,
                'author': {'databaseId': 7},
                'baseRepository': None,
                'closingIssuesReferences': {'nodes': []},
            },
        }
        mock_graphql.return_value = _graphql_response(
            [
                null_base_node,
                _pr_node(number=101, base_repo='owner/repo'),
            ]
        )

        result = find_prs_for_issue('owner/repo', 12, open_only=False, token='fake_token')

        assert result is not None
        assert [pr['number'] for pr in result] == [101]

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_only_null_base_repository_node_returns_empty(self, mock_logging, mock_graphql):
        mock_graphql.return_value = _graphql_response(
            [
                {'source': {'number': 77, 'baseRepository': None}},
            ]
        )

        result = find_prs_for_issue('owner/repo', 12, open_only=False, token='fake_token')

        assert result == []


def _closure_graphql_response(nodes, closed_at=None):
    """Helper to build a GraphQL closed-event response."""
    if closed_at is None and nodes:
        closed_at = nodes[-1].get('createdAt')
    return {
        'data': {
            'repository': {
                'issue': {
                    'closedAt': closed_at,
                    'timelineItems': {
                        'nodes': nodes,
                    },
                },
            },
        },
    }


def _closed_event_pr_node(
    number,
    merged=True,
    merged_at='2025-06-01T00:00:00Z',
    user_id=42,
    base_repo='owner/repo',
    state_reason='COMPLETED',
    created_at='2025-06-01T00:00:01Z',
):
    """Helper to build a single closed-event node whose closer is a PR."""
    return {
        'createdAt': created_at,
        'stateReason': state_reason,
        'closer': {
            '__typename': 'PullRequest',
            'number': number,
            'state': 'MERGED' if merged else 'OPEN',
            'merged': merged,
            'mergedAt': merged_at,
            'author': {'databaseId': user_id},
            'baseRepository': {'nameWithOwner': base_repo},
        },
    }


def _closed_event_non_pr_node(typename='Commit', state_reason='COMPLETED', created_at='2025-06-01T00:00:01Z'):
    return {
        'createdAt': created_at,
        'stateReason': state_reason,
        'closer': {
            '__typename': typename,
        },
    }


class TestFindSolverFromClosureEvent:
    """Test suite for solver detection through GitHub's authoritative close event."""

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_single_merged_pr_closing_issue(self, mock_logging, mock_graphql):
        """Single merged PR closer returns correct solver."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(number=14, user_id=42),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14
        query = mock_graphql.call_args.kwargs['query']
        assert 'CLOSED_EVENT' in query
        assert 'CROSS_REFERENCED_EVENT' not in query

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_unmerged_pr_is_filtered_out(self, mock_logging, mock_graphql):
        """Unmerged PR closers are ignored."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(number=14, merged=False, user_id=42),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_pr_from_different_repo_is_filtered_out(self, mock_logging, mock_graphql):
        """PR closers targeting a different base repo are rejected."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(number=14, user_id=99, base_repo='attacker/evil-repo'),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_pr_mentioning_but_not_closing_issue_is_filtered_out(self, mock_logging, mock_graphql):
        """Non-PR closers are not attributed to a miner."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_non_pr_node('Commit'),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_cross_repo_closing_issue_number_collision_is_filtered_out(self, mock_logging, mock_graphql):
        """Project-driven closures are not attributed to a miner."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_non_pr_node('ProjectV2'),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_closing_issue_repo_check_is_case_insensitive(self, mock_logging, mock_graphql):
        """GitHub repository names are case-insensitive when validating closer PRs."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(number=14, user_id=42, base_repo='Owner/Repo'),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_current_close_event_wins_over_stale_close_event(self, mock_logging, mock_graphql):
        """The current close event wins over stale close events from earlier cycles."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(
                    number=10,
                    user_id=100,
                    merged_at='2025-01-01T00:00:00Z',
                    created_at='2025-01-01T00:00:01Z',
                ),
                _closed_event_pr_node(
                    number=20,
                    user_id=200,
                    merged_at='2025-06-15T00:00:00Z',
                    created_at='2025-06-15T00:00:01Z',
                ),
            ],
            closed_at='2025-06-15T00:00:01Z',
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id == 200
        assert pr_number == 20

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_mixed_valid_and_invalid_candidates(self, mock_logging, mock_graphql):
        """An invalid current closure does not fall back to an older valid PR closure."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(
                    number=14,
                    user_id=42,
                    merged_at='2025-01-01T00:00:00Z',
                    created_at='2025-01-01T00:00:01Z',
                ),
                _closed_event_non_pr_node('Commit', created_at='2025-06-01T00:00:01Z'),
            ],
            closed_at='2025-06-01T00:00:01Z',
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_fork_pr_targeting_main_repo_is_accepted(self, mock_logging, mock_graphql):
        """Fork PRs that target the main repo (baseRepository matches) are accepted."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(
                    number=14,
                    user_id=42,
                    base_repo='owner/repo',  # PR targets the main repo
                ),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_base_repo_check_is_case_insensitive(self, mock_logging, mock_graphql):
        """Base repo comparison is case-insensitive (GitHub repos are case-insensitive)."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(number=14, user_id=42, base_repo='Owner/Repo'),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_no_close_events_returns_none(self, mock_logging, mock_graphql):
        """Empty timeline nodes returns (None, None)."""
        mock_graphql.return_value = _closure_graphql_response([])

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_missing_closed_at_does_not_use_stale_close_event(self, mock_logging, mock_graphql):
        """Without closedAt, an older close event is not authoritative enough to attribute."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(number=14, user_id=42),
            ],
            closed_at='',
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_non_completed_close_event_does_not_attribute_solver(self, mock_logging, mock_graphql):
        """Non-completed close events never produce a bounty solver."""
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(number=14, user_id=42, state_reason='NOT_PLANNED'),
            ]
        )

        solver_id, pr_number = find_solver_from_closure_event('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_graphql_query_failure_returns_none(self, mock_logging, mock_graphql):
        """GraphQL query failures return the lookup-failure sentinel."""
        for graphql_response in (None, {'errors': [{'message': 'rate limited'}]}):
            mock_graphql.return_value = graphql_response
            result = find_solver_from_closure_event('owner/repo', 12, 'fake_token')
            assert result is None


class TestCheckGithubIssueClosed:
    """Test issue state checks keep API failures distinct from no-solver cases."""

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_graphql_failure_sets_solver_lookup_failed(self, mock_logging, mock_get, mock_graphql):
        issue_response = Mock()
        issue_response.status_code = 200
        issue_response.json.return_value = {'state': 'closed', 'state_reason': 'completed'}
        mock_get.return_value = issue_response
        mock_graphql.return_value = None

        result = check_github_issue_closed('owner/repo', 12, 'fake_token')

        assert result == {
            'is_closed': True,
            'solver_github_id': None,
            'pr_number': None,
            'solver_lookup_failed': True,
        }

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_completed_issue_uses_closed_event_closer(self, mock_logging, mock_get, mock_graphql):
        issue_response = Mock()
        issue_response.status_code = 200
        issue_response.json.return_value = {'state': 'closed', 'state_reason': 'completed'}
        mock_get.return_value = issue_response
        mock_graphql.return_value = _closure_graphql_response(
            [
                _closed_event_pr_node(number=900, user_id=999),
            ]
        )

        result = check_github_issue_closed('owner/repo', 12, 'fake_token')

        assert result == {
            'is_closed': True,
            'solver_github_id': 999,
            'pr_number': 900,
            'solver_lookup_failed': False,
        }

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_closed_issue_with_no_solver_keeps_lookup_failed_false(self, mock_logging, mock_get, mock_graphql):
        issue_response = Mock()
        issue_response.status_code = 200
        issue_response.json.return_value = {'state': 'closed', 'state_reason': 'completed'}
        mock_get.return_value = issue_response
        mock_graphql.return_value = _graphql_response([])

        result = check_github_issue_closed('owner/repo', 12, 'fake_token')

        assert result == {
            'is_closed': True,
            'solver_github_id': None,
            'pr_number': None,
            'solver_lookup_failed': False,
        }

    @pytest.mark.parametrize(
        'issue_payload',
        [
            {'state': 'closed', 'state_reason': 'not_planned'},
            {'state': 'closed', 'state_reason': 'duplicate'},
            {'state': 'closed', 'state_reason': 'transferred'},
            {'state': 'closed', 'state_reason': None},
            {'state': 'closed'},
        ],
    )
    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_non_completed_closed_issue_skips_solver_lookup(self, mock_logging, mock_get, mock_graphql, issue_payload):
        issue_response = Mock()
        issue_response.status_code = 200
        issue_response.json.return_value = issue_payload
        mock_get.return_value = issue_response

        result = check_github_issue_closed('owner/repo', 12, 'fake_token')

        assert result == {
            'is_closed': True,
            'solver_github_id': None,
            'pr_number': None,
            'solver_lookup_failed': False,
        }
        mock_graphql.assert_not_called()
