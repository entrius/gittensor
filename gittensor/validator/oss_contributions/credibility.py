# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import TYPE_CHECKING, List, Tuple

import bittensor as bt

from gittensor.constants import (
    MIN_CREDIBILITY,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    MIN_VALID_MERGED_PRS,
)
from gittensor.validator.utils.credibility_math import mulligan_success_ratio

if TYPE_CHECKING:
    from gittensor.classes import PullRequest


def calculate_credibility(merged_prs: List['PullRequest'], closed_prs: List['PullRequest']) -> float:
    """Calculate flat credibility ratio with mulligan applied.

    Mulligan: up to CREDIBILITY_MULLIGAN_COUNT closed PRs are erased entirely —
    they don't count in the denominator (merged + closed).

    Returns credibility in [0.0, 1.0], or 0.0 if no attempts after mulligan.
    """
    return mulligan_success_ratio(len(merged_prs), len(closed_prs))


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
    credibility = calculate_credibility(merged_prs, closed_prs)

    # Count valid merged PRs (token_score >= threshold)
    valid_merged_count = sum(1 for pr in merged_prs if pr.token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE)

    if valid_merged_count < MIN_VALID_MERGED_PRS:
        reason = f'{valid_merged_count}/{MIN_VALID_MERGED_PRS} valid merged PRs (need {MIN_VALID_MERGED_PRS})'
        bt.logging.info(f'Ineligible: {reason}')
        return False, credibility, reason

    if credibility < MIN_CREDIBILITY:
        reason = f'Credibility {credibility:.2f} < {MIN_CREDIBILITY} minimum'
        bt.logging.info(f'Ineligible: {reason}')
        return False, credibility, reason

    bt.logging.info(f'Eligible: {valid_merged_count} valid PRs, credibility {credibility:.2f}')
    return True, credibility, ''
