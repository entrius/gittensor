# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for ``isolated_calculate_token_score`` timeout and exception paths"""

from __future__ import annotations

import multiprocessing

import pytest

from gittensor.classes import FileChange, PrScoringResult
from gittensor.validator.utils import isolated_scoring
from gittensor.validator.utils.load_weights import TokenConfig


def _change(name: str = 'a.py', changes: int = 10) -> FileChange:
    return FileChange(
        pr_number=1,
        repository_full_name='x/y',
        filename=name,
        changes=changes,
        additions=changes,
        deletions=0,
        status='added',
        file_extension='py',
    )


class _FakeAsyncResult:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get(self, timeout: float | None = None):
        raise self._exc


class _FakePool:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.terminated = False
        self.joined = False

    def apply_async(self, fn, args):
        return _FakeAsyncResult(self._exc)

    def terminate(self) -> None:
        self.terminated = True

    def join(self) -> None:
        self.joined = True


@pytest.fixture(autouse=True)
def _reset_module_pool():
    isolated_scoring._pool = None
    yield
    isolated_scoring._pool = None


def _call_with_fake(fake: _FakePool, changes: list[FileChange], timeout_s: float = 0.01) -> PrScoringResult:
    isolated_scoring._pool = fake
    return isolated_scoring.isolated_calculate_token_score(
        changes,
        {},
        weights=TokenConfig(),
        programming_languages={},
        timeout_s=timeout_s,
    )


def test_worker_timeout_yields_empty_result_and_resets_pool():
    fake = _FakePool(multiprocessing.TimeoutError())
    changes = [_change('a.py', 10), _change('b.py', 5)]

    result = _call_with_fake(fake, changes)

    assert isinstance(result, PrScoringResult)
    assert result.total_score == 0.0
    assert result.total_nodes_scored == 0
    assert result.total_lines == 15
    assert [f.filename for f in result.file_results] == ['a.py', 'b.py']
    assert all(f.scoring_method == 'skipped-isolation-timeout' for f in result.file_results)
    assert all(f.score == 0.0 and f.nodes_scored == 0 for f in result.file_results)
    assert fake.terminated and fake.joined
    assert isolated_scoring._pool is None


def test_worker_exception_yields_empty_result_and_resets_pool():
    fake = _FakePool(RuntimeError('boom'))
    changes = [_change('a.py', 7)]

    result = _call_with_fake(fake, changes, timeout_s=1.0)

    assert isinstance(result, PrScoringResult)
    assert result.total_score == 0.0
    assert len(result.file_results) == 1
    assert result.file_results[0].scoring_method == 'skipped-isolation-timeout'
    assert result.file_results[0].total_lines == 7
    assert fake.terminated and fake.joined
    assert isolated_scoring._pool is None
