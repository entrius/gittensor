# Entrius 2025

"""Tests for PAT broadcast and check handlers."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from bittensor.core.synapse import TerminalInfo

from gittensor.synapses import PatBroadcastSynapse, PatCheckSynapse
from gittensor.validator import pat_storage
from gittensor.validator.pat_handler import (
    blacklist_pat_broadcast,
    blacklist_pat_check,
    handle_pat_broadcast,
    handle_pat_check,
)


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


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
    @patch('gittensor.validator.pat_handler.validate_github_credentials', return_value=('github_42', None))
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
    @patch('gittensor.validator.pat_handler.validate_github_credentials', return_value=(None, 'PAT expired'))
    def test_stored_but_invalid_pat(self, mock_validate, mock_test_query, mock_validator):
        """PAT is stored but fails re-validation."""
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_expired', 'github_42')

        synapse = _make_check_synapse('hotkey_1')
        result = _run(handle_pat_check(mock_validator, synapse))
        assert result.has_pat is True
        assert result.pat_valid is False
        assert 'PAT expired' in (result.rejection_reason or '')

    def test_unregistered_hotkey_does_not_raise(self, mock_validator):
        """Regression: hotkey deregistered between blacklist and handler must not crash.

        Simulates the race where blacklist_pat_check passes but the metagraph resyncs
        before handle_pat_check runs. Previously raised ValueError on .index().
        """
        mock_validator.metagraph.hotkeys = []

        synapse = _make_check_synapse('hotkey_1')
        result = _run(handle_pat_check(mock_validator, synapse))

        assert result.has_pat is False
        assert result.pat_valid is False
        assert 'not registered' in (result.rejection_reason or '').lower()
