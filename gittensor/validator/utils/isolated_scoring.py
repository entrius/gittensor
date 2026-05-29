# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Subprocess-isolated wrapper around tree-sitter scoring.

Uses a spawn-context pool (not fork - avoids inheriting bittensor
threads/sockets) with an external wall-clock budget; on
timeout/crash the child is killed and the PR zero-scored.
"""

from __future__ import annotations

import atexit
import multiprocessing
import multiprocessing.pool
import threading
from multiprocessing import get_context
from typing import TYPE_CHECKING, Dict, List, Optional

import bittensor as bt

from gittensor.classes import FileScoreResult, PrScoringResult
from gittensor.constants import SCORING_SUBPROCESS_BUDGET_S
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.validator.utils.load_weights import LanguageConfig, TokenConfig

if TYPE_CHECKING:
    from gittensor.classes import FileChange


_pool: Optional[multiprocessing.pool.Pool] = None
_pool_lock = threading.Lock()


def _worker(
    file_changes: List['FileChange'],
    file_contents: Dict[str, FileContentPair],
    weights: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
) -> PrScoringResult:
    # Lazy import: avoids pulling tree_sitter into importers of this module.
    from gittensor.validator.utils.tree_sitter_scoring import (
        calculate_token_score_from_file_changes,
    )

    return calculate_token_score_from_file_changes(
        file_changes,
        file_contents,
        weights,
        programming_languages,
    )


def _ensure_pool() -> multiprocessing.pool.Pool:
    global _pool
    if _pool is None:
        ctx = get_context('spawn')
        _pool = ctx.Pool(processes=1)
    return _pool


def _reset_pool() -> None:
    global _pool
    if _pool is None:
        return
    try:
        _pool.terminate()
        _pool.join()
    except Exception:
        pass
    _pool = None


def _empty_pr_result(file_changes: List['FileChange']) -> PrScoringResult:
    """Zero-scored result with one ``skipped-isolation-timeout`` entry per file"""
    file_results = [
        FileScoreResult(
            filename=f.short_name,
            score=0.0,
            nodes_scored=0,
            total_lines=f.changes,
            is_test_file=False,
            scoring_method='skipped-isolation-timeout',
        )
        for f in file_changes
    ]
    return PrScoringResult(
        total_score=0.0,
        total_nodes_scored=0,
        total_lines=sum(f.changes for f in file_changes),
        file_results=file_results,
    )


def shutdown() -> None:
    """Tear down the worker pool; idempotent, registered with ``atexit``"""
    with _pool_lock:
        _reset_pool()


atexit.register(shutdown)


def isolated_calculate_token_score(
    file_changes: List['FileChange'],
    file_contents: Dict[str, FileContentPair],
    weights: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
    timeout_s: float = SCORING_SUBPROCESS_BUDGET_S,
) -> PrScoringResult:
    """Score a PR's files in an isolated subprocess with a hard wall-clock.

    On timeout or worker error the pool is reset and a zero-scored result is
    returned.
    """
    with _pool_lock:
        pool = _ensure_pool()
        async_res = pool.apply_async(
            _worker,
            (file_changes, file_contents, weights, programming_languages),
        )

    # Lock not held during get() - a 5s wait would needlessly serialize callers.
    try:
        return async_res.get(timeout=timeout_s)
    except multiprocessing.TimeoutError:
        bt.logging.warning(
            f'Isolated scoring exceeded {timeout_s}s wall budget for {len(file_changes)} files - killing worker'
        )
    except Exception as e:
        bt.logging.warning(f'Isolated scoring worker raised {type(e).__name__}: {e!s} - resetting pool')

    with _pool_lock:
        _reset_pool()
    return _empty_pr_result(file_changes)
