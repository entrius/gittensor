"""Regression tests for validator evaluation storage guards."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

classes = pytest.importorskip('gittensor.classes')
validator_module = pytest.importorskip('neurons.validator')

MinerEvaluation = classes.MinerEvaluation
Validator = validator_module.Validator


class _FakeStorage:
    def __init__(self):
        self.stored = []

    def store_evaluation(self, evaluation, master_repositories):
        self.stored.append(evaluation)
        return SimpleNamespace(success=True, errors=[], stored_counts={})


def _run_bulk_store(storage, miner_evals):
    self_obj = SimpleNamespace(db_storage=storage)
    asyncio.run(Validator.bulk_store_evaluation(self_obj, miner_evals, {'entrius/gittensor': object()}))


def test_transient_identity_failure_without_cache_is_not_stored_as_zero_pr_eval():
    """A transient GitHub /user failure is not authoritative PR state.

    If cache fallback cannot restore a prior PR-bearing evaluation, the zero-PR
    placeholder must not overwrite last-good DB rows.
    """
    storage = _FakeStorage()
    evaluation = MinerEvaluation(uid=29, hotkey='hk', github_id='49853598')
    evaluation.github_pr_fetch_failed = True

    _run_bulk_store(storage, {29: evaluation})

    assert storage.stored == []


def test_successful_zero_pr_evaluation_still_stores():
    """Legitimate zero-PR miners still need DB rows; only fetch failures skip."""
    storage = _FakeStorage()
    evaluation = MinerEvaluation(uid=30, hotkey='hk', github_id='12345')

    _run_bulk_store(storage, {30: evaluation})

    assert storage.stored == [evaluation]
