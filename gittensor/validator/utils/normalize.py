from typing import Dict

import bittensor as bt


def normalize_scores(scores: Dict[int, float]) -> Dict[int, float]:
    """Normalize scores to sum to 1.0, preserving ratios.

    Args:
        scores: Mapping of uid to raw score.

    Returns:
        Mapping of uid to normalized score. Returns the original scores
        unchanged when the total is zero or negative.
    """
    if not scores:
        return {}

    total = sum(scores.values())
    if total <= 0:
        bt.logging.info('All scores are zero, returning original scores')
        return scores

    return {uid: score / total for uid, score in scores.items()}
