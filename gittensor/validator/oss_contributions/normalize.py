from typing import Callable, Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation


def _normalize_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
    score_getter: Callable[[MinerEvaluation], float],
    label: str,
) -> Dict[int, float]:
    """Normalize scores to sum to 1.0, preserving ratios."""
    if not miner_evaluations:
        bt.logging.warning(f'{label}: no miner evaluations provided for normalization')
        return {}

    rewards: Dict[int, float] = {}
    nonzero_count = 0

    for uid, evaluation in miner_evaluations.items():
        rewards[uid] = score_getter(evaluation)
        if rewards[uid] > 0:
            nonzero_count += 1

    total = sum(rewards.values())
    if total <= 0:
        bt.logging.info(f'{label}: all scores are zero, returning original scores')
        return rewards

    normalized = {uid: score / total for uid, score in rewards.items()}
    bt.logging.info(f'{label}: normalized {nonzero_count} miners with scores > 0')
    return normalized


def normalize_rewards_linear(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """Normalize OSS contribution scores to sum to 1.0."""
    return _normalize_scores(miner_evaluations, lambda e: e.total_score, 'OSS contributions')
