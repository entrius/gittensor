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

get_github_graphql_query = github_api_tools.get_github_graphql_query
get_github_id = github_api_tools.get_github_id
get_github_account_age_days = github_api_tools.get_github_account_age_days
get_pull_request_file_changes = github_api_tools.get_pull_request_file_changes
find_prs_for_issue = github_api_tools.find_prs_for_issue
execute_graphql_query = github_api_tools.execute_graphql_query


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
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_get_github_id_retry_logic(self, mock_logging, mock_sleep, mock_get, clear_github_cache):
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
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_get_github_account_age_retry_logic(self, mock_logging, mock_sleep, mock_get, clear_github_cache):
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
        assert mock_sleep.call_count == 2, 'Should sleep between attempts but not after the last one'
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
        """Test that backoff delays are 5s, 10s for 3 attempts (no sleep after last attempt)."""
        mock_500 = Mock(status_code=500, text='Internal Server Error')
        mock_get.return_value = mock_500

        get_pull_request_file_changes('owner/repo', 1, 'fake_token')

        mock_sleep.assert_has_calls([call(5), call(10)])
        assert mock_sleep.call_count == 2, 'Should sleep between attempts but not after the last one'

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_no_sleep_after_final_http_error(self, mock_logging, mock_sleep, mock_get):
        """Verify no unnecessary sleep occurs after the final failed HTTP attempt."""
        mock_403 = Mock(status_code=403, text='Forbidden')
        mock_get.return_value = mock_403

        get_pull_request_file_changes('owner/repo', 42, 'fake_token')

        assert mock_get.call_count == 3, 'Should try exactly 3 times'
        assert mock_sleep.call_count == 2, 'Should only sleep between retries, not after the last attempt'

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_no_sleep_after_final_connection_error(self, mock_logging, mock_sleep, mock_get):
        """Verify no unnecessary sleep occurs after the final failed connection attempt."""
        import requests

        mock_get.side_effect = requests.exceptions.Timeout('timed out')

        get_pull_request_file_changes('owner/repo', 42, 'fake_token')

        assert mock_get.call_count == 3, 'Should try exactly 3 times'
        assert mock_sleep.call_count == 2, 'Should only sleep between retries, not after the last attempt'


# ============================================================================
# execute_graphql_query Retry Logic Tests
# ============================================================================


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


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_rest')
@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_prefers_graphql_when_results_found(mock_graphql, mock_rest):
    graphql_prs = [{'number': 101, 'state': 'OPEN'}]
    mock_graphql.return_value = graphql_prs

    result = find_prs_for_issue('owner/repo', 12, open_only=True, token='fake_token')

    assert result == graphql_prs
    mock_graphql.assert_called_once_with('owner/repo', 12, 'fake_token', open_only=True)
    mock_rest.assert_not_called()


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_rest')
@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_falls_back_to_authenticated_rest_when_graphql_empty(mock_graphql, mock_rest):
    mock_graphql.return_value = []
    rest_prs = [{'number': 102, 'state': 'OPEN'}]
    mock_rest.side_effect = [rest_prs]

    result = find_prs_for_issue('owner/repo', 12, open_only=True, token='fake_token')

    assert result == rest_prs
    mock_graphql.assert_called_once_with('owner/repo', 12, 'fake_token', open_only=True)
    mock_rest.assert_called_once_with('owner/repo', 12, token='fake_token', state='open')


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_rest')
@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_falls_back_to_unauthenticated_rest_when_auth_paths_empty(mock_graphql, mock_rest):
    mock_graphql.return_value = []
    unauth_prs = [{'number': 103, 'state': 'OPEN'}]
    mock_rest.side_effect = [[], unauth_prs]

    result = find_prs_for_issue('owner/repo', 12, open_only=True, token='fake_token')

    assert result == unauth_prs
    assert mock_rest.call_count == 2
    assert mock_rest.call_args_list[0].kwargs == {'token': 'fake_token', 'state': 'open'}
    assert mock_rest.call_args_list[1].kwargs == {'token': None, 'state': 'open'}


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_rest')
@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_uses_all_state_for_non_open_only(mock_graphql, mock_rest):
    mock_graphql.return_value = []
    mock_rest.side_effect = [[], []]

    result = find_prs_for_issue('owner/repo', 12, open_only=False, token='fake_token')

    assert result == []
    assert mock_rest.call_count == 2
    assert mock_rest.call_args_list[0].kwargs == {'token': 'fake_token', 'state': 'all'}
    assert mock_rest.call_args_list[1].kwargs == {'token': None, 'state': 'all'}


@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_rest')
@patch('gittensor.utils.github_api_tools._search_issue_referencing_prs_graphql')
def test_find_prs_without_token_only_uses_unauth_rest(mock_graphql, mock_rest):
    unauth_prs = [{'number': 104, 'state': 'OPEN'}]
    mock_rest.return_value = unauth_prs

    result = find_prs_for_issue('owner/repo', 12, open_only=True, token=None)

    assert result == unauth_prs
    mock_graphql.assert_not_called()
    mock_rest.assert_called_once_with('owner/repo', 12, token=None, state='open')


