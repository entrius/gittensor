# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for github_api_tools module
"""

import sys
import unittest
from unittest.mock import Mock, call, patch, AsyncMock, MagicMock
import asyncio
import aiohttp

# Mock the circular import dependencies before importing the module
sys.modules['gittensor.validator'] = Mock()
sys.modules['gittensor.validator.utils'] = Mock()
sys.modules['gittensor.validator.utils.config'] = Mock()
sys.modules['gittensor.validator.utils.config'].MERGED_PR_LOOKBACK_DAYS = 30

from gittensor.utils.github_api_tools import (
    get_user_merged_prs_graphql,
    get_github_id,
    get_github_account_age_days,
    get_pull_request_file_changes,
)


def create_mock_response(status, json_data=None, text_data=""):
    """Helper to create a mock aiohttp response context manager"""
    response = AsyncMock()
    response.status = status
    if json_data is not None:
        response.json.return_value = json_data
    response.text.return_value = text_data
    
    # The context manager returned by session.get/post
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    
    return context


class TestGraphQLRetryLogic(unittest.IsolatedAsyncioTestCase):
    """Test suite for GraphQL request retry logic"""

    def setUp(self):
        """Set up test fixtures"""
        self.test_user_id = '12345'
        self.test_token = 'fake_github_token'
        self.master_repositories = {}

    @patch('gittensor.utils.github_api_tools.aiohttp.ClientSession')
    @patch('gittensor.utils.github_api_tools.asyncio.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    async def test_retry_on_error_status_then_success(self, mock_logging, mock_sleep, mock_session_cls):
        """Test that function retries on error status and succeeds on subsequent attempt"""

        mock_session = MagicMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        # First call returns 502, second returns 200
        ctx_502 = create_mock_response(502, text_data="Bad Gateway")
        ctx_200 = create_mock_response(200, json_data={
            'data': {
                'node': {
                    'pullRequests': {
                        'pageInfo': {'hasNextPage': False, 'endCursor': None},
                        'nodes': [],
                    }
                }
            }
        })

        mock_session.post.side_effect = [ctx_502, ctx_200]

        # Execute
        result = await get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_session.post.call_count, 2, "Should retry once")
        self.assertEqual(mock_sleep.call_count, 1, "Should sleep once between retries")
        self.assertEqual(result.valid_prs, [])
        
        mock_sleep.assert_called_with(5)


    @patch('gittensor.utils.github_api_tools.aiohttp.ClientSession')
    @patch('gittensor.utils.github_api_tools.asyncio.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    async def test_gives_up_after_max_retries(self, mock_logging, mock_sleep, mock_session_cls):
        """Test that function gives up after max retries"""

        mock_session = MagicMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        # Always return 502
        ctx_502 = create_mock_response(502, text_data="Bad Gateway")
        mock_session.post.return_value = ctx_502

        # Execute
        result = await get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_session.post.call_count, 6, "Should try exactly 6 times")
        self.assertEqual(mock_sleep.call_count, 5, "Should sleep 5 times")
        self.assertEqual(result.valid_prs, [])


    @patch('gittensor.utils.github_api_tools.aiohttp.ClientSession')
    @patch('gittensor.utils.github_api_tools.asyncio.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    async def test_retry_on_client_error(self, mock_logging, mock_sleep, mock_session_cls):
        """Test that function retries on aiohttp ClientError"""

        mock_session = MagicMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        ctx_200 = create_mock_response(200, json_data={
            'data': {
                'node': {
                    'pullRequests': {
                        'pageInfo': {'hasNextPage': False, 'endCursor': None},
                        'nodes': [],
                    }
                }
            }
        })

        mock_session.post.side_effect = [
            aiohttp.ClientError("Connection error"),
            ctx_200
        ]

        # Execute
        result = await get_user_merged_prs_graphql(self.test_user_id, self.test_token, self.master_repositories)

        # Verify
        self.assertEqual(mock_session.post.call_count, 2, "Should retry after exception")
        self.assertEqual(mock_sleep.call_count, 1, "Should sleep once")


class TestOtherGitHubAPIFunctions(unittest.IsolatedAsyncioTestCase):
    """Test suite for other GitHub API functions with existing retry logic"""
    
    def setUp(self):
        # Clear cache before each test
        from gittensor.utils.github_api_tools import _GITHUB_USER_CACHE
        _GITHUB_USER_CACHE.clear()

    @patch('gittensor.utils.github_api_tools.aiohttp.ClientSession')
    @patch('gittensor.utils.github_api_tools.asyncio.sleep')
    async def test_get_github_id_retry_logic(self, mock_sleep, mock_session_cls):
        """Test that get_github_id retries on failure"""
        
        mock_session = MagicMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        ctx_success = create_mock_response(200, json_data={'id': 12345, 'login': 'testuser'})

        # First two raise exception, third succeeds
        mock_session.get.side_effect = [
            Exception("Timeout"),
            Exception("Timeout"),
            ctx_success
        ]

        # Execute
        result = await get_github_id('fake_token')

        # Verify
        self.assertEqual(result, '12345')
        self.assertEqual(mock_session.get.call_count, 3)

    @patch('gittensor.utils.github_api_tools.aiohttp.ClientSession')
    @patch('gittensor.utils.github_api_tools.asyncio.sleep')
    async def test_get_github_account_age_retry_logic(self, mock_sleep, mock_session_cls):
        """Test that get_github_account_age_days retries on failure"""

        mock_session = MagicMock()
        mock_session_cls.return_value.__aenter__.return_value = mock_session

        ctx_success = create_mock_response(200, json_data={'created_at': '2020-01-01T00:00:00Z', 'login': 'testuser'})

        # First attempt fails, second succeeds
        mock_session.get.side_effect = [
            Exception("Timeout"),
            ctx_success
        ]

        # Execute
        result = await get_github_account_age_days('fake_token')

        # Verify
        self.assertIsNotNone(result)
        self.assertIsInstance(result, int)
        self.assertGreater(result, 1000)
        self.assertEqual(mock_session.get.call_count, 2)
    
    @patch('gittensor.utils.github_api_tools.aiohttp.ClientSession')
    @patch('gittensor.utils.github_api_tools.asyncio.sleep')
    async def test_get_pull_request_file_changes_with_session(self, mock_sleep, mock_session_cls):
        """Test get_pull_request_file_changes using an existing session"""
        
        # Create a specific session to pass
        existing_session = MagicMock()
        
        ctx_success = create_mock_response(200, json_data=[
            {'filename': 'test.py', 'additions': 10, 'deletions': 5, 'changes': 15, 'status': 'modified', 'patch': '@@ -1,5 +1,10 @@'}
        ])
        
        existing_session.get.return_value = ctx_success
        
        # Execute
        result = await get_pull_request_file_changes('owner/repo', 1, 'token', session=existing_session)
        
        # Verify
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(existing_session.get.call_count, 1)
        
        # Verify that ClientSession was NOT initialized (since we passed one)
        mock_session_cls.assert_not_called()


if __name__ == '__main__':
    unittest.main()
