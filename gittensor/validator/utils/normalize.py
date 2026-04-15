# Entrius 2025

"""Shared normalization utility for validator reward calculations."""

from typing import Dict


def normalize_scores(scores: Dict[int, float]) -> Dict[int, float]:
    """Normalize a uid→score mapping to sum to 1.0, preserving ratios.

    Returns the original dict unchanged if all scores are zero.
    """
    if not scores:
        return {}

    total = sum(scores.values())
    if total <= 0:
        return scores

    return {uid: score / total for uid, score in scores.items()}
