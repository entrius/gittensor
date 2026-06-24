#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for execute_graphql_query permanent-error handling.

A permanent client error (bad/revoked token, forbidden) returns immediately
instead of consuming the full retry budget, while rate limiting and server
errors keep retrying with backoff.

Note: These tests require the full gittensor package to be importable.
Run with: python run_tests.py tests/utils/
"""

from unittest.mock import Mock, patch

import pytest

github_api_tools = pytest.importorskip(
    'gittensor.utils.github_api_tools', reason='Requires gittensor package with all dependencies'
)

execute_graphql_query = github_api_tools.execute_graphql_query


def _response(status_code, headers=None, json_data=None):
    """Build a mock requests.Response with the given status, headers, and JSON body."""
    response = Mock(status_code=status_code)
    response.headers = headers or {}
    if json_data is not None:
        response.json.return_value = json_data
    return response


class TestExecuteGraphqlQueryPermanentErrors:
    """Permanent client errors fail fast; transient conditions still retry."""

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_invalid_auth_401_fails_fast(self, mock_logging, mock_sleep, mock_post):
        mock_post.return_value = _response(401)

        result = execute_graphql_query('query {}', {}, 'bad_token')

        assert result is None
        assert mock_post.call_count == 1
        mock_sleep.assert_not_called()

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_non_rate_limited_403_fails_fast(self, mock_logging, mock_sleep, mock_post):
        # 403 without a rate-limit signal (remaining != 0, message has no 'rate limit').
        mock_post.return_value = _response(403, headers={}, json_data={'message': 'Forbidden'})

        result = execute_graphql_query('query {}', {}, 'fake_token')

        assert result is None
        assert mock_post.call_count == 1
        mock_sleep.assert_not_called()

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_rate_limited_403_still_retries(self, mock_logging, mock_sleep, mock_post):
        # Regression guard: a genuine rate-limited 403 must keep retrying, not fail fast.
        mock_post.side_effect = [
            _response(403, headers={'x-ratelimit-remaining': '0'}),
            _response(200, json_data={'data': {'ok': True}}),
        ]

        result = execute_graphql_query('query {}', {}, 'fake_token')

        assert result == {'data': {'ok': True}}
        assert mock_post.call_count == 2
        assert mock_sleep.call_count == 1

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_server_error_500_still_retries(self, mock_logging, mock_sleep, mock_post):
        # Regression guard: 5xx is transient and keeps the existing backoff retry.
        mock_post.side_effect = [
            _response(500),
            _response(200, json_data={'data': {'ok': True}}),
        ]

        result = execute_graphql_query('query {}', {}, 'fake_token')

        assert result == {'data': {'ok': True}}
        assert mock_post.call_count == 2
        assert mock_sleep.call_count == 1
