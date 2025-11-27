from typing import Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    PARETO_DISTRIBUTION_ALPHA_VALUE,
)


def normalize_rewards_with_pareto(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """
    Pareto-scale scores (score**(1/alpha)) and normalize to sum to 1.
    alpha < 1 amplifies differences; alpha > 1 compresses; alpha = 1 is linear.

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Dict of uid -> MinerEvaluation

    Returns:
        Dict[int, float]: Pareto-curved scores that sum to 1.0, Dict of uid ->  score.
    """
    if not miner_evaluations:
        bt.logging.warning("No miner evaluations provided for Pareto normalization")
        return {}

    rewards: Dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        evaluation.calculate_total_score_and_total_contributions()
        rewards[uid] = evaluation.total_score
        bt.logging.info(f"Final reward for uid {uid}: {rewards[uid]:.2f}")

    if all(score <= 0 for score in rewards.values()):
        bt.logging.info("All scores are zero, skipping Pareto transformation")
        return rewards

    alpha = PARETO_DISTRIBUTION_ALPHA_VALUE
    bt.logging.info(f"Applying Pareto transformation with α={alpha}")
    
    pareto_scores = {
        uid: (score ** (1.0 / alpha) if score > 0 else 0.0)
        for uid, score in rewards.items()
    }

    return normalize_rewards_linear(pareto_scores)


def normalize_rewards_linear(rewards: Dict[int, float]) -> Dict[int, float]:
    """Normalize scores to sum to 1.0, preserving ratios."""
    total = sum(rewards.values())
    if total <= 0:
        bt.logging.info("All scores are zero, returning original scores")
        return rewards
    
    normalized = {uid: score / total for uid, score in rewards.items()}

    return normalized
