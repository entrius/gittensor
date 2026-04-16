# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import Tuple

from gittensor.constants import CREDIBILITY_MULLIGAN_COUNT


def credibility_with_mulligan(success_count: int, failure_count: int) -> float:
    """Calculate credibility ratio with mulligan applied.

    Mulligan: up to CREDIBILITY_MULLIGAN_COUNT failures are erased entirely —
    they don't count in the denominator (successes + failures).

    Works for both OSS contributions (merged vs closed PRs) and
    issue discovery (solved vs closed issues).

    Returns credibility in [0.0, 1.0], or 0.0 if no attempts after mulligan.
    """
    adjusted_failures = max(0, failure_count - CREDIBILITY_MULLIGAN_COUNT)
    total = success_count + adjusted_failures
    if total == 0:
        return 0.0
    return success_count / total


def check_eligibility_gate(
    success_count: int,
    failure_count: int,
    min_success: int,
    min_credibility: float,
) -> Tuple[bool, float, str]:
    """Check if a miner passes a credibility-based eligibility gate.

    Shared logic for both OSS contributions and issue discovery:
    1. At least ``min_success`` qualifying successes
    2. At least ``min_credibility`` credibility (with mulligan)

    Returns:
        (is_eligible, credibility, reason)
        reason is empty string if eligible, otherwise explains why not.
    """
    credibility = credibility_with_mulligan(success_count, failure_count)

    if success_count < min_success:
        return False, credibility, f'{success_count}/{min_success} qualifying contributions'

    if credibility < min_credibility:
        return False, credibility, f'Credibility {credibility:.2f} < {min_credibility}'

    return True, credibility, ''
