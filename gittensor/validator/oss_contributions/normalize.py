from typing import Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.validator.utils.normalize import normalize_scores


def normalize_rewards_linear(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """Normalize scores to sum to 1.0, preserving ratios."""

    if not miner_evaluations:
        bt.logging.warning('No miner evaluations provided for normalization')
        return {}

    scores: Dict[int, float] = {}
    zero_reward_count = 0

    for uid, evaluation in miner_evaluations.items():
        scores[uid] = evaluation.total_score
        if scores[uid] > 0:
            bt.logging.info(f'Final reward for uid {uid}: {scores[uid]:.2f}')
        else:
            zero_reward_count += 1

    if zero_reward_count > 0:
        bt.logging.info(f'{zero_reward_count} miners have 0 reward')

    if sum(scores.values()) <= 0:
        bt.logging.info('All scores are zero, returning original scores')
        return scores

    return normalize_scores(scores)
