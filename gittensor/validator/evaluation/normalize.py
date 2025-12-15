from typing import Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation


def normalize_rewards_linear(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """Normalize scores to sum to 1.0, preserving ratios."""

    if not miner_evaluations:
        bt.logging.warning("No miner evaluations provided for normalization")
        return {}

    rewards: Dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        rewards[uid] = evaluation.total_score
        bt.logging.info(f"Final reward for uid {uid}: {rewards[uid]:.2f}")

    total = sum(rewards.values())
    if total <= 0:
        bt.logging.info("All scores are zero, returning original scores")
        return rewards

    normalized = {uid: score / total for uid, score in rewards.items()}

    return normalized
