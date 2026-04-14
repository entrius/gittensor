# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for _fetch_closed_issues retry logic in repo_scan module."""

from unittest.mock import Mock, patch

import pytest

repo_scan = pytest.importorskip('gittensor.validator.issue_discovery.repo_scan', reason='Requires gittensor package')
_fetch_closed_issues = repo_scan._fetch_closed_issues


def _mock_response(status_code: int, json_data=None):
    """Create a mock requests.Response."""
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else []
    return resp


class TestFetchClosedIssuesRetry:
    @patch('time.sleep')
    @patch('requests.get')
    def test_success_on_first_attempt(self, mock_get, mock_sleep):
        issues = [{'number': 1, 'title': 'issue1'}]
        mock_get.side_effect = [
            _mock_response(200, issues),
            _mock_response(200, []),  # empty = end pagination
        ]
        result = _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        assert result == issues
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch('requests.get')
    def test_retry_on_502_then_success(self, mock_get, mock_sleep):
        issues = [{'number': 1, 'title': 'issue1'}]
        mock_get.side_effect = [
            _mock_response(502),
            _mock_response(200, issues),
            _mock_response(200, []),
        ]
        result = _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        assert result == issues
        assert mock_sleep.call_count == 1

    @patch('time.sleep')
    @patch('requests.get')
    def test_retry_on_503_then_success(self, mock_get, mock_sleep):
        issues = [{'number': 2, 'title': 'issue2'}]
        mock_get.side_effect = [
            _mock_response(503),
            _mock_response(503),
            _mock_response(200, issues),
            _mock_response(200, []),
        ]
        result = _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        assert result == issues
        assert mock_sleep.call_count == 2

    @patch('time.sleep')
    @patch('requests.get')
    def test_all_retries_exhausted_returns_partial(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            _mock_response(502),
            _mock_response(502),
            _mock_response(502),
        ]
        result = _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        assert result == []

    @patch('time.sleep')
    @patch('requests.get')
    def test_404_returns_immediately_no_retry(self, mock_get, mock_sleep):
        mock_get.return_value = _mock_response(404)
        result = _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        assert result == []
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch('requests.get')
    def test_422_returns_immediately_no_retry(self, mock_get, mock_sleep):
        mock_get.return_value = _mock_response(422)
        result = _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        assert result == []
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch('requests.get')
    def test_connection_error_retries(self, mock_get, mock_sleep):
        import requests as req

        issues = [{'number': 3, 'title': 'issue3'}]
        mock_get.side_effect = [
            req.ConnectionError('connection reset'),
            _mock_response(200, issues),
            _mock_response(200, []),
        ]
        result = _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        assert result == issues
        assert mock_sleep.call_count == 1

    @patch('time.sleep')
    @patch('requests.get')
    def test_exponential_backoff_timing(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            _mock_response(502),
            _mock_response(502),
            _mock_response(502),
        ]
        _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        # Backoff: 5*2^0=5, 5*2^1=10 (third attempt is last, no sleep after)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(5)
        mock_sleep.assert_any_call(10)

    @patch('time.sleep')
    @patch('requests.get')
    def test_pagination_with_retry_on_second_page(self, mock_get, mock_sleep):
        page1 = [{'number': 1}]
        page2 = [{'number': 2}]
        mock_get.side_effect = [
            _mock_response(200, page1),  # page 1 OK
            _mock_response(502),  # page 2 fail
            _mock_response(200, page2),  # page 2 retry OK
            _mock_response(200, []),  # page 3 empty = done
        ]
        result = _fetch_closed_issues('owner/repo', '2025-01-01T00:00:00Z', 'token')
        assert result == page1 + page2
        assert mock_sleep.call_count == 1
