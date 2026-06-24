#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for GitHub rate-limit aware retry backoff in github_api_tools.

Covers the header parsing helpers and the retry loops in get_github_identity and
execute_graphql_query honoring Retry-After / x-ratelimit-reset, capping the wait,
and deferring to a later cycle when the advertised window exceeds the cap.

Note: These tests require the full gittensor package to be importable.
Run with: python run_tests.py tests/utils/
"""

from unittest.mock import Mock, patch

import pytest

github_api_tools = pytest.importorskip(
    'gittensor.utils.github_api_tools', reason='Requires gittensor package with all dependencies'
)

_rate_limit_retry_after_seconds = github_api_tools._rate_limit_retry_after_seconds
_rate_limit_delay = github_api_tools._rate_limit_delay
get_github_identity = github_api_tools.get_github_identity
execute_graphql_query = github_api_tools.execute_graphql_query
GitHubIdentityStatus = github_api_tools.GitHubIdentityStatus
CAP = github_api_tools.GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS


def _response(status_code, headers=None, json_data=None):
    """Build a mock requests.Response with the given status, headers, and JSON body."""
    response = Mock(status_code=status_code)
    response.headers = headers or {}
    if json_data is not None:
        response.json.return_value = json_data
    return response


class TestRateLimitRetryAfterSeconds:
    """Header parsing for the advertised rate-limit wait."""

    def test_reads_retry_after_seconds(self):
        response = _response(429, {'Retry-After': '42'})
        assert _rate_limit_retry_after_seconds(response) == 42

    def test_reads_ratelimit_reset_with_injected_now(self):
        response = _response(403, {'x-ratelimit-reset': '1030'})
        assert _rate_limit_retry_after_seconds(response, now=1000) == 30

    def test_reset_in_the_past_clamps_to_zero(self):
        response = _response(403, {'x-ratelimit-reset': '990'})
        assert _rate_limit_retry_after_seconds(response, now=1000) == 0

    def test_retry_after_takes_precedence_over_reset(self):
        response = _response(429, {'Retry-After': '5', 'x-ratelimit-reset': '9999'})
        assert _rate_limit_retry_after_seconds(response, now=0) == 5

    def test_no_headers_returns_none(self):
        assert _rate_limit_retry_after_seconds(_response(429, {})) is None

    def test_malformed_headers_return_none(self):
        response = _response(429, {'Retry-After': 'soon'})
        assert _rate_limit_retry_after_seconds(response) is None


class TestRateLimitDelay:
    """Wait selection, cap enforcement, and fallback."""

    def test_within_cap_returns_advertised(self):
        response = _response(429, {'Retry-After': str(CAP - 1)})
        assert _rate_limit_delay(response, fallback_delay=2) == CAP - 1

    def test_over_cap_returns_none_to_abort(self):
        response = _response(429, {'Retry-After': str(CAP + 1)})
        assert _rate_limit_delay(response, fallback_delay=2) is None

    def test_falls_back_when_no_header(self):
        assert _rate_limit_delay(_response(429, {}), fallback_delay=7) == 7


class TestGetGithubIdentityRateLimit:
    """get_github_identity honors the advertised wait and defers long windows."""

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_waits_retry_after_then_succeeds(self, mock_logging, mock_sleep, mock_get):
        mock_get.side_effect = [
            _response(429, {'Retry-After': '5'}),
            _response(200, json_data={'id': 12345}),
        ]

        result = get_github_identity('fake_token')

        assert result.github_id == '12345'
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(5)

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_defers_when_window_exceeds_cap(self, mock_logging, mock_sleep, mock_get):
        mock_get.return_value = _response(429, {'Retry-After': str(CAP + 9999)})

        result = get_github_identity('fake_token')

        assert result.github_id is None
        assert result.status is GitHubIdentityStatus.TRANSIENT_FAILURE
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_falls_back_to_flat_delay_without_header(self, mock_logging, mock_sleep, mock_get):
        mock_get.side_effect = [
            _response(429, {}),
            _response(200, json_data={'id': 777}),
        ]

        result = get_github_identity('fake_token')

        assert result.github_id == '777'
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(2)


class TestExecuteGraphqlQueryRateLimit:
    """execute_graphql_query honors the advertised wait and defers long windows."""

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_waits_retry_after_then_succeeds(self, mock_logging, mock_sleep, mock_post):
        mock_post.side_effect = [
            _response(429, {'Retry-After': '5'}),
            _response(200, json_data={'data': {'ok': True}}),
        ]

        result = execute_graphql_query('query {}', {}, 'fake_token')

        assert result == {'data': {'ok': True}}
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(5)

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_defers_when_window_exceeds_cap(self, mock_logging, mock_sleep, mock_post):
        mock_post.return_value = _response(429, {'Retry-After': str(CAP + 9999)})

        result = execute_graphql_query('query {}', {}, 'fake_token')

        assert result is None
        assert mock_post.call_count == 1
        mock_sleep.assert_not_called()
