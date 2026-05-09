# Entrius 2025

"""Tests for PAT broadcast and check handlers."""

import asyncio
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from bittensor.core.synapse import TerminalInfo

from gittensor.synapses import PatBroadcastSynapse, PatCheckSynapse
from gittensor.utils.github_api_tools import GitHubIdentityResult, GitHubIdentityStatus
from gittensor.validator import pat_storage
from gittensor.validator.pat_handler import (
    _test_pat_against_repo,
    blacklist_pat_broadcast,
    blacklist_pat_check,
    handle_pat_broadcast,
    handle_pat_check,
)
from gittensor.validator.utils.github_validation import GitHubCredentialValidation


def _validation(
    github_id: Optional[str] = 'github_42',
    error: Optional[str] = None,
    transient_failure: bool = False,
) -> GitHubCredentialValidation:
    """Build a GitHubCredentialValidation for mocking validate_github_credentials_result."""
    return GitHubCredentialValidation(github_id, error, transient_failure=transient_failure)


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def use_tmp_pats_file(tmp_path, monkeypatch):
    """Redirect PAT storage to a temporary file for each test."""
    tmp_file = tmp_path / 'miner_pats.json'
    monkeypatch.setattr(pat_storage, 'PATS_FILE', tmp_file)
    return tmp_file


@pytest.fixture
def mock_validator():
    """Create a mock validator with metagraph."""
    validator = MagicMock()
    validator.metagraph.hotkeys = ['hotkey_0', 'hotkey_1', 'hotkey_2']
    validator.metagraph.S = [100.0, 200.0, 300.0]
    return validator


def _make_dendrite(hotkey: str) -> TerminalInfo:
    return TerminalInfo(hotkey=hotkey)


def _make_broadcast_synapse(hotkey: str, pat: str = 'ghp_test123') -> PatBroadcastSynapse:
    synapse = PatBroadcastSynapse(github_access_token=pat)
    synapse.dendrite = _make_dendrite(hotkey)
    return synapse


def _make_check_synapse(hotkey: str) -> PatCheckSynapse:
    synapse = PatCheckSynapse()
    synapse.dendrite = _make_dendrite(hotkey)
    return synapse


# ---------------------------------------------------------------------------
# Blacklist tests
# ---------------------------------------------------------------------------


class TestBlacklistPatBroadcast:
    def test_registered_hotkey_accepted(self, mock_validator):
        synapse = _make_broadcast_synapse('hotkey_1')
        blocked, reason = _run(blacklist_pat_broadcast(mock_validator, synapse))
        assert blocked is False

    def test_unregistered_hotkey_rejected(self, mock_validator):
        synapse = _make_broadcast_synapse('unknown_hotkey')
        blocked, reason = _run(blacklist_pat_broadcast(mock_validator, synapse))
        assert blocked is True


class TestBlacklistPatCheck:
    def test_registered_hotkey_accepted(self, mock_validator):
        synapse = _make_check_synapse('hotkey_1')
        blocked, reason = _run(blacklist_pat_check(mock_validator, synapse))
        assert blocked is False

    def test_unregistered_hotkey_rejected(self, mock_validator):
        synapse = _make_check_synapse('unknown_hotkey')
        blocked, reason = _run(blacklist_pat_check(mock_validator, synapse))
        assert blocked is True


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


