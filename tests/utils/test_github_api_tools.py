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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