# ============================================================================
# Solver Detection Tests
# ============================================================================

find_solver_from_timeline = github_api_tools.find_solver_from_timeline
find_solver_from_cross_references = github_api_tools.find_solver_from_cross_references


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
    number, merged=True, merged_at='2025-06-01T00:00:00Z', user_id=42, base_repo='owner/repo', closing_issues=None
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
                'nodes': [{'number': n} for n in (closing_issues or [])],
            },
        },
    }


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


# ============================================================================
# load_miners_prs Per-PR Error Resilience Tests
# ============================================================================

load_miners_prs = github_api_tools.load_miners_prs


def _make_pr_node(
    number,
    repo_owner,
    repo_name,
    state='MERGED',
    created_at='2026-02-01T00:00:00Z',
    merged_at='2026-02-02T00:00:00Z',
    closed_at=None,
    default_branch='main',
    closing_issues_refs=None,
    head_repository=None,
):
    """Build a single PR node matching the GraphQL QUERY schema."""
    if closing_issues_refs is None:
        closing_issues_refs = {'nodes': []}
    return {
        'title': f'PR #{number}',
        'number': number,
        'additions': 10,
        'deletions': 2,
        'mergedAt': merged_at if state == 'MERGED' else None,
        'createdAt': created_at,
        'closedAt': closed_at,
        'lastEditedAt': None,
        'bodyText': 'test body',
        'state': state,
        'commits': {'totalCount': 1},
        'repository': {
            'name': repo_name,
            'owner': {'login': repo_owner},
            'defaultBranchRef': {'name': default_branch},
        },
        'headRepository': head_repository
        or {
            'name': repo_name,
            'owner': {'login': 'contributor'},
        },
        'baseRefName': default_branch,
        'baseRefOid': 'abc123',
        'headRefName': 'feature-branch',
        'headRefOid': 'def456',
        'author': {'login': 'contributor'},
        'authorAssociation': 'CONTRIBUTOR',
        'mergedBy': {'login': 'maintainer'} if state == 'MERGED' else None,
        'closingIssuesReferences': closing_issues_refs,
        'reviews': {'nodes': [{'author': {'login': 'reviewer'}}]},
    }


def _make_graphql_response(pr_nodes):
    """Wrap PR nodes in the full GraphQL response structure."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        'data': {
            'node': {
                'pullRequests': {
                    'pageInfo': {'hasNextPage': False, 'endCursor': None},
                    'nodes': pr_nodes,
                }
            }
        }
    }
    return mock_response


class TestLoadMinersPrsErrorResilience:
    """Test that a single bad PR doesn't abort fetching for the entire miner."""

    @patch('gittensor.utils.github_api_tools.get_github_graphql_query')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_null_closing_issues_skips_bad_pr_continues_rest(self, mock_logging, mock_graphql_query):
        """Simulate a banned/reinstated repo returning null for closingIssuesReferences.

        This is the exact scenario from affinefoundation/affinetes: the GraphQL API returns
        null for nested fields on PRs from a repo that was temporarily banned. The validator
        should skip that single PR and continue processing the rest.
        """
        from gittensor.classes import MinerEvaluation
        from gittensor.validator.utils.load_weights import RepositoryConfig

        good_pr_before = _make_pr_node(
            1, 'goodorg', 'goodrepo', created_at='2026-02-15T00:00:00Z', merged_at='2026-02-16T00:00:00Z'
        )
        bad_pr = _make_pr_node(
            2, 'affinefoundation', 'affinetes', created_at='2026-02-10T00:00:00Z', merged_at='2026-02-11T00:00:00Z'
        )
        # Simulate the banned repo returning null for closingIssuesReferences
        bad_pr['closingIssuesReferences'] = None
        good_pr_after = _make_pr_node(
            3, 'goodorg', 'goodrepo', created_at='2026-02-05T00:00:00Z', merged_at='2026-02-06T00:00:00Z'
        )

        mock_graphql_query.return_value = _make_graphql_response([good_pr_before, bad_pr, good_pr_after])

        master_repos = {
            'goodorg/goodrepo': RepositoryConfig(weight=1.0),
            'affinefoundation/affinetes': RepositoryConfig(weight=1.0),
        }
        miner_eval = MinerEvaluation(uid=74, hotkey='test_hotkey', github_id='12345', github_pat='fake_pat')

        load_miners_prs(miner_eval, master_repos)

        # Both good PRs should be collected; only the bad one is skipped
        assert len(miner_eval.merged_pull_requests) == 2, (
            f'Expected 2 merged PRs (skipping the bad one), got {len(miner_eval.merged_pull_requests)}'
        )
        collected_numbers = {pr.number for pr in miner_eval.merged_pull_requests}
        assert collected_numbers == {1, 3}, f'Expected PRs #1 and #3, got {collected_numbers}'

        # Verify the warning was logged for the bad PR
        warning_calls = [str(c) for c in mock_logging.warning.call_args_list]
        assert any('PR #2' in w for w in warning_calls), f'Expected a warning about PR #2, got: {warning_calls}'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
