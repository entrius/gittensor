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
classes_module = pytest.importorskip('gittensor.classes', reason='Requires gittensor package')
FileChange = classes_module.FileChange

get_github_graphql_query = github_api_tools.get_github_graphql_query
get_github_id = github_api_tools.get_github_id
get_github_account_age_days = github_api_tools.get_github_account_age_days
get_pull_request_file_changes = github_api_tools.get_pull_request_file_changes
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
# Rate Limit Handling Tests
# ============================================================================


class TestRateLimitHandling:
    """Test suite for GitHub API rate limit (429/403) and Retry-After handling."""

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_graphql_429_then_success_with_retry_after(self, mock_logging, mock_sleep, mock_post, graphql_params):
        """On 429, wait Retry-After seconds then retry; succeed on second attempt."""
        mock_429 = Mock(status_code=429, headers={'Retry-After': '3'}, text='rate limited')
        mock_200 = Mock(status_code=200)
        mock_200.json.return_value = {'data': {'node': None}}
        mock_post.side_effect = [mock_429, mock_200]

        result = get_github_graphql_query(**graphql_params)

        assert result is not None
        assert result.status_code == 200
        mock_sleep.assert_called_once_with(3)

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_get_github_user_403_rate_limit_then_success(self, mock_sleep, mock_get, clear_github_cache):
        """On 403 rate limit, wait Retry-After then retry; /user returns 200 on second attempt."""
        mock_403 = Mock(status_code=403, headers={'Retry-After': '2'}, text='rate limit')
        mock_200 = Mock(status_code=200)
        mock_200.json.return_value = {'id': 1, 'login': 'u'}
        mock_get.side_effect = [mock_403, mock_200]

        result = get_github_id('token')

        assert result == '1'
        mock_sleep.assert_called_once_with(2)


# ============================================================================
# FileChange.safe_from_github_response Tests
# ============================================================================


class TestSafeFromGithubResponse:
    """Test FileChange.safe_from_github_response edge cases."""

    def test_returns_none_for_non_dict(self):
        assert FileChange.safe_from_github_response(1, 'o/r', None) is None
        assert FileChange.safe_from_github_response(1, 'o/r', []) is None
        assert FileChange.safe_from_github_response(1, 'o/r', 'x') is None

    def test_returns_none_for_missing_required_keys(self):
        assert FileChange.safe_from_github_response(1, 'o/r', {'filename': 'a.py'}) is None
        assert FileChange.safe_from_github_response(
            1, 'o/r', {'filename': 'a.py', 'changes': 1, 'additions': 0, 'deletions': 0}
        ) is None  # missing status

    def test_returns_none_for_invalid_types(self):
        assert FileChange.safe_from_github_response(
            1, 'o/r', {'filename': 123, 'changes': 1, 'additions': 0, 'deletions': 0, 'status': 'm'}
        ) is None
        assert FileChange.safe_from_github_response(
            1, 'o/r', {'filename': 'a.py', 'changes': 'x', 'additions': 0, 'deletions': 0, 'status': 'm'}
        ) is None

    def test_returns_file_change_for_valid_input(self):
        fc = FileChange.safe_from_github_response(
            1,
            'owner/repo',
            {'filename': 'src/a.py', 'changes': 10, 'additions': 6, 'deletions': 4, 'status': 'modified'},
        )
        assert fc is not None
        assert fc.filename == 'src/a.py'
        assert fc.changes == 10
        assert fc.additions == 6
        assert fc.deletions == 4
        assert fc.status == 'modified'


# ============================================================================
# Safe FileChange and PR Files Response Tests
# ============================================================================


class TestSafeFileChangeAndPRFiles:
    """Test suite for get_pull_request_file_changes validation and safe FileChange parsing."""

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_pr_files_non_list_response_returns_empty(self, mock_get):
        """When API returns a non-list (e.g. error object), return empty list."""
        mock_get.return_value = Mock(status_code=200, json=lambda: {'message': 'Not Found'})

        result = get_pull_request_file_changes('owner/repo', 1, 'token')

        assert result == []

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_pr_files_malformed_entry_skipped(self, mock_get):
        """Valid list with one malformed file entry yields only valid FileChanges."""
        mock_get.return_value = Mock(
            status_code=200,
            json=lambda: [
                {'filename': 'a.py', 'changes': 10, 'additions': 5, 'deletions': 5, 'status': 'modified'},
                {'filename': 'b.py'},  # missing required keys
                {'filename': 'c.py', 'changes': 2, 'additions': 1, 'deletions': 1, 'status': 'added'},
            ],
        )

        result = get_pull_request_file_changes('owner/repo', 2, 'token')

        assert result is not None
        assert len(result) == 2
        assert result[0].filename == 'a.py'
        assert result[1].filename == 'c.py'

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_pr_files_429_wait_and_retry(self, mock_get):
        """On 429, wait Retry-After then retry; return files on success."""
        mock_429 = Mock(status_code=429, headers={'Retry-After': '1'})
        mock_200 = Mock(
            status_code=200,
            json=lambda: [{'filename': 'x.py', 'changes': 0, 'additions': 0, 'deletions': 0, 'status': 'modified'}],
        )
        mock_get.side_effect = [mock_429, mock_200]

        result = get_pull_request_file_changes('owner/repo', 3, 'token')

        assert result is not None
        assert len(result) == 1
        assert result[0].filename == 'x.py'


# ============================================================================
# GraphQL execute_graphql_query Rate Limit in Body
# ============================================================================


class TestExecuteGraphQLRateLimitInBody:
    """Test execute_graphql_query when 200 response contains rate limit errors."""

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_200_with_rate_limit_error_wait_and_retry(self, mock_sleep, mock_post):
        """When response is 200 but errors contain RATE_LIMITED, wait and retry."""
        rate_limited_body = {'data': None, 'errors': [{'type': 'RATE_LIMITED', 'message': 'rate limit exceeded'}]}
        success_body = {'data': {'repository': {'file0': {'text': 'x'}}}}
        mock_post.side_effect = [
            Mock(status_code=200, json=lambda: rate_limited_body),
            Mock(status_code=200, json=lambda: success_body),
        ]

        result = execute_graphql_query('query { x }', {}, 'token')

        assert result == success_body
        assert mock_sleep.call_count == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
