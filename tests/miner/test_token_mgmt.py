# The MIT License (MIT)
# Copyright © 2025 Entrius
# GitTensor Miner Token Management Tests

"""Unit tests for gittensor.miner.token_mgmt module."""

from unittest.mock import MagicMock, patch

import pytest

from gittensor.miner.token_mgmt import (
    _check_rate_limit,
    validate_token,
    is_token_valid,
)


class TestValidateToken:
    """Tests for the validate_token function."""

    @patch('gittensor.miner.token_mgmt.requests.get')
    def test_valid_token(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'login': 'testuser'}
        mock_response.headers = {
            'X-RateLimit-Remaining': '4999',
            'X-RateLimit-Limit': '5000',
        }
        mock_get.return_value = mock_response

        valid, message = validate_token('ghp_valid_token')
        assert valid is True
        assert 'testuser' in message

    @patch('gittensor.miner.token_mgmt.requests.get')
    def test_invalid_token_401(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        valid, message = validate_token('ghp_invalid')
        assert valid is False
        assert '401' in message

    @patch('gittensor.miner.token_mgmt.requests.get')
    def test_rate_limited_403(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.headers = {
            'X-RateLimit-Remaining': '0',
            'X-RateLimit-Reset': '1700000000',
        }
        mock_get.return_value = mock_response

        valid, message = validate_token('ghp_ratelimited')
        assert valid is False
        assert 'Rate limited' in message

    @patch('gittensor.miner.token_mgmt.requests.get')
    @patch('gittensor.miner.token_mgmt.time.sleep')
    def test_retries_on_timeout(self, mock_sleep, mock_get):
        import requests as req

        mock_get.side_effect = req.exceptions.Timeout('Connection timed out')

        valid, message = validate_token('ghp_timeout')
        assert valid is False
        assert 'Failed after' in message
        # Should have retried MAX_RETRIES - 1 times (sleep between attempts)
        assert mock_sleep.call_count == 2  # 3 attempts, 2 sleeps

    @patch('gittensor.miner.token_mgmt.requests.get')
    @patch('gittensor.miner.token_mgmt.time.sleep')
    def test_retries_on_connection_error(self, mock_sleep, mock_get):
        import requests as req

        mock_get.side_effect = req.exceptions.ConnectionError('DNS failure')

        valid, message = validate_token('ghp_connfail')
        assert valid is False
        assert mock_sleep.call_count == 2

    @patch('gittensor.miner.token_mgmt.requests.get')
    @patch('gittensor.miner.token_mgmt.time.sleep')
    def test_exponential_backoff(self, mock_sleep, mock_get):
        import requests as req

        mock_get.side_effect = req.exceptions.Timeout('timeout')

        validate_token('ghp_backoff')

        # First sleep: INITIAL_BACKOFF_SECONDS (2.0)
        # Second sleep: 2.0 * BACKOFF_MULTIPLIER (4.0)
        calls = mock_sleep.call_args_list
        assert calls[0][0][0] == pytest.approx(2.0)
        assert calls[1][0][0] == pytest.approx(4.0)

    @patch('gittensor.miner.token_mgmt.requests.get')
    def test_does_not_retry_on_401(self, mock_get):
        """Token revocation (401) should fail immediately without retrying."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        validate_token('ghp_revoked')
        assert mock_get.call_count == 1


class TestIsTokenValid:
    """Tests for the is_token_valid convenience wrapper."""

    @patch('gittensor.miner.token_mgmt.validate_token')
    def test_returns_true_for_valid(self, mock_validate):
        mock_validate.return_value = (True, 'ok')
        assert is_token_valid('ghp_valid') is True

    @patch('gittensor.miner.token_mgmt.validate_token')
    def test_returns_false_for_invalid(self, mock_validate):
        mock_validate.return_value = (False, 'bad')
        assert is_token_valid('ghp_invalid') is False


class TestCheckRateLimit:
    """Tests for the _check_rate_limit helper."""

    def test_warns_on_low_remaining(self):
        response = MagicMock()
        response.headers = {
            'X-RateLimit-Remaining': '50',
            'X-RateLimit-Limit': '5000',
        }
        # Should not raise
        _check_rate_limit(response)

    def test_no_warn_on_high_remaining(self):
        response = MagicMock()
        response.headers = {
            'X-RateLimit-Remaining': '4500',
            'X-RateLimit-Limit': '5000',
        }
        _check_rate_limit(response)

    def test_handles_missing_headers(self):
        response = MagicMock()
        response.headers = {}
        _check_rate_limit(response)