class TestHandlePatBroadcast:
    @patch('gittensor.validator.pat_handler._test_pat_against_repo', return_value=None)
    @patch('gittensor.validator.pat_handler.validate_github_credentials', return_value=('github_42', None))
    def test_valid_pat_accepted(self, mock_validate, mock_test_query, mock_validator):
        synapse = _make_broadcast_synapse('hotkey_1', pat='ghp_valid')
        result = _run(handle_pat_broadcast(mock_validator, synapse))

        assert result.accepted is True
        assert result.rejection_reason is None
        # PAT should be cleared from response
        assert result.github_access_token == ''

        # Verify PAT was stored by UID
        entry = pat_storage.get_pat_by_uid(1)
        assert entry is not None
        assert entry['pat'] == 'ghp_valid'
        assert entry['hotkey'] == 'hotkey_1'
        assert entry['uid'] == 1
        assert entry['github_id'] == 'github_42'

    def test_unregistered_hotkey_rejected(self, mock_validator):
        synapse = _make_broadcast_synapse('unknown_hotkey')
        result = _run(handle_pat_broadcast(mock_validator, synapse))

        assert result.accepted is False
        assert 'not registered' in (result.rejection_reason or '')

    @patch('gittensor.validator.pat_handler.validate_github_credentials', return_value=(None, 'PAT invalid'))
    def test_invalid_pat_rejected(self, mock_validate, mock_validator):
        synapse = _make_broadcast_synapse('hotkey_1', pat='ghp_bad')
        result = _run(handle_pat_broadcast(mock_validator, synapse))

        assert result.accepted is False
        assert 'PAT invalid' in (result.rejection_reason or '')

        # Verify PAT was NOT stored
        assert pat_storage.get_pat_by_uid(1) is None

    @patch('gittensor.validator.pat_handler._test_pat_against_repo', return_value='GitHub API returned 403')
    @patch('gittensor.validator.pat_handler.validate_github_credentials', return_value=('github_42', None))
    def test_test_query_failure_rejected(self, mock_validate, mock_test_query, mock_validator):
        synapse = _make_broadcast_synapse('hotkey_1')
        result = _run(handle_pat_broadcast(mock_validator, synapse))

        assert result.accepted is False
        assert '403' in (result.rejection_reason or '')

    @patch('gittensor.validator.pat_handler._test_pat_against_repo', return_value=None)
    @patch('gittensor.validator.pat_handler.validate_github_credentials', return_value=('github_99', None))
    def test_github_identity_change_rejected(self, mock_validate, mock_test_query, mock_validator):
        """Same hotkey cannot switch to a different GitHub account."""
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_old', 'github_42')

        synapse = _make_broadcast_synapse('hotkey_1', pat='ghp_new_account')
        result = _run(handle_pat_broadcast(mock_validator, synapse))

        assert result.accepted is False
        assert 'locked' in (result.rejection_reason or '').lower()

        # Original entry should be unchanged
        entry = pat_storage.get_pat_by_uid(1)
        assert entry is not None
        assert entry['github_id'] == 'github_42'

    @patch('gittensor.validator.pat_handler._test_pat_against_repo', return_value=None)
    @patch('gittensor.validator.pat_handler.validate_github_credentials', return_value=('github_42', None))
    def test_pat_rotation_same_github_accepted(self, mock_validate, mock_test_query, mock_validator):
        """Same hotkey can rotate PATs if GitHub identity stays the same."""
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_old', 'github_42')

        synapse = _make_broadcast_synapse('hotkey_1', pat='ghp_refreshed')
        result = _run(handle_pat_broadcast(mock_validator, synapse))

        assert result.accepted is True
        entry = pat_storage.get_pat_by_uid(1)
        assert entry is not None
        assert entry['pat'] == 'ghp_refreshed'
        assert entry['github_id'] == 'github_42'

    @patch('gittensor.validator.pat_handler._test_pat_against_repo', return_value=None)
    @patch('gittensor.validator.pat_handler.validate_github_credentials', return_value=('github_99', None))
    def test_new_miner_on_uid_can_use_any_github(self, mock_validate, mock_test_query, mock_validator):
        """A new hotkey on the same UID (new miner) can register any GitHub account."""
        pat_storage.save_pat(1, 'old_hotkey', 'ghp_old', 'github_42')

        synapse = _make_broadcast_synapse('hotkey_1', pat='ghp_new_miner')
        result = _run(handle_pat_broadcast(mock_validator, synapse))

        assert result.accepted is True
        entry = pat_storage.get_pat_by_uid(1)
        assert entry is not None
        assert entry['github_id'] == 'github_99'
        assert entry['hotkey'] == 'hotkey_1'


