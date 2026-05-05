"""Per-miner exception isolation in the OSS scoring loop.

Regression for the failure mode where a single miner's uncaught exception in
``evaluate_miners_pull_requests`` aborted the whole scoring round, causing the
validator's daemon scoring thread to die at ``base/validator.py``'s
``except Exception`` and leaving the main thread to log "Validator running"
indefinitely with no further forward passes.

The fix wraps the per-miner call in ``try/except Exception`` and substitutes a
``MinerEvaluation(failed_reason=...)`` so the loop continues. This matches the
existing isolation pattern in ``issue_discovery/mirror_scan.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Dict, Set
from unittest.mock import AsyncMock

import numpy as np
import pytest

reward_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.reward',
    reason='Requires gittensor validator subpackage',
)
classes_module = pytest.importorskip('gittensor.classes', reason='Requires gittensor classes')

MinerEvaluation = classes_module.MinerEvaluation


class _StubValidator:
    """Minimal stand-in for the parts of Validator that get_rewards reads."""

    def __init__(self, hotkeys: Dict[int, str]):
        self.metagraph = SimpleNamespace(hotkeys=hotkeys)

    def store_or_use_cached_evaluation(self, miner_evaluations: Dict[int, MinerEvaluation]) -> Set[int]:
        return set()


def test_per_miner_exception_does_not_abort_round(monkeypatch):
    """A single miner raising in evaluate_miners_pull_requests must not stop
    the loop; every other UID must still be evaluated and the round must
    return normally."""

    uids = {1, 2, 3, 4, 5}
    boom_uid = 3
    hotkeys = {uid: f'hot{uid}' for uid in uids}
    validator = _StubValidator(hotkeys=hotkeys)

    async def fake_evaluate(uid, hotkey, *args, **kwargs):
        if uid == boom_uid:
            raise RecursionError('simulated tree-sitter blow-up')
        return MinerEvaluation(uid=uid, hotkey=hotkey)

    monkeypatch.setattr(reward_module, 'evaluate_miners_pull_requests', fake_evaluate)
    monkeypatch.setattr(reward_module.pat_storage, 'load_all_pats', lambda: [])
    monkeypatch.setattr(reward_module, 'detect_and_penalize_miners_sharing_github', lambda evals: set())
    monkeypatch.setattr(reward_module, 'finalize_miner_scores', lambda evals: None)
    monkeypatch.setattr(
        reward_module,
        'normalize_rewards_linear',
        lambda evals: {uid: 0.0 for uid in evals},
    )

    rewards, evaluations, cached_uids, penalized_uids = asyncio.run(
        reward_module.get_rewards(
            validator,
            uids,
            master_repositories={},
            programming_languages={},
            token_config=SimpleNamespace(),
        )
    )

    assert set(evaluations.keys()) == uids, 'every UID must be present after the round'
    crashed = evaluations[boom_uid]
    assert crashed.failed_reason is not None, 'crashed miner must carry a failed_reason'
    assert 'RecursionError' in crashed.failed_reason
    for uid in uids - {boom_uid}:
        assert evaluations[uid].failed_reason is None, f'UID {uid} should not be marked failed'
    assert isinstance(rewards, np.ndarray)
    assert rewards.shape == (len(uids),)
    assert cached_uids == set()
    assert penalized_uids == set()


def test_failed_reason_records_exception_class_and_message(monkeypatch):
    """The substituted MinerEvaluation must capture both the exception class
    name and its message so post-hoc DB inspection can identify the vector."""

    hotkeys = {7: 'hot7'}
    validator = _StubValidator(hotkeys=hotkeys)

    raise_call = AsyncMock(side_effect=ValueError('malformed mirror json'))
    monkeypatch.setattr(reward_module, 'evaluate_miners_pull_requests', raise_call)
    monkeypatch.setattr(reward_module.pat_storage, 'load_all_pats', lambda: [])
    monkeypatch.setattr(reward_module, 'detect_and_penalize_miners_sharing_github', lambda evals: set())
    monkeypatch.setattr(reward_module, 'finalize_miner_scores', lambda evals: None)
    monkeypatch.setattr(reward_module, 'normalize_rewards_linear', lambda evals: {7: 0.0})

    _, evaluations, _, _ = asyncio.run(
        reward_module.get_rewards(
            validator,
            {7},
            master_repositories={},
            programming_languages={},
            token_config=SimpleNamespace(),
        )
    )

    failed = evaluations[7]
    assert failed.uid == 7
    assert failed.hotkey == 'hot7'
    assert 'ValueError' in failed.failed_reason
    assert 'malformed mirror json' in failed.failed_reason
