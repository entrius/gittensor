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

from gittensor.utils.github_api_tools import (
    get_user_merged_prs_graphql,
    get_github_id,
    get_github_account_age_days,
)


class TestGraphQLRetryLogic(unittest.TestCase):
    """Test suite for GraphQL request retry logic"""

    def setUp(self):
        """Set up test fixtures"""
        self.test_user_id = '12345'
        self.test_token = 'fake_github_token'
        self.master_repositories = {}

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
        mock_response_200.json.return_value = {
            'data': {
                'node': {
                    'pullRequests': {
                        'pageInfo': {'hasNextPage': False, 'endCursor': None},
                        'nodes': [],
                    }
                }
            }
        }

        mock_post.side_effect = [mock_response_502, mock_response_502, mock_response_200]

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_post.call_count, 3, "Should retry 3 times total")
        self.assertEqual(mock_sleep.call_count, 2, "Should sleep twice between retries")
        self.assertEqual(result, [])
        self.assertEqual(count, 0)

        # Verify 15 second wait between retries
        sleep_calls = [call(15), call(15)]
        mock_sleep.assert_has_calls(sleep_calls)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_gives_up_after_three_502s(self, mock_logging, mock_sleep, mock_post):
        """Test that function gives up after 3 failed 502 attempts"""

        mock_response_502 = Mock()
        mock_response_502.status_code = 502
        mock_response_502.text = "<html><title>502 Bad Gateway</title></html>"

        mock_post.return_value = mock_response_502

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_post.call_count, 3, "Should try exactly 3 times")
        self.assertEqual(mock_sleep.call_count, 2, "Should sleep twice")
        self.assertEqual(result, [])
        self.assertEqual(count, 0)

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
        mock_response_200.json.return_value = {
            'data': {
                'node': {
                    'pullRequests': {
                        'pageInfo': {'hasNextPage': False, 'endCursor': None},
                        'nodes': [],
                    }
                }
            }
        }

        mock_post.side_effect = [mock_response_503, mock_response_200]

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_post.call_count, 2, "Should retry once after 503")
        self.assertEqual(mock_sleep.call_count, 1, "Should sleep once")

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
        mock_response_200.json.return_value = {
            'data': {
                'node': {
                    'pullRequests': {
                        'pageInfo': {'hasNextPage': False, 'endCursor': None},
                        'nodes': [],
                    }
                }
            }
        }

        mock_post.side_effect = [mock_response_504, mock_response_200]

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_post.call_count, 2, "Should retry once after 504")

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_no_retry_on_401_unauthorized(self, mock_logging, mock_sleep, mock_post):
        """Test that function does NOT retry on 401 Unauthorized (non-retryable error)"""

        mock_response_401 = Mock()
        mock_response_401.status_code = 401
        mock_response_401.text = "Unauthorized"

        mock_post.return_value = mock_response_401

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify - should only try once, not retry
        self.assertEqual(mock_post.call_count, 1, "Should NOT retry on 401")
        self.assertEqual(mock_sleep.call_count, 0, "Should not sleep")
        self.assertEqual(result, [])

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_no_retry_on_404_not_found(self, mock_logging, mock_sleep, mock_post):
        """Test that function does NOT retry on 404 Not Found (non-retryable error)"""

        mock_response_404 = Mock()
        mock_response_404.status_code = 404
        mock_response_404.text = "Not Found"

        mock_post.return_value = mock_response_404

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_post.call_count, 1, "Should NOT retry on 404")
        self.assertEqual(mock_sleep.call_count, 0, "Should not sleep")

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_on_connection_error(self, mock_logging, mock_sleep, mock_post):
        """Test that function retries on connection errors"""
        import requests

        # Simulate connection error on first two attempts, then success
        mock_response_200 = Mock()
        mock_response_200.status_code = 200
        mock_response_200.json.return_value = {
            'data': {
                'node': {
                    'pullRequests': {
                        'pageInfo': {'hasNextPage': False, 'endCursor': None},
                        'nodes': [],
                    }
                }
            }
        }

        mock_post.side_effect = [
            requests.exceptions.ConnectionError("Connection refused"),
            requests.exceptions.ConnectionError("Connection refused"),
            mock_response_200,
        ]

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_post.call_count, 3, "Should retry after connection errors")
        self.assertEqual(mock_sleep.call_count, 2, "Should sleep twice")

        # Verify 15 second wait between retries
        sleep_calls = [call(15), call(15)]
        mock_sleep.assert_has_calls(sleep_calls)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_gives_up_after_three_connection_errors(self, mock_logging, mock_sleep, mock_post):
        """Test that function gives up after 3 connection errors"""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_post.call_count, 3, "Should try 3 times before giving up")
        self.assertEqual(result, [])
        self.assertEqual(count, 0)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_successful_request_no_retry(self, mock_logging, mock_post):
        """Test that successful requests don't trigger retry logic"""

        mock_response_200 = Mock()
        mock_response_200.status_code = 200
        mock_response_200.json.return_value = {
            'data': {
                'node': {
                    'pullRequests': {
                        'pageInfo': {'hasNextPage': False, 'endCursor': None},
                        'nodes': [],
                    }
                }
            }
        }

        mock_post.return_value = mock_response_200

        # Execute
        result, count = get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_post.call_count, 1, "Should only call once on success")
        self.assertEqual(result, [])
        self.assertEqual(count, 0)


class TestOtherGitHubAPIFunctions(unittest.TestCase):
    """Test suite for other GitHub API functions with existing retry logic"""

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_get_github_id_retry_logic(self, mock_sleep, mock_get):
        """Test that get_github_id retries on failure"""

        # First two fail, third succeeds
        mock_response_fail = Mock()
        mock_response_fail.status_code = 500
        mock_response_fail.json.side_effect = Exception("Failed")

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
        mock_response_success.json.return_value = {'created_at': '2020-01-01T00:00:00Z'}

        mock_get.side_effect = [
            Exception("Timeout"),
            mock_response_success,
        ]

        # Execute
        result = get_github_account_age_days('fake_token')

        # Verify
        self.assertIsNotNone(result)
        self.assertIsInstance(result, int)
        self.assertGreater(result, 1000)  # Account older than 1000 days
        self.assertEqual(mock_get.call_count, 2)


if __name__ == '__main__':
    unittest.main()
