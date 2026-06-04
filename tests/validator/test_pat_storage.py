# Entrius 2025

"""Tests for validator PAT storage."""

import json
import threading

import pytest

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

    def test_save_releases_uid_but_retains_displaced_pin(self):
        """When a new hotkey takes over a UID, the new hotkey becomes the slot's
        occupant, but the displaced hotkey's record (and its identity pin) is
        retained with its UID released to None."""
        pat_storage.save_pat(1, 'old_hotkey', 'ghp_old', 'user_old')
        pat_storage.save_pat(1, 'new_hotkey', 'ghp_new', 'user_new')

        # The UID slot now belongs to new_hotkey.
        occupant = pat_storage.get_pat_by_uid(1)
        assert occupant is not None
        assert occupant['hotkey'] == 'new_hotkey'
        assert occupant['pat'] == 'ghp_new'

        # The displaced hotkey's identity pin survives (UID released to None).
        displaced = pat_storage.get_pat_by_hotkey('old_hotkey')
        assert displaced is not None
        assert displaced['github_id'] == 'user_old'
        assert displaced['uid'] is None

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


class TestGetPatByHotkey:
    def test_get_existing(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc', 'user_1')
        entry = pat_storage.get_pat_by_hotkey('hotkey_1')
        assert entry is not None
        assert entry['github_id'] == 'user_1'

    def test_get_missing(self):
        assert pat_storage.get_pat_by_hotkey('never_seen') is None

    def test_pin_follows_hotkey_across_uid_reuse_and_reregistration(self):
        """A hotkey's pin must be findable even after its old UID slot is taken
        over and it later re-registers on a different UID."""
        pat_storage.save_pat(1, 'victim', 'ghp_v', 'github_42')
        # UID 1 reused by a throwaway hotkey.
        pat_storage.save_pat(1, 'throwaway', 'ghp_t', 'github_77')
        # victim re-registers on a fresh UID.
        pat_storage.save_pat(5, 'victim', 'ghp_v2', 'github_42')

        assert pat_storage.get_pat_by_uid(1)['hotkey'] == 'throwaway'
        pin = pat_storage.get_pat_by_hotkey('victim')
        assert pin is not None
        assert pin['github_id'] == 'github_42'
        assert pin['uid'] == 5


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
