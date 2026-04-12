# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for stale PR state cleanup (Fixes #398).

Scenario 1: PR records from repos removed from master_repositories should be deleted.
Scenario 2: PRs skipped during evaluation (e.g., merged to non-acceptable branch)
             should have their pr_state updated to match GitHub.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from gittensor.classes import MinerEvaluation, PRState, PullRequest
from gittensor.validator.storage.queries import CLEANUP_STALE_PULL_REQUESTS, UPDATE_SKIPPED_PR_STATE


# ============================================================================
# SQL Query Tests (using SQLite for portability)
# ============================================================================


@pytest.fixture
def sqlite_db():
    """Create an in-memory SQLite database with the pull_requests schema."""
    conn = sqlite3.connect(':memory:')
    conn.execute('''
        CREATE TABLE pull_requests (
            number INTEGER,
            repository_full_name TEXT,
            uid INTEGER,
            hotkey TEXT,
            github_id TEXT,
            pr_state TEXT,
            updated_at TEXT,
            UNIQUE(number, repository_full_name)
        )
    ''')
    conn.commit()
    return conn


def seed_pull_requests(conn, rows):
    """Insert test PR rows: list of (number, repo, uid, hotkey, pr_state)."""
    for number, repo, uid, hotkey, state in rows:
        conn.execute(
            'INSERT INTO pull_requests (number, repository_full_name, uid, hotkey, github_id, pr_state) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (number, repo, uid, hotkey, 'gh-123', state),
        )
    conn.commit()


def get_all_prs(conn):
    """Return all PRs as list of (number, repo, pr_state) tuples."""
    return conn.execute(
        'SELECT number, repository_full_name, pr_state FROM pull_requests ORDER BY number'
    ).fetchall()


class TestScenario1_RepoRemovedFromTracking:
    """When a repo is removed from master_repositories.json, its PR records should be deleted."""

    def test_deletes_prs_from_removed_repo(self, sqlite_db):
        seed_pull_requests(sqlite_db, [
            (1, 'tracked/repo-a', 1, 'hk1', 'OPEN'),
            (2, 'tracked/repo-b', 1, 'hk1', 'MERGED'),
            (3, 'removed/repo-c', 1, 'hk1', 'OPEN'),      # should be deleted
            (4, 'removed/repo-d', 1, 'hk1', 'MERGED'),     # should be deleted
        ])

        active_repos = ('tracked/repo-a', 'tracked/repo-b')
        placeholders = ','.join(['?'] * len(active_repos))
        sqlite_db.execute(
            f'DELETE FROM pull_requests WHERE uid = ? AND hotkey = ? '
            f'AND repository_full_name NOT IN ({placeholders})',
            (1, 'hk1') + active_repos,
        )
        sqlite_db.commit()

        result = get_all_prs(sqlite_db)
        assert len(result) == 2
        assert result[0] == (1, 'tracked/repo-a', 'OPEN')
        assert result[1] == (2, 'tracked/repo-b', 'MERGED')

    def test_does_not_delete_other_miners_prs(self, sqlite_db):
        seed_pull_requests(sqlite_db, [
            (1, 'removed/repo', 1, 'hk1', 'OPEN'),    # miner 1 — should be deleted
            (2, 'removed/repo', 2, 'hk2', 'OPEN'),    # miner 2 — should NOT be deleted
        ])

        active_repos = ('tracked/repo',)
        placeholders = ','.join(['?'] * len(active_repos))
        sqlite_db.execute(
            f'DELETE FROM pull_requests WHERE uid = ? AND hotkey = ? '
            f'AND repository_full_name NOT IN ({placeholders})',
            (1, 'hk1') + active_repos,
        )
        sqlite_db.commit()

        result = get_all_prs(sqlite_db)
        assert len(result) == 1
        assert result[0] == (2, 'removed/repo', 'OPEN')

    def test_no_deletion_when_all_repos_tracked(self, sqlite_db):
        seed_pull_requests(sqlite_db, [
            (1, 'tracked/repo-a', 1, 'hk1', 'OPEN'),
            (2, 'tracked/repo-b', 1, 'hk1', 'MERGED'),
        ])

        active_repos = ('tracked/repo-a', 'tracked/repo-b')
        placeholders = ','.join(['?'] * len(active_repos))
        sqlite_db.execute(
            f'DELETE FROM pull_requests WHERE uid = ? AND hotkey = ? '
            f'AND repository_full_name NOT IN ({placeholders})',
            (1, 'hk1') + active_repos,
        )
        sqlite_db.commit()

        result = get_all_prs(sqlite_db)
        assert len(result) == 2


