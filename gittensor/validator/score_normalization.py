# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared helpers for converting raw miner scores into a linear reward distribution."""

from typing import Dict, Optional

import bittensor as bt


def normalize_linear_scores(rewards: Dict[int, float], all_zero_log: Optional[str] = None) -> Dict[int, float]:
    """Normalize rewards to sum to 1.0 while preserving relative ratios.

    If the total score is zero or negative, returns ``rewards`` unchanged.
    """
    if not rewards:
        return {}

    total = sum(rewards.values())
    if total <= 0:
        if all_zero_log:
            bt.logging.info(all_zero_log)
        return rewards

    return {uid: score / total for uid, score in rewards.items()}
