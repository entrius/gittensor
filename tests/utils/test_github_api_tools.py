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
import requests

# Use importorskip to gracefully handle import issues
github_api_tools = pytest.importorskip(
    'gittensor.utils.github_api_tools', reason='Requires gittensor package with all dependencies'
)

get_github_graphql_query = github_api_tools.get_github_graphql_query
get_github_id = github_api_tools.get_github_id
get_github_account_age_days = github_api_tools.get_github_account_age_days
get_pull_request_file_changes = github_api_tools.get_pull_request_file_changes


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def graphql_params():
    """Common parameters for GraphQL query tests."""
    return {
        'token': 'fake_github_token',
        'global_user_id': 'MDQ6VXNlcjEyMzQ1',  # Base64 encoded user ID
        'merged_pr_count': 0,
        'max_prs': 100,
        'cursor': None,
    }


@pytest.fixture
def mock_response_200():
    """Successful response mock."""
    response = Mock()
    response.status_code = 200
    return response


@pytest.fixture
def mock_response_502():
    """502 Bad Gateway response mock."""
    response = Mock()
    response.status_code = 502
    response.text = '<html><title>502 Bad Gateway</title></html>'
    return response


@pytest.fixture
def clear_github_cache():
    """Clear the GitHub user cache before test."""
    import gittensor.utils.github_api_tools as api_tools

    api_tools._GITHUB_USER_CACHE.clear()
    yield
    api_tools._GITHUB_USER_CACHE.clear()


# ============================================================================
# GraphQL Retry Logic Tests
# ============================================================================


