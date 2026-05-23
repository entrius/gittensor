# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import TYPE_CHECKING, Sequence, Tuple

if TYPE_CHECKING:
    from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
    from gittensor.validator.utils.load_weights import ResolvedEligibility

# Beta prior strength for volume-aware credibility smoothing.
# A couple of closed PRs no longer sink an active miner.
PRIOR_K = 2


def calculate_credibility(merged_prs: Sequence['ScoredPR'], closed_prs: Sequence['ScoredPR']) -> float:
    """Calculate a volume-aware credibility ratio with Beta prior.

    Formula: (merged + PRIOR_K) / (merged + closed + 2 * PRIOR_K)

    A small prior prevents a handful of closed PRs from tanking an
    otherwise productive miner (see issue #1340). Returns credibility
    in [0.0, 1.0], or 0.5 when there are no attempts (pure prior).
    """
    total_attempts = len(merged_prs) + len(closed_prs)
    if total_attempts == 0:
        return 0.0

    return (len(merged_prs) + PRIOR_K) / (total_attempts + 2 * PRIOR_K)


def check_eligibility(
    merged_prs: Sequence['ScoredPR'],
    closed_prs: Sequence['ScoredPR'],
    cfg: 'ResolvedEligibility',
) -> Tuple[bool, float, str]:
    """Check whether a miner passes one repository's eligibility gate.

    Gate requires:
    1. At least ``cfg.min_valid_merged_prs`` merged PRs with
       ``token_score >= cfg.min_token_score_for_base_score``
    2. At least ``cfg.min_credibility`` credibility

    Returns:
        (is_eligible, credibility, reason)
        reason is an empty string when eligible, otherwise explains why not.
    """
    credibility = calculate_credibility(merged_prs, closed_prs)

    valid_merged_count = sum(1 for pr in merged_prs if pr.token_score >= cfg.min_token_score_for_base_score)

    if valid_merged_count < cfg.min_valid_merged_prs:
        return False, credibility, f'{valid_merged_count}/{cfg.min_valid_merged_prs} valid merged PRs'

    if credibility < cfg.min_credibility:
        return False, credibility, f'credibility {credibility:.2f} < {cfg.min_credibility} minimum'

    return True, credibility, ''
