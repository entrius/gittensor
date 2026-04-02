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


class TestSavePat:
    def test_save_creates_file(self, use_tmp_pats_file):
        pat_storage.save_pat('hotkey_1', 1, 'ghp_abc', 'github_123')
        assert use_tmp_pats_file.exists()

        entries = json.loads(use_tmp_pats_file.read_text())
        assert len(entries) == 1
        assert entries[0]['hotkey'] == 'hotkey_1'
        assert entries[0]['uid'] == 1
        assert entries[0]['pat'] == 'ghp_abc'
        assert entries[0]['github_id'] == 'github_123'
        assert 'stored_at' in entries[0]

    def test_save_upsert_by_hotkey(self):
        pat_storage.save_pat('hotkey_1', 1, 'ghp_old', 'id_1')
        pat_storage.save_pat('hotkey_1', 1, 'ghp_new', 'id_1')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 1
        assert entries[0]['pat'] == 'ghp_new'

    def test_save_multiple_miners(self):
        pat_storage.save_pat('hotkey_1', 1, 'ghp_a', 'id_1')
        pat_storage.save_pat('hotkey_2', 2, 'ghp_b', 'id_2')
        pat_storage.save_pat('hotkey_3', 3, 'ghp_c', 'id_3')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 3


class TestLoadAllPats:
    def test_load_empty_when_no_file(self):
        entries = pat_storage.load_all_pats()
        assert entries == []

    def test_load_returns_all_entries(self):
        pat_storage.save_pat('h1', 1, 'p1', 'g1')
        pat_storage.save_pat('h2', 2, 'p2', 'g2')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 2

    def test_load_handles_corrupt_file(self, use_tmp_pats_file):
        use_tmp_pats_file.write_text('not json{{{')
        entries = pat_storage.load_all_pats()
        assert entries == []


class TestGetPatByHotkey:
    def test_get_existing(self):
        pat_storage.save_pat('hotkey_1', 1, 'ghp_abc', 'id_1')
        entry = pat_storage.get_pat_by_hotkey('hotkey_1')
        assert entry is not None
        assert entry['pat'] == 'ghp_abc'

    def test_get_missing(self):
        entry = pat_storage.get_pat_by_hotkey('nonexistent')
        assert entry is None


class TestRemovePat:
    def test_remove_existing(self):
        pat_storage.save_pat('hotkey_1', 1, 'ghp_abc', 'id_1')
        assert pat_storage.remove_pat('hotkey_1') is True
        assert pat_storage.get_pat_by_hotkey('hotkey_1') is None

    def test_remove_missing(self):
        assert pat_storage.remove_pat('nonexistent') is False

    def test_remove_preserves_others(self):
        pat_storage.save_pat('h1', 1, 'p1', 'g1')
        pat_storage.save_pat('h2', 2, 'p2', 'g2')
        pat_storage.remove_pat('h1')

        entries = pat_storage.load_all_pats()
        assert len(entries) == 1
        assert entries[0]['hotkey'] == 'h2'


class TestConcurrency:
    def test_concurrent_writes(self):
        """Multiple threads writing simultaneously should not corrupt the file."""
        errors = []

        def write_pat(i):
            try:
                pat_storage.save_pat(f'hotkey_{i}', i, f'ghp_{i}', f'id_{i}')
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
