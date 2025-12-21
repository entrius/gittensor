# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for github_api_tools module

Tests the GitHub API interaction functions, particularly focusing on:
- Retry logic for transient failures (502, 503, 504)
- Exponential backoff behavior
- Error handling for various response codes
- Successful request scenarios
"""

import sys
import unittest
from unittest.mock import Mock, call, patch

# Mock the circular import dependencies before importing the module
# This prevents the circular import error when running tests
sys.modules['gittensor.validator'] = Mock()
sys.modules['gittensor.validator.utils'] = Mock()
sys.modules['gittensor.validator.utils.config'] = Mock()
sys.modules['gittensor.validator.utils.config'].MERGED_PR_LOOKBACK_DAYS = 30

# Mock gittensor.classes to break circular import
mock_classes = Mock()
mock_classes.PRState = Mock()
mock_classes.PRState.OPEN = Mock(value='OPEN')
mock_classes.PRState.CLOSED = Mock(value='CLOSED')
mock_classes.FileChange = Mock()
mock_classes.MinerEvaluation = Mock()
sys.modules['gittensor.classes'] = mock_classes

from gittensor.utils.github_api_tools import (
    get_github_graphql_query,
    get_github_id,
    get_github_account_age_days,
)


class TestGraphQLRetryLogic(unittest.TestCase):
    """Test suite for GraphQL request retry logic in get_github_graphql_query"""

    def setUp(self):
        """Set up test fixtures"""
        self.test_token = 'fake_github_token'
        self.test_global_user_id = 'MDQ6VXNlcjEyMzQ1'  # Base64 encoded user ID
        self.merged_pr_count = 0
        self.max_prs = 100
        self.cursor = None

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_502_then_success(self, mock_logging, mock_sleep, mock_post):
        """Test that function retries on 502 Bad Gateway and succeeds on third attempt"""

        # First two calls return 502, third succeeds
        mock_response_502 = Mock()
        mock_response_502.status_code = 502
        mock_response_502.text = "<html><title>502 Bad Gateway</title></html>"

        mock_response_200 = Mock()
        mock_response_200.status_code = 200

        mock_post.side_effect = [mock_response_502, mock_response_502, mock_response_200]

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify
        self.assertEqual(mock_post.call_count, 3, "Should retry 3 times total")
        self.assertEqual(mock_sleep.call_count, 2, "Should sleep twice between retries")
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 200)

        # Verify exponential backoff: 5s, 10s
        sleep_calls = [call(5), call(10)]
        mock_sleep.assert_has_calls(sleep_calls)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_gives_up_after_six_attempts(self, mock_logging, mock_sleep, mock_post):
        """Test that function gives up after 6 failed attempts"""

        mock_response_502 = Mock()
        mock_response_502.status_code = 502
        mock_response_502.text = "<html><title>502 Bad Gateway</title></html>"

        mock_post.return_value = mock_response_502

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify
        self.assertEqual(mock_post.call_count, 6, "Should try exactly 6 times")
        self.assertEqual(mock_sleep.call_count, 5, "Should sleep 5 times between attempts")
        self.assertIsNone(result)

        # Verify error was logged
        mock_logging.error.assert_called()

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_503_service_unavailable(self, mock_logging, mock_sleep, mock_post):
        """Test that function retries on 503 Service Unavailable"""

        mock_response_503 = Mock()
        mock_response_503.status_code = 503
        mock_response_503.text = "Service Unavailable"

        mock_response_200 = Mock()
        mock_response_200.status_code = 200

        mock_post.side_effect = [mock_response_503, mock_response_200]

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify
        self.assertEqual(mock_post.call_count, 2, "Should retry once after 503")
        self.assertEqual(mock_sleep.call_count, 1, "Should sleep once")
        self.assertIsNotNone(result)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_504_gateway_timeout(self, mock_logging, mock_sleep, mock_post):
        """Test that function retries on 504 Gateway Timeout"""

        mock_response_504 = Mock()
        mock_response_504.status_code = 504
        mock_response_504.text = "Gateway Timeout"

        mock_response_200 = Mock()
        mock_response_200.status_code = 200

        mock_post.side_effect = [mock_response_504, mock_response_200]

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify
        self.assertEqual(mock_post.call_count, 2, "Should retry once after 504")
        self.assertIsNotNone(result)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_401_unauthorized(self, mock_logging, mock_sleep, mock_post):
        """Test that function retries on 401 Unauthorized (all non-200 responses are retried)"""

        mock_response_401 = Mock()
        mock_response_401.status_code = 401
        mock_response_401.text = "Unauthorized"

        mock_response_200 = Mock()
        mock_response_200.status_code = 200

        mock_post.side_effect = [mock_response_401, mock_response_200]

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify - retries on all non-200 responses
        self.assertEqual(mock_post.call_count, 2, "Should retry on 401")
        self.assertEqual(mock_sleep.call_count, 1, "Should sleep once")
        self.assertIsNotNone(result)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_404_not_found(self, mock_logging, mock_sleep, mock_post):
        """Test that function retries on 404 Not Found (all non-200 responses are retried)"""

        mock_response_404 = Mock()
        mock_response_404.status_code = 404
        mock_response_404.text = "Not Found"

        mock_response_200 = Mock()
        mock_response_200.status_code = 200

        mock_post.side_effect = [mock_response_404, mock_response_200]

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify
        self.assertEqual(mock_post.call_count, 2, "Should retry on 404")
        self.assertEqual(mock_sleep.call_count, 1, "Should sleep once")

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_connection_error(self, mock_logging, mock_sleep, mock_post):
        """Test that function retries on connection errors"""
        import requests

        # Simulate connection error on first two attempts, then success
        mock_response_200 = Mock()
        mock_response_200.status_code = 200

        mock_post.side_effect = [
            requests.exceptions.ConnectionError("Connection refused"),
            requests.exceptions.ConnectionError("Connection refused"),
            mock_response_200,
        ]

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify
        self.assertEqual(mock_post.call_count, 3, "Should retry after connection errors")
        self.assertEqual(mock_sleep.call_count, 2, "Should sleep twice")
        self.assertIsNotNone(result)

        # Verify exponential backoff: 5s, 10s
        sleep_calls = [call(5), call(10)]
        mock_sleep.assert_has_calls(sleep_calls)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_gives_up_after_six_connection_errors(self, mock_logging, mock_sleep, mock_post):
        """Test that function gives up after 6 connection errors"""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify
        self.assertEqual(mock_post.call_count, 6, "Should try 6 times before giving up")
        self.assertIsNone(result)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_successful_request_no_retry(self, mock_logging, mock_post):
        """Test that successful requests don't trigger retry logic"""

        mock_response_200 = Mock()
        mock_response_200.status_code = 200

        mock_post.return_value = mock_response_200

        # Execute
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify
        self.assertEqual(mock_post.call_count, 1, "Should only call once on success")
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 200)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_exponential_backoff_timing(self, mock_logging, mock_sleep, mock_post):
        """Test that exponential backoff uses correct delays: 5s, 10s, 20s, 40s, 80s"""

        mock_response_500 = Mock()
        mock_response_500.status_code = 500
        mock_response_500.text = "Internal Server Error"

        mock_post.return_value = mock_response_500

        # Execute - will fail all 6 attempts
        result = get_github_graphql_query(
            self.test_token, self.test_global_user_id, self.merged_pr_count, self.max_prs, self.cursor
        )

        # Verify exponential backoff delays
        expected_delays = [call(5), call(10), call(20), call(40), call(80)]
        mock_sleep.assert_has_calls(expected_delays)
        self.assertEqual(mock_sleep.call_count, 5, "Should sleep 5 times for 6 attempts")