class TestScenario2_MergedToNonAcceptableBranch:
    """When a PR is merged to a non-acceptable branch, pr_state should update in DB."""

    def test_updates_open_to_merged(self, sqlite_db):
        seed_pull_requests(sqlite_db, [
            (1, 'bitcoin/bitcoin', 1, 'hk1', 'OPEN'),  # stale — actually MERGED on GitHub
        ])

        sqlite_db.execute(
            'UPDATE pull_requests SET pr_state = ?, updated_at = datetime("now") '
            'WHERE number = ? AND repository_full_name = ? AND pr_state != ?',
            ('MERGED', 1, 'bitcoin/bitcoin', 'MERGED'),
        )
        sqlite_db.commit()

        result = get_all_prs(sqlite_db)
        assert result[0] == (1, 'bitcoin/bitcoin', 'MERGED')

    def test_no_update_when_state_already_correct(self, sqlite_db):
        seed_pull_requests(sqlite_db, [
            (1, 'bitcoin/bitcoin', 1, 'hk1', 'MERGED'),  # already correct
        ])

        cursor = sqlite_db.execute(
            'UPDATE pull_requests SET pr_state = ?, updated_at = datetime("now") '
            'WHERE number = ? AND repository_full_name = ? AND pr_state != ?',
            ('MERGED', 1, 'bitcoin/bitcoin', 'MERGED'),
        )
        sqlite_db.commit()

        assert cursor.rowcount == 0  # no rows updated

    def test_updates_multiple_skipped_prs(self, sqlite_db):
        seed_pull_requests(sqlite_db, [
            (1, 'bitcoin/bitcoin', 1, 'hk1', 'OPEN'),
            (2, 'entrius/gittensor', 1, 'hk1', 'OPEN'),
            (3, 'entrius/gittensor', 1, 'hk1', 'MERGED'),  # already correct
        ])

        state_updates = [
            (1, 'bitcoin/bitcoin', 'MERGED'),
            (2, 'entrius/gittensor', 'MERGED'),
            (3, 'entrius/gittensor', 'MERGED'),  # no-op
        ]
        for pr_number, repo, state in state_updates:
            sqlite_db.execute(
                'UPDATE pull_requests SET pr_state = ?, updated_at = datetime("now") '
                'WHERE number = ? AND repository_full_name = ? AND pr_state != ?',
                (state, pr_number, repo, state),
            )
        sqlite_db.commit()

        result = get_all_prs(sqlite_db)
        assert result[0] == (1, 'bitcoin/bitcoin', 'MERGED')
        assert result[1] == (2, 'entrius/gittensor', 'MERGED')
        assert result[2] == (3, 'entrius/gittensor', 'MERGED')


# ============================================================================
# Integration Tests (MinerEvaluation + skipped_pr_state_updates)
# ============================================================================


class TestSkippedPrStateTracking:
    """Test that skipped PRs are correctly tracked on MinerEvaluation."""

    def test_skipped_pr_state_updates_initialized_empty(self):
        miner_eval = MinerEvaluation(uid=1, hotkey='hk1')
        assert miner_eval.skipped_pr_state_updates == []

    def test_append_skipped_pr(self):
        miner_eval = MinerEvaluation(uid=1, hotkey='hk1')
        miner_eval.skipped_pr_state_updates.append((42, 'bitcoin/bitcoin', 'MERGED'))
        assert len(miner_eval.skipped_pr_state_updates) == 1
        assert miner_eval.skipped_pr_state_updates[0] == (42, 'bitcoin/bitcoin', 'MERGED')

    def test_multiple_skipped_prs(self):
        miner_eval = MinerEvaluation(uid=1, hotkey='hk1')
        miner_eval.skipped_pr_state_updates.append((1, 'repo/a', 'MERGED'))
        miner_eval.skipped_pr_state_updates.append((2, 'repo/b', 'MERGED'))
        miner_eval.skipped_pr_state_updates.append((3, 'repo/c', 'CLOSED'))
        assert len(miner_eval.skipped_pr_state_updates) == 3


# ============================================================================
# Repository Method Tests (mocked DB)
# ============================================================================


class TestRepositoryCleanupMethods:
    """Test Repository.cleanup_stale_pull_requests and update_skipped_pr_states."""

    def test_cleanup_stale_pull_requests_builds_correct_query(self):
        from gittensor.validator.storage.repository import Repository

        mock_db = MagicMock()
        repo = Repository(mock_db)

        active_repos = ('bitcoin/bitcoin', 'entrius/gittensor', 'opentensor/subtensor')

        with patch.object(repo, 'execute_command', return_value=True) as mock_exec:
            result = repo.cleanup_stale_pull_requests(uid=1, hotkey='hk1', active_repos=active_repos)

        assert result is True
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        query = call_args[0][0]
        params = call_args[0][1]

        # Query should have 3 placeholders for repos + 2 for uid/hotkey
        assert 'NOT IN (%s,%s,%s)' in query
        assert params == (1, 'hk1', 'bitcoin/bitcoin', 'entrius/gittensor', 'opentensor/subtensor')

    def test_cleanup_stale_pull_requests_empty_repos_returns_false(self):
        from gittensor.validator.storage.repository import Repository

        mock_db = MagicMock()
        repo = Repository(mock_db)

        result = repo.cleanup_stale_pull_requests(uid=1, hotkey='hk1', active_repos=())
        assert result is False

    def test_update_skipped_pr_states_calls_execute_per_pr(self):
        from gittensor.validator.storage.repository import Repository

        mock_db = MagicMock()
        repo = Repository(mock_db)

        state_updates = [
            (1, 'bitcoin/bitcoin', 'MERGED'),
            (2, 'entrius/gittensor', 'MERGED'),
        ]

        with patch.object(repo, 'execute_command', return_value=True) as mock_exec:
            count = repo.update_skipped_pr_states(state_updates)

        assert count == 2
        assert mock_exec.call_count == 2

        # Verify params for first call: (state, number, repo, state)
        first_call_params = mock_exec.call_args_list[0][0][1]
        assert first_call_params == ('MERGED', 1, 'bitcoin/bitcoin', 'MERGED')

    def test_update_skipped_pr_states_empty_list(self):
        from gittensor.validator.storage.repository import Repository

        mock_db = MagicMock()
        repo = Repository(mock_db)

        with patch.object(repo, 'execute_command') as mock_exec:
            count = repo.update_skipped_pr_states([])

        assert count == 0
        mock_exec.assert_not_called()