class TestGraphQLRetryLogic:
    """Test suite for GraphQL request retry logic in get_github_graphql_query."""

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_502_then_success(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that function retries on 502 Bad Gateway and succeeds on third attempt."""
        mock_response_502 = Mock(status_code=502, text='<html><title>502 Bad Gateway</title></html>')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_502, mock_response_502, mock_response_200]

        result = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 3, 'Should retry 3 times total'
        assert mock_sleep.call_count == 2, 'Should sleep twice between retries'
        assert result is not None
        assert result.status_code == 200

        # Verify exponential backoff: 5s, 10s
        mock_sleep.assert_has_calls([call(5), call(10)])

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_502_halves_page_size_on_retry(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that 502 errors cause the page size (limit) to be halved on each retry."""
        mock_response_502 = Mock(status_code=502, text='502 Bad Gateway')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_502, mock_response_502, mock_response_200]

        result = get_github_graphql_query(**graphql_params)

        assert result.status_code == 200
        # Initial limit=100, halved to 50 after first 502, halved to 25 after second 502
        limits = [c.kwargs['json']['variables']['limit'] for c in mock_post.call_args_list]
        assert limits == [100, 50, 25]

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_page_size_floors_at_10(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that page size never drops below 10 even after many 502s."""
        mock_response_502 = Mock(status_code=502, text='502 Bad Gateway')
        mock_post.return_value = mock_response_502

        get_github_graphql_query(**graphql_params)

        # 100 -> 50 -> 25 -> 12 -> 10 -> 10 -> 10 -> 10
        limits = [c.kwargs['json']['variables']['limit'] for c in mock_post.call_args_list]
        assert limits == [100, 50, 25, 12, 10, 10, 10, 10]

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_non_5xx_does_not_reduce_page_size(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that non-5xx errors (e.g. 401) do not reduce page size."""
        mock_response_401 = Mock(status_code=401, text='Unauthorized')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_401, mock_response_401, mock_response_200]

        result = get_github_graphql_query(**graphql_params)

        assert result.status_code == 200
        limits = [c.kwargs['json']['variables']['limit'] for c in mock_post.call_args_list]
        assert limits == [100, 100, 100], 'Page size should stay at 100 for non-5xx errors'

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_503_and_504_also_reduce_page_size(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that 503 and 504 errors also trigger page size reduction."""
        mock_response_503 = Mock(status_code=503, text='Service Unavailable')
        mock_response_504 = Mock(status_code=504, text='Gateway Timeout')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_503, mock_response_504, mock_response_200]

        result = get_github_graphql_query(**graphql_params)

        assert result.status_code == 200
        limits = [c.kwargs['json']['variables']['limit'] for c in mock_post.call_args_list]
        assert limits == [100, 50, 25]

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_small_initial_limit_not_reduced_below_10(self, mock_logging, mock_sleep, mock_post):
        """Test that a small initial limit (e.g. 15) floors correctly at 10."""
        mock_response_502 = Mock(status_code=502, text='502 Bad Gateway')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_502, mock_response_502, mock_response_200]

        params = {
            'token': 'fake_github_token',
            'global_user_id': 'MDQ6VXNlcjEyMzQ1',
            'merged_pr_count': 85,
            'max_prs': 100,
            'cursor': None,
        }
        result = get_github_graphql_query(**params)

        assert result.status_code == 200
        # Initial limit = min(100, 100-85) = 15, halved to 10, stays at 10
        limits = [c.kwargs['json']['variables']['limit'] for c in mock_post.call_args_list]
        assert limits == [15, 10, 10]

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_gives_up_after_eight_attempts(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that function gives up after 8 failed attempts."""
        mock_response_502 = Mock(status_code=502, text='<html><title>502 Bad Gateway</title></html>')
        mock_post.return_value = mock_response_502

        result = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 8, 'Should try exactly 8 times'
        assert mock_sleep.call_count == 7, 'Should sleep 7 times between attempts'
        assert result is None
        mock_logging.error.assert_called()

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_503_service_unavailable(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that function retries on 503 Service Unavailable."""
        mock_response_503 = Mock(status_code=503, text='Service Unavailable')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_503, mock_response_200]

        result = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 2, 'Should retry once after 503'
        assert mock_sleep.call_count == 1, 'Should sleep once'
        assert result is not None

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_504_gateway_timeout(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that function retries on 504 Gateway Timeout."""
        mock_response_504 = Mock(status_code=504, text='Gateway Timeout')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_504, mock_response_200]

        result = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 2, 'Should retry once after 504'
        assert result is not None

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_401_unauthorized(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that function retries on 401 Unauthorized (all non-200 responses are retried)."""
        mock_response_401 = Mock(status_code=401, text='Unauthorized')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_401, mock_response_200]

        result = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 2, 'Should retry on 401'
        assert mock_sleep.call_count == 1, 'Should sleep once'
        assert result is not None

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_404_not_found(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that function retries on 404 Not Found (all non-200 responses are retried)."""
        mock_response_404 = Mock(status_code=404, text='Not Found')
        mock_response_200 = Mock(status_code=200)

        mock_post.side_effect = [mock_response_404, mock_response_200]

        _ = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 2, 'Should retry on 404'
        assert mock_sleep.call_count == 1, 'Should sleep once'

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_connection_error(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that function retries on connection errors."""
        import requests

        mock_response_200 = Mock(status_code=200)
        mock_post.side_effect = [
            requests.exceptions.ConnectionError('Connection refused'),
            requests.exceptions.ConnectionError('Connection refused'),
            mock_response_200,
        ]

        result = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 3, 'Should retry after connection errors'
        assert mock_sleep.call_count == 2, 'Should sleep twice'
        assert result is not None

        # Verify exponential backoff: 5s, 10s
        mock_sleep.assert_has_calls([call(5), call(10)])

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_gives_up_after_eight_connection_errors(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that function gives up after 8 connection errors."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError('Connection refused')

        result = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 8, 'Should try 8 times before giving up'
        assert result is None

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_successful_request_no_retry(self, mock_logging, mock_post, graphql_params):
        """Test that successful requests don't trigger retry logic."""
        mock_response_200 = Mock(status_code=200)
        mock_post.return_value = mock_response_200

        result = get_github_graphql_query(**graphql_params)

        assert mock_post.call_count == 1, 'Should only call once on success'
        assert result is not None
        assert result.status_code == 200

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_exponential_backoff_timing(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """Test that exponential backoff uses correct delays: 5s, 10s, 20s, 30s (capped), 30s, 30s, 30s."""
        mock_response_500 = Mock(status_code=500, text='Internal Server Error')
        mock_post.return_value = mock_response_500

        _ = get_github_graphql_query(**graphql_params)

        # Verify exponential backoff delays (capped at 30s)
        expected_delays = [call(5), call(10), call(20), call(30), call(30), call(30), call(30)]
        mock_sleep.assert_has_calls(expected_delays)
        assert mock_sleep.call_count == 7, 'Should sleep 7 times for 8 attempts'


# ============================================================================
# Other GitHub API Functions Tests
# ============================================================================


class TestOtherGitHubAPIFunctions:
    """Test suite for other GitHub API functions with existing retry logic."""

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_get_github_id_retry_logic(self, mock_sleep, mock_get, clear_github_cache):
        """Test that get_github_id retries on failure."""
        mock_response_success = Mock()
        mock_response_success.status_code = 200
        mock_response_success.json.return_value = {'id': 12345}

        mock_get.side_effect = [
            Exception('Timeout'),
            Exception('Timeout'),
            mock_response_success,
        ]

        result = get_github_id('fake_token')

        assert result == '12345'
        assert mock_get.call_count == 3

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_get_github_account_age_retry_logic(self, mock_sleep, mock_get, clear_github_cache):
        """Test that get_github_account_age_days retries on failure."""
        mock_response_success = Mock()
        mock_response_success.status_code = 200
        mock_response_success.json.return_value = {'id': 999, 'created_at': '2020-01-01T00:00:00Z'}

        mock_get.side_effect = [
            Exception('Timeout'),
            mock_response_success,
        ]

        result = get_github_account_age_days('fake_token_2')

        assert result is not None
        assert isinstance(result, int)
        assert result > 1000  # Account older than 1000 days
        assert mock_get.call_count == 2


# ============================================================================
# File Changes Retry Logic Tests
# ============================================================================


class TestFileChangesRetryLogic:
    """Test suite for retry logic in get_pull_request_file_changes."""

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_successful_request_no_retry(self, mock_get):
        """Test that a successful request returns file changes without retrying."""
        mock_response = Mock(status_code=200)
        mock_response.json.return_value = [
            {
                'filename': 'test.py',
                'status': 'modified',
                'changes': 2,
                'additions': 1,
                'deletions': 1,
                'patch': '@@ -1 +1 @@\n-old\n+new',
            },
        ]
        mock_get.return_value = mock_response

        result = get_pull_request_file_changes('owner/repo', 1, 'fake_token')

        assert mock_get.call_count == 1
        assert result is not None
        assert len(result) == 1

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_502_then_success(self, mock_logging, mock_sleep, mock_get):
        """Test that 502 triggers retry and succeeds on second attempt."""
        mock_502 = Mock(status_code=502, text='Bad Gateway')
        mock_200 = Mock(status_code=200)
        mock_200.json.return_value = [
            {'filename': 'test.py', 'status': 'modified', 'changes': 1, 'additions': 1, 'deletions': 0, 'patch': ''},
        ]

        mock_get.side_effect = [mock_502, mock_200]

        result = get_pull_request_file_changes('owner/repo', 1, 'fake_token')

        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1
        assert len(result) == 1

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_gives_up_after_three_attempts(self, mock_logging, mock_sleep, mock_get):
        """Test that function gives up after 3 failed attempts and returns empty list."""
        mock_500 = Mock(status_code=500, text='Internal Server Error')
        mock_get.return_value = mock_500

        result = get_pull_request_file_changes('owner/repo', 1, 'fake_token')

        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 3
        assert result == []
        mock_logging.error.assert_called()

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_connection_error_then_success(self, mock_logging, mock_sleep, mock_get):
        """Test that connection errors trigger retry."""
        import requests

        mock_200 = Mock(status_code=200)
        mock_200.json.return_value = []

        mock_get.side_effect = [requests.exceptions.ConnectionError('refused'), mock_200]

        result = get_pull_request_file_changes('owner/repo', 1, 'fake_token')

        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1
        assert result == []

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_gives_up_after_three_connection_errors(self, mock_logging, mock_sleep, mock_get):
        """Test that function gives up after 3 connection errors."""
        import requests

        mock_get.side_effect = requests.exceptions.ConnectionError('refused')

        result = get_pull_request_file_changes('owner/repo', 1, 'fake_token')

        assert mock_get.call_count == 3
        assert result == []
        mock_logging.error.assert_called()

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_exponential_backoff_timing(self, mock_logging, mock_sleep, mock_get):
        """Test that backoff delays are 5s, 10s for 3 attempts."""
        mock_500 = Mock(status_code=500, text='Internal Server Error')
        mock_get.return_value = mock_500

        get_pull_request_file_changes('owner/repo', 1, 'fake_token')

        mock_sleep.assert_has_calls([call(5), call(10), call(20)])
        assert mock_sleep.call_count == 3


# ============================================================================
# Solver Detection Tests
# ============================================================================

find_solver_from_timeline = github_api_tools.find_solver_from_timeline
find_solver_from_cross_references = github_api_tools.find_solver_from_cross_references
get_prs_referencing_issue = github_api_tools.get_prs_referencing_issue


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


def _pr_node(
    number,
    merged=True,
    merged_at='2025-06-01T00:00:00Z',
    user_id=42,
    base_repo='owner/repo',
    closing_issues=None,
    state=None,
):
    """Helper to build a single cross-referenced PR node (matches get_prs_referencing_issue shape)."""
    if state is None:
        state = 'MERGED' if merged else 'OPEN'
    return {
        'source': {
            'number': number,
            'merged': merged,
            'mergedAt': merged_at,
            'state': state,
            'title': f'PR #{number}',
            'createdAt': merged_at,
            'url': f'https://github.com/{base_repo}/pull/{number}',
            'author': {'databaseId': user_id, 'login': 'user'},
            'baseRepository': {'nameWithOwner': base_repo},
            'closingIssuesReferences': {
                'nodes': [{'number': n} for n in (closing_issues or [])],
            },
            'reviews': {'totalCount': 0},
        },
    }


class TestGetPrsReferencingIssue:
    """Test get_prs_referencing_issue (shared timeline PR fetch)."""

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_open_only_returns_only_open_prs(self, mock_graphql):
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=10, merged=False, state='OPEN', closing_issues=[12]),
                _pr_node(number=11, merged=True, state='MERGED', closing_issues=[12]),
            ]
        )
        prs = get_prs_referencing_issue('owner/repo', 12, 'token', open_only=True)
        assert len(prs) == 1
        assert prs[0]['number'] == 10
        assert prs[0]['state'] == 'OPEN'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_open_only_empty_when_no_open(self, mock_graphql):
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=11, merged=True, state='MERGED', closing_issues=[12]),
            ]
        )
        prs = get_prs_referencing_issue('owner/repo', 12, 'token', open_only=True)
        assert len(prs) == 0

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_ghost_author_returns_ghost_login(self, mock_graphql):
        node = _pr_node(number=10, merged=False, state='OPEN', closing_issues=[12])
        node['source']['author'] = None  # deleted GitHub account
        mock_graphql.return_value = _graphql_response([node])
        prs = get_prs_referencing_issue('owner/repo', 12, 'token')
        assert prs[0]['author_login'] == 'ghost'
        assert prs[0]['author_id'] is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_null_source_node_skipped(self, mock_graphql):
        mock_graphql.return_value = _graphql_response([{'source': {}}, {'source': None}])
        prs = get_prs_referencing_issue('owner/repo', 12, 'token')
        assert len(prs) == 0

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_missing_pr_number_skipped(self, mock_graphql):
        node = _pr_node(number=10, merged=False, state='OPEN', closing_issues=[12])
        del node['source']['number']
        mock_graphql.return_value = _graphql_response([node])
        prs = get_prs_referencing_issue('owner/repo', 12, 'token')
        assert len(prs) == 0

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_all_states_returned_when_open_only_false(self, mock_graphql):
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=10, merged=False, state='OPEN', closing_issues=[12]),
                _pr_node(number=11, merged=True, state='MERGED', closing_issues=[12]),
                _pr_node(number=12, merged=False, state='CLOSED', closing_issues=[12]),
            ]
        )
        prs = get_prs_referencing_issue('owner/repo', 12, 'token', open_only=False)
        assert len(prs) == 3

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_reviews_counted_correctly(self, mock_graphql):
        node = _pr_node(number=10, merged=False, state='OPEN', closing_issues=[12])
        node['source']['reviews'] = {'totalCount': 3}
        mock_graphql.return_value = _graphql_response([node])
        prs = get_prs_referencing_issue('owner/repo', 12, 'token')
        assert prs[0]['review_count'] == 3

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_closing_numbers_populated(self, mock_graphql):
        mock_graphql.return_value = _graphql_response(
            [_pr_node(number=10, merged=False, state='OPEN', closing_issues=[12, 15, 20])]
        )
        prs = get_prs_referencing_issue('owner/repo', 12, 'token')
        assert prs[0]['closing_numbers'] == [12, 15, 20]

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_empty_graphql_result_returns_empty(self, mock_graphql):
        mock_graphql.return_value = None
        prs = get_prs_referencing_issue('owner/repo', 12, 'token')
        assert prs == []

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_multiple_open_prs_all_returned(self, mock_graphql):
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=10, merged=False, state='OPEN', closing_issues=[12]),
                _pr_node(number=11, merged=False, state='OPEN', closing_issues=[12]),
                _pr_node(number=12, merged=False, state='OPEN', closing_issues=[12]),
            ]
        )
        prs = get_prs_referencing_issue('owner/repo', 12, 'token', open_only=True)
        assert len(prs) == 3
        assert {p['number'] for p in prs} == {10, 11, 12}

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_pr_from_different_repo_filtered(self, mock_graphql):
        mock_graphql.return_value = _graphql_response(
            [_pr_node(number=10, merged=False, state='OPEN', base_repo='other/repo', closing_issues=[12])]
        )
        prs = get_prs_referencing_issue('owner/repo', 12, 'token')
        assert len(prs) == 0


