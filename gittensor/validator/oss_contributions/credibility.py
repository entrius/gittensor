# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import TYPE_CHECKING, List, Tuple

import bittensor as bt

from gittensor.constants import (
    MIN_CREDIBILITY,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    MIN_VALID_MERGED_PRS,
)
from gittensor.validator.utils.credibility import check_eligibility_gate, credibility_with_mulligan

if TYPE_CHECKING:
    from gittensor.classes import PullRequest


def calculate_credibility(merged_prs: List['PullRequest'], closed_prs: List['PullRequest']) -> float:
    """Calculate flat credibility ratio with mulligan applied.

    Mulligan: up to CREDIBILITY_MULLIGAN_COUNT closed PRs are erased entirely —
    they don't count in the denominator (merged + closed).

    Returns credibility in [0.0, 1.0], or 0.0 if no attempts after mulligan.
    """
    return credibility_with_mulligan(len(merged_prs), len(closed_prs))


def check_eligibility(merged_prs: List['PullRequest'], closed_prs: List['PullRequest']) -> Tuple[bool, float, str]:
    """Check if a miner passes the eligibility gate.

    Gate requires:
    1. At least MIN_VALID_MERGED_PRS merged PRs with token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE
       (after mulligan — if a closed PR was "valid", it no longer counts toward the minimum)
    2. At least MIN_CREDIBILITY credibility (after mulligan)

    Returns:
        (is_eligible, credibility, reason)
        reason is empty string if eligible, otherwise explains why not.
    """
    # Count valid merged PRs (token_score >= threshold)
    valid_merged_count = sum(1 for pr in merged_prs if pr.token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE)

    is_eligible, credibility, reason = check_eligibility_gate(
        valid_merged_count, len(closed_prs), MIN_VALID_MERGED_PRS, MIN_CREDIBILITY,
    )

    if is_eligible:
        bt.logging.info(f'Eligible: {valid_merged_count} valid PRs, credibility {credibility:.2f}')
    else:
        bt.logging.info(f'Ineligible: {reason}')

    return is_eligible, credibility, reason
