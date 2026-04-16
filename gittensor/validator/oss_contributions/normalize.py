from typing import Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.validator.score_normalization import normalize_linear_scores


def normalize_rewards_linear(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """Normalize scores to sum to 1.0, preserving ratios."""

    if not miner_evaluations:
        bt.logging.warning('No miner evaluations provided for normalization')
        return {}

    rewards: Dict[int, float] = {}
    zero_reward_count = 0

    for uid, evaluation in miner_evaluations.items():
        rewards[uid] = evaluation.total_score
        if rewards[uid] > 0:
            bt.logging.info(f'Final reward for uid {uid}: {rewards[uid]:.2f}')
        else:
            zero_reward_count += 1

    if zero_reward_count > 0:
        bt.logging.info(f'{zero_reward_count} miners have 0 reward')

    return normalize_linear_scores(rewards, all_zero_log='All scores are zero, returning original scores')
