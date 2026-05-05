# Entrius 2025

"""Tests for validator PAT storage."""

import json
import threading
from datetime import datetime, timedelta

import pytest

from gittensor.constants import REGISTRATION_GRACE_DAYS
from gittensor.validator import pat_storage


@pytest.fixture(autouse=True)
def use_tmp_pats_file(tmp_path, monkeypatch):
    """Redirect PAT storage to a temporary file for each test."""
    tmp_file = tmp_path / 'miner_pats.json'
    monkeypatch.setattr(pat_storage, 'PATS_FILE', tmp_file)
    return tmp_file


class TestEnsurePatsFile:
    def test_creates_file(self, use_tmp_pats_file):
        assert not use_tmp_pats_file.exists()
        pat_storage.ensure_pats_file()
        assert use_tmp_pats_file.exists()
        assert json.loads(use_tmp_pats_file.read_text()) == []

    def test_does_not_overwrite_existing(self, use_tmp_pats_file):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc', 'user_1')
        pat_storage.ensure_pats_file()
        entries = json.loads(use_tmp_pats_file.read_text())
        assert len(entries) == 1


class TestSavePat:
    def test_save_creates_file(self, use_tmp_pats_file):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc', 'user_1')
        assert use_tmp_pats_file.exists()

        entries = json.loads(use_tmp_pats_file.read_text())
        assert len(entries) == 1
        assert entries[0]['uid'] == 1
        assert entries[0]['hotkey'] == 'hotkey_1'
        assert entries[0]['pat'] == 'ghp_abc'
        assert entries[0]['github_id'] == 'user_1'
        assert 'stored_at' in entries[0]

    def test_save_upsert_by_uid(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_old', 'user_1')
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_new', 'user_1')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 1
        assert entries[0]['pat'] == 'ghp_new'

    def test_save_upsert_replaces_hotkey_on_uid(self):
        """When a new miner takes over a UID, save_pat overwrites the old entry."""
        pat_storage.save_pat(1, 'old_hotkey', 'ghp_old', 'user_old')
        pat_storage.save_pat(1, 'new_hotkey', 'ghp_new', 'user_new')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 1
        assert entries[0]['hotkey'] == 'new_hotkey'
        assert entries[0]['pat'] == 'ghp_new'

    def test_save_multiple_miners(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_a', 'user_a')
        pat_storage.save_pat(2, 'hotkey_2', 'ghp_b', 'user_b')
        pat_storage.save_pat(3, 'hotkey_3', 'ghp_c', 'user_c')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 3


class TestLoadAllPats:
    def test_load_empty_when_no_file(self):
        entries = pat_storage.load_all_pats()
        assert entries == []

    def test_load_returns_all_entries(self):
        pat_storage.save_pat(1, 'h1', 'p1', 'user_1')
        pat_storage.save_pat(2, 'h2', 'p2', 'user_2')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 2

    def test_load_handles_corrupt_file(self, use_tmp_pats_file):
        use_tmp_pats_file.write_text('not json{{{')
        entries = pat_storage.load_all_pats()
        assert entries == []


class TestGetPatByUid:
    def test_get_existing(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc', 'user_1')
        entry = pat_storage.get_pat_by_uid(1)
        assert entry is not None
        assert entry['pat'] == 'ghp_abc'

    def test_get_missing(self):
        entry = pat_storage.get_pat_by_uid(999)
        assert entry is None


class TestRemovePat:
    def test_remove_existing(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc', 'user_1')
        assert pat_storage.remove_pat(1) is True
        assert pat_storage.get_pat_by_uid(1) is None

    def test_remove_missing(self):
        assert pat_storage.remove_pat(999) is False

    def test_remove_preserves_others(self):
        pat_storage.save_pat(1, 'h1', 'p1', 'user_1')
        pat_storage.save_pat(2, 'h2', 'p2', 'user_2')
        pat_storage.remove_pat(1)

        entries = pat_storage.load_all_pats()
        assert len(entries) == 1
        assert entries[0]['uid'] == 2


class TestConcurrency:
    def test_concurrent_writes(self):
        """Multiple threads writing simultaneously should not corrupt the file."""
        errors = []

        def write_pat(i):
            try:
                pat_storage.save_pat(i, f'hotkey_{i}', f'ghp_{i}', f'user_{i}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_pat, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        entries = pat_storage.load_all_pats()
        assert len(entries) == 20


class TestFirstRegisteredAt:
    def test_new_entry_stamps_first_registered_at(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp', 'user_1')
        entry = pat_storage.get_pat_by_uid(1)
        assert entry is not None
        assert 'first_registered_at' in entry
        assert entry['first_registered_at'] == entry['stored_at']

    def test_re_broadcast_preserves_first_registered_at(self, use_tmp_pats_file):
        initial = [
            {
                'uid': 1,
                'hotkey': 'hotkey_1',
                'pat': 'ghp_old',
                'github_id': 'user_1',
                'stored_at': '2026-01-01T00:00:00+00:00',
                'first_registered_at': '2026-01-01T00:00:00+00:00',
            }
        ]
        use_tmp_pats_file.write_text(json.dumps(initial))

        pat_storage.save_pat(1, 'hotkey_1', 'ghp_new', 'user_1')

        entry = pat_storage.get_pat_by_uid(1)
        assert entry['first_registered_at'] == '2026-01-01T00:00:00+00:00'
        assert entry['pat'] == 'ghp_new'
        assert entry['stored_at'] != '2026-01-01T00:00:00+00:00'

    def test_legacy_entry_grandfathered_on_re_broadcast(self, use_tmp_pats_file):
        legacy = [
            {
                'uid': 1,
                'hotkey': 'hotkey_1',
                'pat': 'ghp_old',
                'github_id': 'user_1',
                'stored_at': '2020-01-01T00:00:00+00:00',
            }
        ]
        use_tmp_pats_file.write_text(json.dumps(legacy))

        pat_storage.save_pat(1, 'hotkey_1', 'ghp_new', 'user_1')

        entry = pat_storage.get_pat_by_uid(1)
        assert 'first_registered_at' not in entry
        assert entry['pat'] == 'ghp_new'

    def test_hotkey_change_resets_first_registered_at(self, use_tmp_pats_file):
        initial = [
            {
                'uid': 1,
                'hotkey': 'old_hotkey',
                'pat': 'ghp_old',
                'github_id': 'user_old',
                'stored_at': '2020-01-01T00:00:00+00:00',
                'first_registered_at': '2020-01-01T00:00:00+00:00',
            }
        ]
        use_tmp_pats_file.write_text(json.dumps(initial))

        pat_storage.save_pat(1, 'new_hotkey', 'ghp_new', 'user_new')

        entry = pat_storage.get_pat_by_uid(1)
        assert entry['first_registered_at'] != '2020-01-01T00:00:00+00:00'
        assert entry['first_registered_at'] == entry['stored_at']


class TestGetRegistrationCutoff:
    def test_no_entry(self):
        assert pat_storage.get_registration_cutoff(1, 'hotkey_1') is None

    def test_hotkey_mismatch(self):
        pat_storage.save_pat(1, 'old_hotkey', 'ghp', 'user_1')
        assert pat_storage.get_registration_cutoff(1, 'different_hotkey') is None

    def test_legacy_entry_no_field(self, use_tmp_pats_file):
        legacy = [
            {
                'uid': 1,
                'hotkey': 'hotkey_1',
                'pat': 'ghp',
                'github_id': 'user_1',
                'stored_at': '2020-01-01T00:00:00+00:00',
            }
        ]
        use_tmp_pats_file.write_text(json.dumps(legacy))
        assert pat_storage.get_registration_cutoff(1, 'hotkey_1') is None

    def test_returns_first_registered_minus_grace(self, use_tmp_pats_file):
        first_reg = '2026-04-01T00:00:00+00:00'
        entry = [
            {
                'uid': 1,
                'hotkey': 'hotkey_1',
                'pat': 'ghp',
                'github_id': 'user_1',
                'stored_at': first_reg,
                'first_registered_at': first_reg,
            }
        ]
        use_tmp_pats_file.write_text(json.dumps(entry))

        cutoff = pat_storage.get_registration_cutoff(1, 'hotkey_1')

        assert cutoff is not None
        expected = datetime.fromisoformat(first_reg) - timedelta(days=REGISTRATION_GRACE_DAYS)
        assert cutoff == expected

    def test_malformed_iso_returns_none(self, use_tmp_pats_file):
        bad = [
            {
                'uid': 1,
                'hotkey': 'hotkey_1',
                'pat': 'ghp',
                'github_id': 'user_1',
                'first_registered_at': 'not-a-date',
            }
        ]
        use_tmp_pats_file.write_text(json.dumps(bad))
        assert pat_storage.get_registration_cutoff(1, 'hotkey_1') is None