class TestFindSolverFromCrossReferences:
    """Test suite for find_solver_from_cross_references (GraphQL-based solver detection)."""

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_single_merged_pr_closing_issue(self, mock_logging, mock_graphql):
        """Single merged PR with closing reference returns correct solver."""
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=14, user_id=42, closing_issues=[12]),
            ]
        )

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_unmerged_pr_is_filtered_out(self, mock_logging, mock_graphql):
        """Unmerged PRs are ignored even if they have closing references."""
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=14, merged=False, user_id=42, closing_issues=[12]),
            ]
        )

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_pr_from_different_repo_is_filtered_out(self, mock_logging, mock_graphql):
        """PRs targeting a different base repo are rejected (prevents cross-repo gaming)."""
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=14, user_id=99, base_repo='attacker/evil-repo', closing_issues=[12]),
            ]
        )

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_pr_mentioning_but_not_closing_issue_is_filtered_out(self, mock_logging, mock_graphql):
        """PRs that mention the issue but don't have it in closingIssuesReferences are ignored."""
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=14, user_id=42, closing_issues=[99]),  # Closes #99, not #12
            ]
        )

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_multiple_candidates_picks_most_recent(self, mock_logging, mock_graphql):
        """When multiple merged PRs close the issue, the most recently merged one is selected."""
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=10, user_id=100, merged_at='2025-01-01T00:00:00Z', closing_issues=[12]),
                _pr_node(number=20, user_id=200, merged_at='2025-06-15T00:00:00Z', closing_issues=[12]),
                _pr_node(number=15, user_id=150, merged_at='2025-03-01T00:00:00Z', closing_issues=[12]),
            ]
        )

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id == 200
        assert pr_number == 20
        mock_logging.warning.assert_called()  # Should warn about multiple candidates

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_mixed_valid_and_invalid_candidates(self, mock_logging, mock_graphql):
        """Only valid candidates survive all filters (merged + same repo + closing ref)."""
        mock_graphql.return_value = _graphql_response(
            [
                # Invalid: unmerged
                _pr_node(number=10, merged=False, user_id=100, closing_issues=[12]),
                # Invalid: wrong repo
                _pr_node(number=11, user_id=101, base_repo='other/repo', closing_issues=[12]),
                # Invalid: doesn't close this issue
                _pr_node(number=13, user_id=103, closing_issues=[99]),
                # Valid
                _pr_node(number=14, user_id=42, merged_at='2025-06-01T00:00:00Z', closing_issues=[12]),
            ]
        )

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_fork_pr_targeting_main_repo_is_accepted(self, mock_logging, mock_graphql):
        """PRs from forks that target the main repo (baseRepository matches) are accepted."""
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(
                    number=14,
                    user_id=42,
                    base_repo='owner/repo',  # PR targets the main repo
                    closing_issues=[12],
                ),
            ]
        )

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_base_repo_check_is_case_insensitive(self, mock_logging, mock_graphql):
        """Base repo comparison is case-insensitive (GitHub repos are case-insensitive)."""
        mock_graphql.return_value = _graphql_response(
            [
                _pr_node(number=14, user_id=42, base_repo='Owner/Repo', closing_issues=[12]),
            ]
        )

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_no_cross_references_returns_none(self, mock_logging, mock_graphql):
        """Empty timeline nodes returns (None, None)."""
        mock_graphql.return_value = _graphql_response([])

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_graphql_query_failure_returns_none(self, mock_logging, mock_graphql):
        """GraphQL query failure returns (None, None)."""
        mock_graphql.return_value = None

        solver_id, pr_number = find_solver_from_cross_references('owner/repo', 12, 'fake_token')

        assert solver_id is None
        assert pr_number is None


