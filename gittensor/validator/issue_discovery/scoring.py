# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared issue-discovery scoring helpers.

The legacy timeline-scraping path (``score_discovered_issues``,
``_collect_issues_from_prs``, ``_merge_scan_issues``, ``scan_closed_issues``)
has been removed — unreliable solver detection was the original motivator for
migrating issue discovery to the mirror. The functions here are the math-only
helpers still used by the mirror path (``issue_discovery.mirror_scan``): the
review-quality multiplier, open-issue spam threshold, credibility formula, and
eligibility gate.
"""

from typing import Tuple

import bittensor as bt

from gittensor.constants import (
    CREDIBILITY_MULLIGAN_COUNT,
    ISSUE_REVIEW_CLEAN_BONUS,
    ISSUE_REVIEW_PENALTY_RATE,
    MAX_OPEN_ISSUE_THRESHOLD,
    MIN_ISSUE_CREDIBILITY,
    MIN_VALID_SOLVED_ISSUES,
    OPEN_ISSUE_SPAM_BASE_THRESHOLD,
    OPEN_ISSUE_SPAM_TOKEN_SCORE_PER_SLOT,
)


def calculate_issue_review_quality_multiplier(changes_requested_count: int) -> float:
    """Cliff model: clean bonus when 0 changes requested, then linear penalty.

    0 rounds → 1.1 (clean bonus)
    1 round  → 0.85
    2 rounds → 0.70
    7+ rounds → 0.0
    """
    if changes_requested_count == 0:
        multiplier = ISSUE_REVIEW_CLEAN_BONUS
    else:
        multiplier = max(0.0, 1.0 - ISSUE_REVIEW_PENALTY_RATE * changes_requested_count)
    bt.logging.info(
        f'{changes_requested_count} solving-PR CHANGES_REQUESTED review(s) → '
        f'issue_review_quality_multiplier={multiplier:.2f}'
    )
    return multiplier


def calculate_open_issue_spam_multiplier(total_open_issues: int, solved_token_score: float) -> float:
    """Binary penalty for excessive open issues.

    threshold = min(BASE + floor(token_score / PER_SLOT), MAX)
    Returns 1.0 if under threshold, 0.0 otherwise.
    """
    bonus = int(solved_token_score // OPEN_ISSUE_SPAM_TOKEN_SCORE_PER_SLOT)
    threshold = min(OPEN_ISSUE_SPAM_BASE_THRESHOLD + bonus, MAX_OPEN_ISSUE_THRESHOLD)
    return 1.0 if total_open_issues <= threshold else 0.0


def calculate_issue_credibility(solved_count: int, closed_count: int) -> float:
    """Calculate issue credibility with mulligan.

    credibility = solved / (solved + max(0, closed - mulligan))
    """
    adjusted_closed = max(0, closed_count - CREDIBILITY_MULLIGAN_COUNT)
    total = solved_count + adjusted_closed
    if total == 0:
        return 0.0
    return solved_count / total


def check_issue_eligibility(solved_count: int, valid_solved_count: int, closed_count: int) -> Tuple[bool, float, str]:
    """Check if a miner passes the issue discovery eligibility gate.

    Credibility uses total solved / total attempts (with mulligan).
    The gate uses ``valid_solved_count`` (solving PR meets token threshold)
    so low-quality solves don't carry the miner past the minimum.

    Returns (is_eligible, issue_credibility, reason).
    """
    credibility = calculate_issue_credibility(solved_count, closed_count)

    if valid_solved_count < MIN_VALID_SOLVED_ISSUES:
        return False, credibility, f'{valid_solved_count}/{MIN_VALID_SOLVED_ISSUES} valid solved issues'

    if credibility < MIN_ISSUE_CREDIBILITY:
        return False, credibility, f'Issue credibility {credibility:.2f} < {MIN_ISSUE_CREDIBILITY}'

    return True, credibility, ''
