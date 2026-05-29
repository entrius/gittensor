# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import TYPE_CHECKING, Sequence, Tuple

if TYPE_CHECKING:
    from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
    from gittensor.validator.utils.load_weights import ResolvedEligibility


def calculate_credibility(merged_prs: Sequence['ScoredPR'], closed_prs: Sequence['ScoredPR']) -> float:
    """Calculate the flat credibility ratio: merged / (merged + closed).

    Returns credibility in [0.0, 1.0], or 0.0 when there are no attempts.
    """
    total_attempts = len(merged_prs) + len(closed_prs)
    if total_attempts == 0:
        return 0.0

    return len(merged_prs) / total_attempts


def check_eligibility(
    merged_prs: Sequence['ScoredPR'],
    closed_prs: Sequence['ScoredPR'],
    cfg: 'ResolvedEligibility',
) -> Tuple[bool, float, str]:
    """Check whether a miner passes one repository's eligibility gate.

    Gate requires:
    1. At least ``cfg.min_valid_merged_prs`` merged PRs
    2. At least ``cfg.min_credibility`` credibility

    Returns:
        (is_eligible, credibility, reason)
        reason is an empty string when eligible, otherwise explains why not.
    """
    credibility = calculate_credibility(merged_prs, closed_prs)

    merged_count = len(merged_prs)
    if merged_count < cfg.min_valid_merged_prs:
        return False, credibility, f'{merged_count}/{cfg.min_valid_merged_prs} merged PRs'

    if credibility < cfg.min_credibility:
        return False, credibility, f'credibility {credibility:.2f} < {cfg.min_credibility} minimum'

    return True, credibility, ''
