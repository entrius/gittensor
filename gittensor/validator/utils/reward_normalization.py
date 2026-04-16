# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared linear normalization for validator reward dicts."""

from typing import Dict


def normalize_reward_ratios(rewards: Dict[int, float]) -> Dict[int, float]:
    """Scale scores so they sum to 1.0, preserving ratios.

    If ``rewards`` is empty or the sum of values is non-positive, returns ``rewards``
    unchanged (caller handles logging for those cases).
    """
    if not rewards:
        return rewards
    total = sum(rewards.values())
    if total <= 0:
        return rewards
    return {uid: score / total for uid, score in rewards.items()}
