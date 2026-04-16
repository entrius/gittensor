# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared credibility math used by OSS and issue discovery scoring."""

from gittensor.constants import CREDIBILITY_MULLIGAN_COUNT


def mulligan_success_ratio(
    successes: int, failures: int, *, mulligan: int = CREDIBILITY_MULLIGAN_COUNT
) -> float:
    """Ratio successes / (successes + max(0, failures - mulligan)).

    Used for PR credibility (merged vs closed) and issue discovery (solved vs closed).
    Returns a value in [0.0, 1.0], or 0.0 when the denominator is zero.
    """
    adjusted_failures = max(0, failures - mulligan)
    denominator = successes + adjusted_failures
    if denominator == 0:
        return 0.0
    return successes / denominator
