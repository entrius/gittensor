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
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc')
        pat_storage.ensure_pats_file()
        entries = json.loads(use_tmp_pats_file.read_text())
        assert len(entries) == 1


class TestSavePat:
    def test_save_creates_file(self, use_tmp_pats_file):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc')
        assert use_tmp_pats_file.exists()

        entries = json.loads(use_tmp_pats_file.read_text())
        assert len(entries) == 1
        assert entries[0]['uid'] == 1
        assert entries[0]['hotkey'] == 'hotkey_1'
        assert entries[0]['pat'] == 'ghp_abc'
        assert 'stored_at' in entries[0]
        assert 'github_id' not in entries[0]

    def test_save_upsert_by_uid(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_old')
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_new')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 1
        assert entries[0]['pat'] == 'ghp_new'

    def test_save_upsert_replaces_hotkey_on_uid(self):
        """When a new miner takes over a UID, save_pat overwrites the old entry."""
        pat_storage.save_pat(1, 'old_hotkey', 'ghp_old')
        pat_storage.save_pat(1, 'new_hotkey', 'ghp_new')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 1
        assert entries[0]['hotkey'] == 'new_hotkey'
        assert entries[0]['pat'] == 'ghp_new'

    def test_save_multiple_miners(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_a')
        pat_storage.save_pat(2, 'hotkey_2', 'ghp_b')
        pat_storage.save_pat(3, 'hotkey_3', 'ghp_c')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 3


class TestLoadAllPats:
    def test_load_empty_when_no_file(self):
        entries = pat_storage.load_all_pats()
        assert entries == []

    def test_load_returns_all_entries(self):
        pat_storage.save_pat(1, 'h1', 'p1')
        pat_storage.save_pat(2, 'h2', 'p2')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 2

    def test_load_handles_corrupt_file(self, use_tmp_pats_file):
        use_tmp_pats_file.write_text('not json{{{')
        entries = pat_storage.load_all_pats()
        assert entries == []


class TestGetPatByUid:
    def test_get_existing(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc')
        entry = pat_storage.get_pat_by_uid(1)
        assert entry is not None
        assert entry['pat'] == 'ghp_abc'

    def test_get_missing(self):
        entry = pat_storage.get_pat_by_uid(999)
        assert entry is None


class TestRemovePat:
    def test_remove_existing(self):
        pat_storage.save_pat(1, 'hotkey_1', 'ghp_abc')
        assert pat_storage.remove_pat(1) is True
        assert pat_storage.get_pat_by_uid(1) is None

    def test_remove_missing(self):
        assert pat_storage.remove_pat(999) is False

    def test_remove_preserves_others(self):
        pat_storage.save_pat(1, 'h1', 'p1')
        pat_storage.save_pat(2, 'h2', 'p2')
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
                pat_storage.save_pat(i, f'hotkey_{i}', f'ghp_{i}')
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
