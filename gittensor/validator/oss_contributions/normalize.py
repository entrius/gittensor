from typing import Dict

from gittensor.classes import MinerEvaluation
from gittensor.validator.score_normalization import normalize_miner_evaluations_linear


def normalize_rewards_linear(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """Normalize scores to sum to 1.0, preserving ratios."""

    return normalize_miner_evaluations_linear(
        miner_evaluations,
        lambda e: e.total_score,
        empty_warning='No miner evaluations provided for normalization',
        log_each_positive_uid=True,
        count_zero_log_label='0 reward',
        all_zero_log='All scores are zero, returning original scores',
    )
