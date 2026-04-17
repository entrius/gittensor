#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for DB retry logic in BaseRepository._execute_with_retry."""

from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stub so the module imports without a real DB or bittensor
# ---------------------------------------------------------------------------

@pytest.fixture
def base_repo():
    """Return a BaseRepository instance with a mock DB connection."""
    from gittensor.validator.storage.repository import BaseRepository

    db = MagicMock()
    db.cursor.return_value = MagicMock(__enter__=lambda s, *a: s, __exit__=MagicMock(return_value=False))
    return BaseRepository(db_connection=db)


# ---------------------------------------------------------------------------
# _execute_with_retry
# ---------------------------------------------------------------------------

class TestExecuteWithRetry:

    def test_returns_value_on_first_success(self, base_repo):
        """Operation succeeds immediately — no retries, correct return value."""
        result = base_repo._execute_with_retry(lambda: 42, default=0)
        assert result == 42

    def test_returns_default_on_non_transient_error(self, base_repo):
        """Non-transient Exception is not retried; default is returned immediately."""
        calls = []

        def _op():
            calls.append(1)
            raise ValueError('bad data')

        with patch.object(base_repo.logger, 'error') as mock_error:
            result = base_repo._execute_with_retry(_op, default=-1)

        assert result == -1
        assert len(calls) == 1, 'Should not retry on non-transient error'
        mock_error.assert_called_once()

    @patch('gittensor.validator.storage.repository.time.sleep')
    def test_retries_on_transient_error_then_succeeds(self, mock_sleep, base_repo):
        """Transient error on first two attempts, success on third."""
        import psycopg2

        attempt = {'n': 0}

        def _op():
            attempt['n'] += 1
            if attempt['n'] < 3:
                raise psycopg2.OperationalError('connection reset')
            return 'ok'

        with patch.object(base_repo.logger, 'warning') as mock_warn:
            result = base_repo._execute_with_retry(_op, default='fail')

        assert result == 'ok'
        assert attempt['n'] == 3
        assert mock_sleep.call_count == 2
        assert mock_warn.call_count == 2

    @patch('gittensor.validator.storage.repository.time.sleep')
    def test_exponential_backoff_delays(self, mock_sleep, base_repo):
        """Sleep delays follow the (1, 2, 4) schedule."""
        import psycopg2

        def _op():
            raise psycopg2.OperationalError('connection reset')

        base_repo._execute_with_retry(_op, default=None)

        # 4 attempts total, sleep between the first 3 failures
        mock_sleep.assert_has_calls([call(1), call(2), call(4)])
        assert mock_sleep.call_count == 3

    @patch('gittensor.validator.storage.repository.time.sleep')
    def test_returns_default_after_all_retries_exhausted(self, mock_sleep, base_repo):
        """Default returned after all 4 attempts fail with transient errors."""
        import psycopg2

        calls = []

        def _op():
            calls.append(1)
            raise psycopg2.InterfaceError('conn closed')

        with patch.object(base_repo.logger, 'error') as mock_error:
            result = base_repo._execute_with_retry(_op, default='fallback')

        assert result == 'fallback'
        assert len(calls) == 4, 'Should attempt exactly 4 times (1 + 3 retries)'
        mock_error.assert_called_once()

    @patch('gittensor.validator.storage.repository.time.sleep')
    def test_rollback_called_on_every_transient_failure(self, mock_sleep, base_repo):
        """DB rollback is invoked on each transient failure."""
        import psycopg2

        def _op():
            raise psycopg2.OperationalError('timeout')

        base_repo._execute_with_retry(_op, default=None)

        assert base_repo.db.rollback.call_count == 4

    def test_rollback_called_on_non_transient_failure(self, base_repo):
        """DB rollback is invoked once on a non-transient failure."""
        def _op():
            raise RuntimeError('logic error')

        base_repo._execute_with_retry(_op, default=None)

        assert base_repo.db.rollback.call_count == 1

    @patch('gittensor.validator.storage.repository.time.sleep')
    def test_interface_error_is_also_retried(self, mock_sleep, base_repo):
        """psycopg2.InterfaceError (conn dropped) is treated as transient."""
        import psycopg2

        calls = []

        def _op():
            calls.append(1)
            if len(calls) == 1:
                raise psycopg2.InterfaceError('connection already closed')
            return 'done'

        result = base_repo._execute_with_retry(_op, default='x')
        assert result == 'done'
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# execute_command (delegates to _execute_with_retry)
# ---------------------------------------------------------------------------

class TestExecuteCommand:

    @patch('gittensor.validator.storage.repository.time.sleep')
    def test_retries_on_operational_error(self, mock_sleep, base_repo):
        """execute_command retries when psycopg2.OperationalError is raised."""
        import psycopg2

        cursor_mock = MagicMock()
        cursor_mock.execute.side_effect = [
            psycopg2.OperationalError('server closed'),
            None,  # success on second attempt
        ]
        # get_cursor calls self.db.cursor() and yields the result directly
        base_repo.db.cursor.return_value = cursor_mock

        result = base_repo.execute_command('INSERT INTO t VALUES (%s)', (1,))

        assert result is True
        assert cursor_mock.execute.call_count == 2
        assert mock_sleep.call_count == 1

    def test_returns_false_on_persistent_error(self, base_repo):
        """execute_command returns False when operation keeps raising non-transient errors."""
        base_repo.db.cursor.side_effect = RuntimeError('pool exhausted')

        result = base_repo.execute_command('SELECT 1')

        assert result is False
