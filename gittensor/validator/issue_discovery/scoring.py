# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared issue-discovery scoring helpers.

The legacy timeline-scraping path (``score_discovered_issues``,
``_collect_issues_from_prs``, ``_merge_scan_issues``, ``scan_closed_issues``)
has been removed — unreliable solver detection was the original motivator for
migrating issue discovery to the mirror. The functions here are the math-only
helpers still used by ``issue_discovery.scan``: the review-quality multiplier,
open-issue spam threshold, credibility formula, and per-repo eligibility gate.
"""

from typing import TYPE_CHECKING, Tuple

import bittensor as bt

if TYPE_CHECKING:
    from gittensor.validator.utils.load_weights import ResolvedEligibility


def calculate_issue_review_quality_multiplier(changes_requested_count: int, review_penalty_rate: float) -> float:
    """Linear penalty on the solving PR's maintainer CHANGES_REQUESTED rounds.

    0 rounds → 1.0
    1 round  → 0.85
    7+ rounds → 0.0
    """
    multiplier = max(0.0, 1.0 - review_penalty_rate * changes_requested_count)
    if changes_requested_count > 0:
        bt.logging.info(
            f'{changes_requested_count} solving-PR CHANGES_REQUESTED review(s) → '
            f'issue_review_quality_multiplier={multiplier:.2f}'
        )
    return multiplier


def calculate_open_issue_spam_multiplier(
    cfg: 'ResolvedEligibility', total_open_issues: int, solved_token_score: float
) -> float:
    """Binary penalty for excessive open issues within one repository.

    threshold = min(base + floor(token_score / per_slot), max)
    Returns 1.0 if at or under threshold, 0.0 otherwise.
    """
    bonus = int(solved_token_score // cfg.open_issue_spam_token_score_per_slot)
    threshold = min(cfg.open_issue_spam_base_threshold + bonus, cfg.max_open_issue_threshold)
    return 1.0 if total_open_issues <= threshold else 0.0


def calculate_issue_credibility(solved_count: int, closed_count: int) -> float:
    """Calculate issue credibility: solved / (solved + closed).

    Returns credibility in [0.0, 1.0], or 0.0 when there are no attempts.
    """
    total = solved_count + closed_count
    if total == 0:
        return 0.0
    return solved_count / total


def check_issue_eligibility(
    cfg: 'ResolvedEligibility',
    solved_count: int,
    valid_solved_count: int,
    closed_count: int,
) -> Tuple[bool, float, str]:
    """Check whether a miner passes one repository's issue-discovery gate.

    Credibility uses total solved / total attempts. The gate uses
    ``valid_solved_count`` (solving PR meets the token threshold) so
    low-quality solves don't carry the miner past the minimum.

    Returns (is_eligible, issue_credibility, reason).
    """
    credibility = calculate_issue_credibility(solved_count, closed_count)

    if valid_solved_count < cfg.min_valid_solved_issues:
        return False, credibility, f'{valid_solved_count}/{cfg.min_valid_solved_issues} valid solved issues'

    if credibility < cfg.min_issue_credibility:
        return False, credibility, f'issue credibility {credibility:.2f} < {cfg.min_issue_credibility}'

    return True, credibility, ''
