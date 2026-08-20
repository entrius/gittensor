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

from unittest.mock import Mock, patch

import pytest

# Use importorskip to gracefully handle import issues
github_api_tools = pytest.importorskip(
    'gittensor.utils.github_api_tools', reason='Requires gittensor package with all dependencies'
)

get_github_identity = github_api_tools.get_github_identity


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