class TestOtherGitHubAPIFunctions(unittest.TestCase):
    """Test suite for other GitHub API functions with existing retry logic"""

    def setUp(self):
        """Clear the GitHub user cache before each test"""
        import gittensor.utils.github_api_tools as api_tools
        api_tools._GITHUB_USER_CACHE.clear()

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_get_github_id_retry_logic(self, mock_sleep, mock_get):
        """Test that get_github_id retries on failure"""

        # First two fail, third succeeds
        mock_response_success = Mock()
        mock_response_success.status_code = 200
        mock_response_success.json.return_value = {'id': 12345}

        mock_get.side_effect = [
            Exception("Timeout"),
            Exception("Timeout"),
            mock_response_success,
        ]

        # Execute
        result = get_github_id('fake_token')

        # Verify
        self.assertEqual(result, '12345')
        self.assertEqual(mock_get.call_count, 3)

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_get_github_account_age_retry_logic(self, mock_sleep, mock_get):
        """Test that get_github_account_age_days retries on failure"""

        # First attempt fails, second succeeds
        mock_response_success = Mock()
        mock_response_success.status_code = 200
        mock_response_success.json.return_value = {'id': 999, 'created_at': '2020-01-01T00:00:00Z'}

        mock_get.side_effect = [
            Exception("Timeout"),
            mock_response_success,
        ]

        # Execute
        result = get_github_account_age_days('fake_token_2')

        # Verify
        self.assertIsNotNone(result)
        self.assertIsInstance(result, int)
        self.assertGreater(result, 1000)  # Account older than 1000 days
        self.assertEqual(mock_get.call_count, 2)


if __name__ == '__main__':
    unittest.main()