class TestResolvePrState:
    """Test _resolve_pr_state state normalization."""

    def test_open_lowercased(self):
        assert github_api_tools._resolve_pr_state('open') == 'OPEN'

    def test_closed_lowercased(self):
        assert github_api_tools._resolve_pr_state('closed') == 'CLOSED'

    def test_already_uppercase(self):
        assert github_api_tools._resolve_pr_state('OPEN') == 'OPEN'

    def test_merged_flag_overrides_state(self):
        assert github_api_tools._resolve_pr_state('closed', merged=True) == 'MERGED'

    def test_merged_flag_overrides_open(self):
        assert github_api_tools._resolve_pr_state('open', merged=True) == 'MERGED'

    def test_mixed_case_normalized(self):
        assert github_api_tools._resolve_pr_state('Open') == 'OPEN'
        assert github_api_tools._resolve_pr_state('Closed') == 'CLOSED'

    def test_merged_flag_false_preserves_state(self):
        assert github_api_tools._resolve_pr_state('closed', merged=False) == 'CLOSED'


class TestSearchPrsRest:
    """Test _search_prs_rest REST Search API helper."""

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_parses_search_results(self, mock_get):
        mock_get.return_value.json.return_value = {
            'items': [
                {
                    'number': 10,
                    'title': 'Fix bug',
                    'state': 'open',
                    'html_url': 'https://github.com/owner/repo/pull/10',
                    'created_at': '2025-01-01T00:00:00Z',
                    'user': {'login': 'dev1'},
                },
            ],
        }
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert len(prs) == 1
        assert prs[0]['number'] == 10
        assert prs[0]['author_login'] == 'dev1'
        assert prs[0]['state'] == 'OPEN'
        assert prs[0]['review_count'] == 0

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_auth_token_sent_in_header(self, mock_get):
        mock_get.return_value.json.return_value = {'items': []}
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        github_api_tools._search_prs_rest('owner/repo', 42, token='my-token')
        headers = mock_get.call_args[1]['headers']
        # make_headers uses 'token ...', not 'Bearer ...'
        assert headers['Authorization'] == 'token my-token'

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_no_auth_header_without_token(self, mock_get):
        mock_get.return_value.json.return_value = {'items': []}
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        github_api_tools._search_prs_rest('owner/repo', 42, token=None)
        headers = mock_get.call_args[1]['headers']
        assert 'Authorization' not in headers

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_retry_on_500_success(self, mock_sleep, mock_get):
        # Fail with 500 first, then succeed
        bad_resp = Mock()
        bad_resp.status_code = 500
        bad_resp.raise_for_status.side_effect = requests.HTTPError('500 Server Error')

        good_resp = Mock()
        good_resp.status_code = 200
        good_resp.raise_for_status = lambda: None
        good_resp.json.return_value = {'items': [{'number': 1}]}

        mock_get.side_effect = [bad_resp, good_resp]

        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert len(prs) == 1
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_retry_exhausted_raises(self, mock_sleep, mock_get):
        # Fail with 500 always
        bad_resp = Mock()
        bad_resp.status_code = 500
        bad_resp.raise_for_status.side_effect = requests.HTTPError('500 Server Error')
        mock_get.return_value = bad_resp

        with pytest.raises(requests.HTTPError):
            github_api_tools._search_prs_rest('owner/repo', 42)

        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_retry_on_request_exception(self, mock_sleep, mock_get):
        # Raise RequestException first, then succeed
        good_resp = Mock()
        good_resp.status_code = 200
        good_resp.raise_for_status = lambda: None
        good_resp.json.return_value = {'items': [{'number': 1}]}

        mock_get.side_effect = [requests.exceptions.RequestException('Connection error'), good_resp]

        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert len(prs) == 1
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_skips_items_without_number(self, mock_get):
        mock_get.return_value.json.return_value = {
            'items': [{'title': 'no number', 'state': 'open', 'user': {'login': 'x'}}],
        }
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert len(prs) == 0

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_missing_user_defaults_to_ghost(self, mock_get):
        mock_get.return_value.json.return_value = {
            'items': [{'number': 5, 'title': 'X', 'state': 'open', 'html_url': '', 'user': None}],
        }
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert prs[0]['author_login'] == 'ghost'

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_absent_user_key_defaults_to_ghost(self, mock_get):
        mock_get.return_value.json.return_value = {
            'items': [{'number': 5, 'title': 'X', 'state': 'open', 'html_url': ''}],
        }
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert prs[0]['author_login'] == 'ghost'

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_http_error_propagates(self, mock_sleep, mock_get):
        # 403 should NOT retry (unless we decide otherwise, but currently loop catches RequestException and 5xx)
        # requests.HTTPError is a subclass of RequestException.
        # However, typically 4xx (client errors) are not retried in some logics, but my implementation retries RequestException.
        # Wait, my implementation:
        # if resp.status_code >= 500: raise_for_status() -> catches exception -> retries.
        # resp.raise_for_status() (outside if) -> catches exception -> retries.
        # So 403 will retry 3 times then raise.

        mock_get.return_value.status_code = 403
        mock_get.return_value.raise_for_status.side_effect = requests.HTTPError('403 rate limited')

        with pytest.raises(requests.HTTPError):
            github_api_tools._search_prs_rest('owner/repo', 42)

        # Should retry 3 times
        assert mock_get.call_count == 3

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_state_all_omits_state_clause(self, mock_get):
        mock_get.return_value.json.return_value = {'items': []}
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        github_api_tools._search_prs_rest('owner/repo', 42, state='all')
        query = mock_get.call_args[1]['params']['q']
        assert 'state:' not in query

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_state_open_includes_state_clause(self, mock_get):
        mock_get.return_value.json.return_value = {'items': []}
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        github_api_tools._search_prs_rest('owner/repo', 42, state='open')
        query = mock_get.call_args[1]['params']['q']
        assert 'state:open' in query

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_timeout_propagates(self, mock_sleep, mock_get):
        mock_get.side_effect = requests.Timeout('timed out')
        with pytest.raises(requests.Timeout):
            github_api_tools._search_prs_rest('owner/repo', 42)

        # Should retry 3 times
        assert mock_get.call_count == 3

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_multiple_items_all_parsed(self, mock_get):
        mock_get.return_value.json.return_value = {
            'items': [
                {'number': 1, 'title': 'A', 'state': 'open', 'html_url': '', 'user': {'login': 'a'}, 'created_at': ''},
                {'number': 2, 'title': 'B', 'state': 'open', 'html_url': '', 'user': {'login': 'b'}, 'created_at': ''},
                {'number': 3, 'title': 'C', 'state': 'open', 'html_url': '', 'user': {'login': 'c'}, 'created_at': ''},
            ],
        }
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert len(prs) == 3
        assert {p['number'] for p in prs} == {1, 2, 3}

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_closed_state_normalized_to_uppercase(self, mock_get):
        mock_get.return_value.json.return_value = {
            'items': [{'number': 5, 'title': 'X', 'state': 'closed', 'html_url': '', 'user': {'login': 'x'}}],
        }
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        prs = github_api_tools._search_prs_rest('owner/repo', 42, state='all')
        assert prs[0]['state'] == 'CLOSED'

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_empty_items_returns_empty(self, mock_get):
        mock_get.return_value.json.return_value = {'items': []}
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert prs == []

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_missing_items_key_returns_empty(self, mock_get):
        mock_get.return_value.json.return_value = {}
        mock_get.return_value.raise_for_status = lambda: None
        mock_get.return_value.status_code = 200
        prs = github_api_tools._search_prs_rest('owner/repo', 42)
        assert prs == []


