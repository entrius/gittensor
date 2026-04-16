# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared linear normalization of per-miner scores to a distribution that sums to 1.0."""

from typing import Callable, Dict, Optional

import bittensor as bt

from gittensor.classes import MinerEvaluation


def normalize_linear_scores(
    rewards: Dict[int, float],
    *,
    all_zero_log: Optional[str] = None,
) -> Dict[int, float]:
    """Divide scores by their sum; if total <= 0, return ``rewards`` unchanged."""
    if not rewards:
        return {}
    total = sum(rewards.values())
    if total <= 0:
        if all_zero_log:
            bt.logging.info(all_zero_log)
        return rewards
    return {uid: score / total for uid, score in rewards.items()}


def normalize_miner_evaluations_linear(
    miner_evaluations: Dict[int, MinerEvaluation],
    score_fn: Callable[[MinerEvaluation], float],
    *,
    empty_warning: Optional[str] = None,
    log_each_positive_uid: bool = False,
    count_zero_log_label: Optional[str] = None,
    all_zero_log: Optional[str] = None,
) -> Dict[int, float]:
    """
    Map evaluations through ``score_fn``, then apply :func:`normalize_linear_scores`.

    Used for OSS contribution rewards (``total_score``) and issue discovery (``issue_discovery_score``).
    """
    if not miner_evaluations:
        if empty_warning is not None:
            bt.logging.warning(empty_warning)
        return {}

    rewards: Dict[int, float] = {}
    zeroish = 0
    for uid, evaluation in miner_evaluations.items():
        rewards[uid] = score_fn(evaluation)
        if rewards[uid] > 0:
            if log_each_positive_uid:
                bt.logging.info(f'Final reward for uid {uid}: {rewards[uid]:.2f}')
        else:
            zeroish += 1

    if count_zero_log_label is not None and zeroish > 0:
        bt.logging.info(f'{zeroish} miners have {count_zero_log_label}')

    return normalize_linear_scores(rewards, all_zero_log=all_zero_log)
