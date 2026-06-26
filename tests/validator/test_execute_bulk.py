"""Tests for BaseRepository.execute_bulk — the shared executemany helper used by
the store_*_bulk methods."""

from contextlib import contextmanager
from unittest.mock import MagicMock

from gittensor.validator.storage.repository import BaseRepository


def _repo_with_mock_cursor():
    db = MagicMock()
    repo = BaseRepository(db)
    cursor = MagicMock()

    @contextmanager
    def fake_cursor():
        yield cursor

    repo.get_cursor = fake_cursor  # type: ignore[method-assign]
    return repo, cursor, db


def test_success_returns_row_count_and_commits():
    repo, cursor, db = _repo_with_mock_cursor()
    values = [(1,), (2,), (3,)]

    assert repo.execute_bulk('SQL', values, 'pull request') == 3

    cursor.executemany.assert_called_once_with('SQL', values)
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_commit_false_skips_commit():
    repo, cursor, db = _repo_with_mock_cursor()

    assert repo.execute_bulk('SQL', [(1,)], 'issue', commit=False) == 1

    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_failure_rolls_back_and_returns_zero():
    repo, cursor, db = _repo_with_mock_cursor()
    cursor.executemany.side_effect = Exception('boom')

    assert repo.execute_bulk('SQL', [(1,)], 'file change') == 0

    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_error_detail_fn_only_invoked_on_failure():
    repo, cursor, _ = _repo_with_mock_cursor()
    detail = MagicMock(return_value=' | extra')

    repo.execute_bulk('SQL', [(1,)], 'file change', error_detail_fn=detail)
    detail.assert_not_called()

    cursor.executemany.side_effect = Exception('boom')
    repo.execute_bulk('SQL', [(1,)], 'file change', error_detail_fn=detail)
    detail.assert_called_once()