class TestHandlePatCheck:
    @patch('gittensor.validator.pat_handler._test_pat_against_repo', return_value=None)
    @patch('gittensor.validator.pat_handler.validate_github_credentials_result', return_value=_validation())
    def test_valid_pat(self, mock_validate, mock_test_query, mock_validator):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_test', 'github_42')

        synapse = _make_check_synapse('hotkey_1')
        result = _run(handle_pat_check(mock_validator, synapse))
        assert result.has_pat is True
        assert result.pat_valid is True
        assert result.rejection_reason is None

    def test_missing_pat(self, mock_validator):
        synapse = _make_check_synapse('hotkey_1')
        result = _run(handle_pat_check(mock_validator, synapse))
        assert result.has_pat is False
        assert result.pat_valid is False

    def test_stale_pat_reports_false(self, mock_validator):
        """If a different miner now holds this UID, has_pat should be False."""
        pat_storage.save_pat(1, 'old_hotkey', 'ghp_old', 'github_42')

        synapse = _make_check_synapse('hotkey_1')
        result = _run(handle_pat_check(mock_validator, synapse))
        assert result.has_pat is False
        assert result.pat_valid is False

    @patch('gittensor.validator.pat_handler._test_pat_against_repo', return_value=None)
    @patch(
        'gittensor.validator.pat_handler.validate_github_credentials_result',
        return_value=_validation(None, 'PAT expired'),
    )
    def test_stored_but_invalid_pat(self, mock_validate, mock_test_query, mock_validator):
        """PAT is stored but fails re-validation."""
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_expired', 'github_42')

        synapse = _make_check_synapse('hotkey_1')
        result = _run(handle_pat_check(mock_validator, synapse))
        assert result.has_pat is True
        assert result.pat_valid is False
        assert 'PAT expired' in (result.rejection_reason or '')

    @patch('gittensor.validator.pat_handler._test_pat_against_repo')
    @patch(
        'gittensor.validator.utils.github_validation.get_github_identity',
        return_value=GitHubIdentityResult(None, GitHubIdentityStatus.TRANSIENT_FAILURE),
    )
    def test_transient_identity_lookup_reports_inconclusive(self, mock_get_identity, mock_test_query, mock_validator):
        """Transient GitHub /user failures must surface as pat_valid=None, not False (issue #1106)."""
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_stored', 'github_42')

        synapse = _make_check_synapse('hotkey_1')
        result = _run(handle_pat_check(mock_validator, synapse))

        assert result.has_pat is True
        assert result.pat_valid is None
        assert result.rejection_reason == (
            'GitHub API temporarily unavailable; please retry the PAT check in a few minutes.'
        )
        mock_test_query.assert_not_called()


# ---------------------------------------------------------------------------
# _test_pat_against_repo tests
# ---------------------------------------------------------------------------


def _mock_post_response(status_code: int = 200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload if payload is not None else {}
    return resp


class TestPatAgainstRepo:
    @patch('gittensor.validator.pat_handler.requests.post')
    def test_valid_viewer_returns_none(self, mock_post):
        mock_post.return_value = _mock_post_response(200, {'data': {'viewer': {'login': 'someone'}}})
        assert _test_pat_against_repo('ghp_valid') is None

    @patch('gittensor.validator.pat_handler.requests.post')
    def test_non_200_returns_status_error(self, mock_post):
        mock_post.return_value = _mock_post_response(401)
        result = _test_pat_against_repo('ghp_bad')
        assert result is not None
        assert '401' in result

    @patch('gittensor.validator.pat_handler.requests.post')
    def test_errors_field_returns_message(self, mock_post):
        mock_post.return_value = _mock_post_response(200, {'errors': [{'message': 'Bad credentials'}]})
        result = _test_pat_against_repo('ghp_bad')
        assert result is not None
        assert 'Bad credentials' in result

    @patch('gittensor.validator.pat_handler.requests.post')
    def test_empty_errors_list_does_not_crash(self, mock_post):
        """Some proxies return {"errors": []}; must not raise IndexError."""
        mock_post.return_value = _mock_post_response(200, {'data': None, 'errors': []})
        # Should not raise, and since viewer is not present, should reject with scope msg
        result = _test_pat_against_repo('ghp_proxy')
        assert result is not None
        assert 'Public Repositories (read-only)' in result

    @patch('gittensor.validator.pat_handler.requests.post')
    def test_viewer_null_rejected_with_scope_message(self, mock_post):
        """Fine-grained PAT without read access returns viewer:null; must be rejected."""
        mock_post.return_value = _mock_post_response(200, {'data': {'viewer': None}})
        result = _test_pat_against_repo('ghp_scopeless')
        assert result is not None
        assert 'Public Repositories (read-only)' in result

    @patch('gittensor.validator.pat_handler.requests.post')
    def test_data_null_rejected_with_scope_message(self, mock_post):
        """Proxy-shaped {"data": null} response must not crash and must be rejected."""
        mock_post.return_value = _mock_post_response(200, {'data': None})
        result = _test_pat_against_repo('ghp_nulldata')
        assert result is not None
        assert 'Public Repositories (read-only)' in result
