#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for gittensor.utils.mirror.client.

Covers the MirrorClient HTTP wrapper:
- URL + query-param construction for each endpoint
- since datetime → ISO UTC formatting; aware-input requirement
- Response parsing into the right dataclass
- Retry behavior: 5xx / 429 / connection errors retry with exponential backoff
- Fail-fast on non-429 4xx
- max_attempts exhaustion raises MirrorRequestError
"""

from datetime import datetime, timezone
from unittest.mock import Mock, call, patch

import pytest
import requests

mirror_client_module = pytest.importorskip(
    'gittensor.utils.mirror.client', reason='Requires gittensor package'
)
mirror_models = pytest.importorskip(
    'gittensor.utils.mirror.models', reason='Requires gittensor package'
)

MirrorClient = mirror_client_module.MirrorClient
MirrorRequestError = mirror_client_module.MirrorRequestError
MirrorPullRequestsResponse = mirror_models.MirrorPullRequestsResponse
MirrorIssuesResponse = mirror_models.MirrorIssuesResponse
MirrorPullRequestFilesResponse = mirror_models.MirrorPullRequestFilesResponse


# ============================================================================
# Helpers
# ============================================================================


def _ok(json_body: dict) -> Mock:
    """Build a 2xx response mock returning the given JSON."""
    response = Mock(status_code=200)
    response.json.return_value = json_body
    return response


def _err(status: int, body: str = 'error') -> Mock:
    """Build a non-2xx response mock."""
    return Mock(status_code=status, text=body)


def _make_client(session: Mock, **kwargs) -> MirrorClient:
    """Build a client wired to a mock session, defaults suitable for tests."""
    return MirrorClient(session=session, **kwargs)


def _minimal_pulls_payload() -> dict:
    return {
        'github_id': '218712309',
        'since': '2026-03-15T00:00:00Z',
        'generated_at': '2026-04-21T00:00:00Z',
        'pull_requests': [],
    }


def _minimal_issues_payload() -> dict:
    return {
        'github_id': '218712309',
        'since': '2026-03-15T00:00:00Z',
        'generated_at': '2026-04-21T00:00:00Z',
        'issues': [],
    }


def _minimal_files_payload() -> dict:
    return {
        'repo_full_name': 'entrius/gittensor-ui',
        'pr_number': 518,
        'head_sha': 'h',
        'base_sha': 'b',
        'merge_base_sha': 'mb',
        'scoring_data_stored': True,
        'files': [],
    }


# ============================================================================
# URL + param construction
# ============================================================================


class TestUrlConstruction:
    def test_get_miner_pulls_builds_correct_url(self):
        session = Mock()
        session.get.return_value = _ok(_minimal_pulls_payload())
        client = _make_client(session)

        client.get_miner_pulls('218712309')

        session.get.assert_called_once()
        url = session.get.call_args.args[0]
        assert url == 'https://mirror.gittensor.io/api/v1/miners/218712309/pulls'

    def test_get_miner_issues_builds_correct_url(self):
        session = Mock()
        session.get.return_value = _ok(_minimal_issues_payload())
        client = _make_client(session)

        client.get_miner_issues('218712309')

        url = session.get.call_args.args[0]
        assert url == 'https://mirror.gittensor.io/api/v1/miners/218712309/issues'

    def test_get_pr_files_interpolates_owner_repo_and_number(self):
        session = Mock()
        session.get.return_value = _ok(_minimal_files_payload())
        client = _make_client(session)

        client.get_pr_files('entrius/gittensor-ui', 518)

        url = session.get.call_args.args[0]
        assert url == 'https://mirror.gittensor.io/api/v1/pulls/entrius/gittensor-ui/518/files'

    def test_since_param_formatted_as_iso_utc(self):
        session = Mock()
        session.get.return_value = _ok(_minimal_pulls_payload())
        client = _make_client(session)

        since = datetime(2026, 3, 15, 12, 30, 45, tzinfo=timezone.utc)
        client.get_miner_pulls('218712309', since=since)

        params = session.get.call_args.kwargs['params']
        # ISO string with explicit UTC offset
        assert params['since'].startswith('2026-03-15T12:30:45')
        assert '+00:00' in params['since'] or params['since'].endswith('Z')

    def test_since_param_omitted_when_none(self):
        session = Mock()
        session.get.return_value = _ok(_minimal_pulls_payload())
        client = _make_client(session)

        client.get_miner_pulls('218712309')

        # params should be None or not contain 'since'
        params = session.get.call_args.kwargs.get('params')
        assert params is None or 'since' not in params

    def test_non_utc_aware_since_converted_to_utc(self):
        """A datetime in another tz should serialize as UTC equivalent."""
        from datetime import timedelta

        session = Mock()
        session.get.return_value = _ok(_minimal_pulls_payload())
        client = _make_client(session)

        # 2026-03-15 06:30 in UTC-6 == 2026-03-15 12:30 UTC
        non_utc = datetime(
            2026, 3, 15, 6, 30, 0, tzinfo=timezone(timedelta(hours=-6))
        )
        client.get_miner_pulls('218712309', since=non_utc)

        params = session.get.call_args.kwargs['params']
        assert params['since'].startswith('2026-03-15T12:30:00')


# ============================================================================
# Response parsing
# ============================================================================


class TestResponseParsing:
    def test_get_miner_pulls_returns_parsed_dataclass(self):
        session = Mock()
        session.get.return_value = _ok(_minimal_pulls_payload())
        client = _make_client(session)

        result = client.get_miner_pulls('218712309')

        assert isinstance(result, MirrorPullRequestsResponse)
        assert result.github_id == '218712309'

    def test_get_miner_issues_returns_parsed_dataclass(self):
        session = Mock()
        session.get.return_value = _ok(_minimal_issues_payload())
        client = _make_client(session)

        result = client.get_miner_issues('218712309')

        assert isinstance(result, MirrorIssuesResponse)

    def test_get_pr_files_returns_parsed_dataclass(self):
        session = Mock()
        session.get.return_value = _ok(_minimal_files_payload())
        client = _make_client(session)

        result = client.get_pr_files('entrius/gittensor-ui', 518)

        assert isinstance(result, MirrorPullRequestFilesResponse)
        assert result.repo_full_name == 'entrius/gittensor-ui'


# ============================================================================
# Retry behavior
# ============================================================================


@patch('gittensor.utils.mirror.client.time.sleep')
@patch('gittensor.utils.mirror.client.bt.logging')
class TestRetryBehavior:
    def test_500_then_success_retries_with_backoff(self, _log, mock_sleep):
        session = Mock()
        session.get.side_effect = [
            _err(500, 'oops'),
            _ok(_minimal_pulls_payload()),
        ]
        client = _make_client(session)

        client.get_miner_pulls('218712309')

        assert session.get.call_count == 2
        # Backoff after the first failure: 5s (formula: min(5 * 2**attempt, 30) at attempt=0)
        mock_sleep.assert_called_once_with(5)

    def test_502_502_success_uses_exponential_backoff(self, _log, mock_sleep):
        session = Mock()
        session.get.side_effect = [
            _err(502),
            _err(502),
            _ok(_minimal_pulls_payload()),
        ]
        client = _make_client(session, max_attempts=3)

        client.get_miner_pulls('218712309')

        assert session.get.call_count == 3
        # 5 * 2**0 = 5, then 5 * 2**1 = 10
        mock_sleep.assert_has_calls([call(5), call(10)])

    def test_429_is_retried(self, _log, mock_sleep):
        """429 (Cloudflare rate limit) should retry, unlike other 4xx."""
        session = Mock()
        session.get.side_effect = [
            _err(429, 'rate limited'),
            _ok(_minimal_pulls_payload()),
        ]
        client = _make_client(session)

        client.get_miner_pulls('218712309')

        assert session.get.call_count == 2

    def test_connection_error_retries(self, _log, mock_sleep):
        session = Mock()
        session.get.side_effect = [
            requests.ConnectionError('boom'),
            _ok(_minimal_pulls_payload()),
        ]
        client = _make_client(session)

        client.get_miner_pulls('218712309')

        assert session.get.call_count == 2
        mock_sleep.assert_called_once_with(5)

    def test_max_attempts_exhausted_raises(self, _log, mock_sleep):
        session = Mock()
        session.get.return_value = _err(503, 'unavailable')
        client = _make_client(session, max_attempts=3)

        with pytest.raises(MirrorRequestError, match='after 3 attempts'):
            client.get_miner_pulls('218712309')

        assert session.get.call_count == 3
        # 2 sleeps between 3 attempts (none after the last)
        assert mock_sleep.call_count == 2

    def test_max_attempts_exhausted_on_connection_errors(self, _log, mock_sleep):
        session = Mock()
        session.get.side_effect = requests.Timeout('slow')
        client = _make_client(session, max_attempts=3)

        with pytest.raises(MirrorRequestError, match='after 3 attempts'):
            client.get_miner_pulls('218712309')

        assert session.get.call_count == 3


@patch('gittensor.utils.mirror.client.time.sleep')
@patch('gittensor.utils.mirror.client.bt.logging')
class TestFailFast4xx:
    """4xx other than 429 indicates a client error — retry won't help."""

    def test_404_fails_fast_no_retry(self, _log, mock_sleep):
        session = Mock()
        session.get.return_value = _err(404, 'not found')
        client = _make_client(session, max_attempts=3)

        with pytest.raises(MirrorRequestError, match='404'):
            client.get_miner_pulls('218712309')

        assert session.get.call_count == 1
        mock_sleep.assert_not_called()

    def test_400_fails_fast_no_retry(self, _log, mock_sleep):
        session = Mock()
        session.get.return_value = _err(400, 'bad request')
        client = _make_client(session)

        with pytest.raises(MirrorRequestError, match='400'):
            client.get_miner_pulls('218712309')

        assert session.get.call_count == 1
        mock_sleep.assert_not_called()

    def test_403_fails_fast_no_retry(self, _log, mock_sleep):
        session = Mock()
        session.get.return_value = _err(403, 'forbidden')
        client = _make_client(session)

        with pytest.raises(MirrorRequestError, match='403'):
            client.get_pr_files('entrius/gittensor-ui', 518)

        assert session.get.call_count == 1


# ============================================================================
# Constructor defaults
# ============================================================================


class TestConstructorDefaults:
    def test_default_base_url_from_constants(self):
        from gittensor.constants import GITTENSOR_MIRROR_DEFAULT_URL

        client = MirrorClient()
        assert client.base_url == GITTENSOR_MIRROR_DEFAULT_URL.rstrip('/')

    def test_default_max_attempts_from_constants(self):
        from gittensor.constants import MIRROR_MAX_ATTEMPTS

        client = MirrorClient()
        assert client.max_attempts == MIRROR_MAX_ATTEMPTS