class TestFindPrsForIssue:
    """Test find_prs_for_issue 3-level cascading PR discovery."""

    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue')
    def test_graphql_results_returned_directly(self, mock_graphql):
        mock_graphql.return_value = [{'number': 10, 'state': 'OPEN'}]
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok')
        assert len(prs) == 1
        mock_graphql.assert_called_once_with('owner/repo', 42, 'tok', open_only=True)

    @patch('gittensor.utils.github_api_tools._search_prs_rest')
    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[])
    def test_graphql_empty_cascades_to_auth_rest(self, mock_graphql, mock_rest):
        mock_rest.return_value = [{'number': 20}]
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok')
        assert prs[0]['number'] == 20
        mock_rest.assert_called_once_with('owner/repo', 42, token='tok', state='open')

    @patch('gittensor.utils.github_api_tools._search_prs_rest', side_effect=[[], [{'number': 30}]])
    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[])
    def test_full_three_level_cascade(self, mock_graphql, mock_rest):
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok')
        assert prs[0]['number'] == 30
        assert mock_rest.call_count == 2
        mock_rest.assert_any_call('owner/repo', 42, token='tok', state='open')
        mock_rest.assert_any_call('owner/repo', 42, token=None, state='open')

    @patch('gittensor.utils.github_api_tools._search_prs_rest')
    def test_no_token_skips_graphql(self, mock_rest):
        mock_rest.return_value = [{'number': 10}]
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token=None)
        assert len(prs) == 1
        mock_rest.assert_called_once_with('owner/repo', 42, token=None, state='open')

    @patch('gittensor.utils.github_api_tools._search_prs_rest', side_effect=Exception('network'))
    def test_all_levels_fail_returns_empty(self, mock_rest):
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token=None)
        assert prs == []

    @patch('gittensor.utils.github_api_tools._search_prs_rest')
    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', side_effect=Exception('gql fail'))
    def test_graphql_exception_cascades_to_rest(self, mock_graphql, mock_rest):
        mock_rest.return_value = [{'number': 10}]
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok')
        assert prs[0]['number'] == 10

    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue')
    def test_state_filter_all_passes_open_only_false(self, mock_graphql):
        mock_graphql.return_value = [{'number': 10}]
        github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok', state_filter='all')
        mock_graphql.assert_called_once_with('owner/repo', 42, 'tok', open_only=False)

    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue')
    def test_state_filter_open_passes_open_only_true(self, mock_graphql):
        mock_graphql.return_value = [{'number': 10}]
        github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok', state_filter='open')
        mock_graphql.assert_called_once_with('owner/repo', 42, 'tok', open_only=True)

    @patch('gittensor.utils.github_api_tools._search_prs_rest')
    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[])
    def test_state_filter_all_propagates_to_rest(self, mock_graphql, mock_rest):
        mock_rest.return_value = [{'number': 10}]
        github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok', state_filter='all')
        mock_rest.assert_called_once_with('owner/repo', 42, token='tok', state='all')

    @patch('gittensor.utils.github_api_tools.bt.logging')
    @patch('gittensor.utils.github_api_tools._search_prs_rest')
    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', side_effect=Exception('gql rate limit'))
    def test_logs_debug_on_graphql_failure(self, mock_graphql, mock_rest, mock_logging):
        mock_rest.return_value = [{'number': 10}]
        github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok')
        mock_logging.debug.assert_any_call('GraphQL PR fetch failed for owner/repo#42: gql rate limit')

    @patch('gittensor.utils.github_api_tools.bt.logging')
    @patch('gittensor.utils.github_api_tools._search_prs_rest', side_effect=[Exception('rest 403'), [{'number': 10}]])
    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[])
    def test_logs_debug_on_auth_rest_failure(self, mock_graphql, mock_rest, mock_logging):
        github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok')
        mock_logging.debug.assert_any_call('Authenticated REST search failed for owner/repo#42: rest 403')

    @patch('gittensor.utils.github_api_tools.bt.logging')
    @patch('gittensor.utils.github_api_tools._search_prs_rest', side_effect=Exception('network down'))
    def test_logs_debug_on_unauth_rest_failure(self, mock_rest, mock_logging):
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token=None)
        assert prs == []
        mock_logging.debug.assert_any_call('Unauthenticated REST search failed for owner/repo#42: network down')

    @patch('gittensor.utils.github_api_tools._search_prs_rest', side_effect=[[], []])
    @patch('gittensor.utils.github_api_tools.get_prs_referencing_issue', return_value=[])
    def test_all_levels_empty_returns_empty(self, mock_graphql, mock_rest):
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token='tok')
        assert prs == []

    @patch('gittensor.utils.github_api_tools._search_prs_rest')
    def test_no_token_with_state_all(self, mock_rest):
        mock_rest.return_value = [{'number': 10}]
        prs = github_api_tools.find_prs_for_issue('owner/repo', 42, token=None, state_filter='all')
        assert len(prs) == 1
        mock_rest.assert_called_once_with('owner/repo', 42, token=None, state='all')


class TestFindSolverFromTimeline:
    """Test that find_solver_from_timeline delegates to cross-references."""

    @patch('gittensor.utils.github_api_tools.find_solver_from_cross_references')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_delegates_to_cross_references(self, mock_logging, mock_cross_ref):
        """find_solver_from_timeline delegates directly to find_solver_from_cross_references."""
        mock_cross_ref.return_value = (42, 14)

        solver_id, pr_number = find_solver_from_timeline('owner/repo', 12, 'fake_token')

        assert solver_id == 42
        assert pr_number == 14
        mock_cross_ref.assert_called_once_with('owner/repo', 12, 'fake_token')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
