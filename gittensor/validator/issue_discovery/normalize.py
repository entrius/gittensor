# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.validator.score_normalization import normalize_linear_scores


def normalize_issue_discovery_rewards(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """Normalize issue discovery scores to sum to 1.0, preserving ratios."""

    if not miner_evaluations:
        return {}

    rewards: Dict[int, float] = {}
    nonzero_count = 0

    for uid, evaluation in miner_evaluations.items():
        rewards[uid] = evaluation.issue_discovery_score
        if rewards[uid] > 0:
            nonzero_count += 1

    total = sum(rewards.values())
    out = normalize_linear_scores(
        rewards,
        all_zero_log='Issue discovery: all scores are zero, returning empty rewards',
    )
    if total > 0:
        bt.logging.info(f'Issue discovery: normalized {nonzero_count} miners with scores > 0')
    return out
