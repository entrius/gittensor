# The MIT License (MIT)
# Copyright © 2025 Entrius

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation
from gittensor.constants import (
    CREDIBILITY_MULLIGAN_COUNT,
    ISSUE_REVIEW_CLEAN_BONUS,
    ISSUE_REVIEW_PENALTY_RATE,
    MAX_OPEN_ISSUE_THRESHOLD,
    MIN_ISSUE_CREDIBILITY,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    MIN_VALID_SOLVED_ISSUES,
    OPEN_ISSUE_SPAM_BASE_THRESHOLD,
    OPEN_ISSUE_SPAM_TOKEN_SCORE_PER_SLOT,
    SECONDS_PER_HOUR,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
)
from gittensor.validator.utils.load_weights import RepositoryConfig


def calculate_issue_review_quality_multiplier(changes_requested_count: int) -> float:
    """Cliff model: clean bonus when 0 changes requested, then linear penalty.

    0 rounds → 1.1 (clean bonus)
    1 round  → 0.85
    2 rounds → 0.70
    7+ rounds → 0.0
    """
    if changes_requested_count == 0:
        return ISSUE_REVIEW_CLEAN_BONUS
    return max(0.0, 1.0 - ISSUE_REVIEW_PENALTY_RATE * changes_requested_count)


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


def check_issue_eligibility(solved_count: int, closed_count: int) -> Tuple[bool, float, str]:
    """Check if a miner passes the issue discovery eligibility gate.

    Returns (is_eligible, issue_credibility, reason).
    """
    credibility = calculate_issue_credibility(solved_count, closed_count)

    if solved_count < MIN_VALID_SOLVED_ISSUES:
        return False, credibility, f'{solved_count}/{MIN_VALID_SOLVED_ISSUES} valid solved issues'

    if credibility < MIN_ISSUE_CREDIBILITY:
        return False, credibility, f'Issue credibility {credibility:.2f} < {MIN_ISSUE_CREDIBILITY}'

    return True, credibility, ''


def _calculate_time_decay_from_merge(merged_at: datetime) -> float:
    """Time decay anchored to a PR's merge date. Same sigmoid as OSS contributions."""
    now = datetime.now(timezone.utc)
    hours_since_merge = (now - merged_at).total_seconds() / SECONDS_PER_HOUR

    if hours_since_merge < TIME_DECAY_GRACE_PERIOD_HOURS:
        return 1.0

    days_since_merge = hours_since_merge / 24
    sigmoid = 1 / (1 + math.exp(TIME_DECAY_SIGMOID_STEEPNESS_SCALAR * (days_since_merge - TIME_DECAY_SIGMOID_MIDPOINT)))
    return max(sigmoid, TIME_DECAY_MIN_MULTIPLIER)


def score_discovered_issues(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    scan_issues: Optional[Dict[str, List[Issue]]] = None,
) -> None:
    """Score issue discovery for all miners. Mutates miner_evaluations in place.

    Args:
        miner_evaluations: All miner evaluations (from OSS scoring phase)
        master_repositories: Repository configs for repo_weight lookup
        scan_issues: Issues from repo-centric scan, keyed by github_id
    """
    bt.logging.info('**Scoring issue discovery**')

    # Build github_id → uid mapping for all valid miners
    github_id_to_uid: Dict[str, int] = {}
    for uid, evaluation in miner_evaluations.items():
        if evaluation.github_id and evaluation.github_id != '0':
            github_id_to_uid[evaluation.github_id] = uid

    if not github_id_to_uid:
        bt.logging.info('No valid miners for issue discovery')
        return

    # Collect all discoverer data: github_id → {solved_issues, closed_count, scored_issues}
    discoverer_data: Dict[str, _DiscovererData] = defaultdict(lambda: _DiscovererData())

    # Phase 1: Collect issues from all miners' merged PRs
    _collect_issues_from_prs(miner_evaluations, github_id_to_uid, discoverer_data, master_repositories)

    # Phase 2: Merge in scan issues (from repo-centric closed scan)
    if scan_issues:
        _merge_scan_issues(scan_issues, github_id_to_uid, discoverer_data)

    if not discoverer_data:
        bt.logging.info('No issue discovery data found')
        return

    # Phase 3: For each discoverer, check eligibility and compute scores
    for discoverer_github_id, data in discoverer_data.items():
        uid = github_id_to_uid.get(discoverer_github_id)
        if uid is None:
            continue

        evaluation = miner_evaluations.get(uid)
        if not evaluation:
            continue

        evaluation.total_solved_issues = data.solved_count
        evaluation.total_valid_solved_issues = data.valid_solved_count
        evaluation.total_closed_issues = data.closed_count
        evaluation.issue_token_score = round(data.issue_token_score, 2)

        is_eligible, credibility, reason = check_issue_eligibility(data.valid_solved_count, data.closed_count)
        evaluation.is_issue_eligible = is_eligible
        evaluation.issue_credibility = credibility

        if not is_eligible:
            bt.logging.info(f'UID {uid} issue discovery ineligible: {reason}')
            continue

        # Calculate spam multiplier once per miner (uses sum of solving PR token_scores)
        spam_mult = calculate_open_issue_spam_multiplier(evaluation.total_open_issues, data.issue_token_score)

        # Score each eligible issue
        total_discovery_score = 0.0
        for issue in data.scored_issues:
            issue.discovery_credibility_multiplier = round(credibility, 2)
            issue.discovery_open_issue_spam_multiplier = spam_mult
            issue.discovery_earned_score = round(
                issue.discovery_base_score
                * issue.discovery_repo_weight_multiplier
                * issue.discovery_time_decay_multiplier
                * issue.discovery_review_quality_multiplier
                * issue.discovery_credibility_multiplier
                * issue.discovery_open_issue_spam_multiplier,
                2,
            )
            total_discovery_score += issue.discovery_earned_score

        evaluation.issue_discovery_score = round(total_discovery_score, 2)

        bt.logging.info(
            f'UID {uid} issue discovery: {data.solved_count} solved, {data.closed_count} closed, '
            f'credibility={credibility:.2f}, score={evaluation.issue_discovery_score:.2f}'
        )

    bt.logging.info('Issue discovery scoring complete.')


class _DiscovererData:
    """Accumulator for a single discoverer's issue data."""

    __slots__ = ('solved_count', 'valid_solved_count', 'closed_count', 'scored_issues', 'issue_token_score')

    def __init__(self):
        self.solved_count: int = 0
        self.valid_solved_count: int = 0  # solved where solving PR has token_score >= 5
        self.closed_count: int = 0
        self.scored_issues: List[Issue] = []
        self.issue_token_score: float = 0.0  # sum of solving PR token_scores


def _collect_issues_from_prs(
    miner_evaluations: Dict[int, MinerEvaluation],
    github_id_to_uid: Dict[str, int],
    discoverer_data: Dict[str, _DiscovererData],
    master_repositories: Dict[str, RepositoryConfig],
) -> None:
    """Collect issues from all miners' merged PRs and attribute to discoverers.

    Enforces one-issue-per-PR rule: earliest-created issue gets score, others credibility only.
    """
    # Track which PRs have already awarded a discovery score (one-issue-per-PR rule)
    pr_scored: set = set()  # (repo, pr_number)

    for uid, evaluation in miner_evaluations.items():
        for pr in evaluation.merged_pull_requests:
            if not pr.issues or not pr.merged_at:
                continue

            # Sort issues by creation date (earliest first) for one-issue-per-PR selection
            sorted_issues = sorted(
                [i for i in pr.issues if i.author_github_id],
                key=lambda i: i.created_at or datetime.max.replace(tzinfo=timezone.utc),
            )

            for issue in sorted_issues:
                discoverer_id = issue.author_github_id
                if not discoverer_id or discoverer_id not in github_id_to_uid:
                    continue

                data = discoverer_data[discoverer_id]

                # Classify: is this issue solved (merged PR closed it)?
                is_solved = issue.state == 'CLOSED' and pr.merged_at is not None

                if is_solved:
                    data.solved_count += 1
                else:
                    data.closed_count += 1
                    continue  # No score for unsolved issues

                # Anti-gaming: post-merge edit detection
                # Use last_edited_at (body/title edits only) instead of updated_at
                # (which gets updated by ANY issue activity including closing)
                edit_time = issue.last_edited_at or issue.updated_at
                if edit_time and pr.merged_at and edit_time > pr.merged_at:
                    bt.logging.info(
                        f'Issue #{issue.number} edited after PR #{pr.number} merge — 0 score, counts as closed'
                    )
                    data.solved_count -= 1
                    data.closed_count += 1
                    continue

                # Count valid solved (PR quality gate only — independent of same-account/one-per-PR)
                if pr.token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE:
                    data.valid_solved_count += 1

                # Same-account: discoverer == solver → 0 score but credibility counts
                if discoverer_id == pr.github_id:
                    continue

                # One-issue-per-PR: only the first (earliest-created) issue gets scored
                pr_key = (pr.repository_full_name, pr.number)
                if pr_key in pr_scored:
                    continue  # Credibility already counted above, skip scoring
                pr_scored.add(pr_key)

                # Check solving PR quality gate for scoring
                if pr.token_score < MIN_TOKEN_SCORE_FOR_BASE_SCORE:
                    continue

                # Populate discovery scoring fields
                repo_config = master_repositories.get(pr.repository_full_name)
                issue.discovery_base_score = pr.base_score
                issue.discovery_repo_weight_multiplier = round(repo_config.weight if repo_config else 0.01, 2)
                issue.discovery_time_decay_multiplier = round(_calculate_time_decay_from_merge(pr.merged_at), 2)
                issue.discovery_review_quality_multiplier = round(
                    calculate_issue_review_quality_multiplier(pr.changes_requested_count), 2
                )
                # credibility and spam multipliers applied in the main loop after eligibility check

                data.scored_issues.append(issue)
                data.issue_token_score += pr.token_score


def _merge_scan_issues(
    scan_issues: Dict[str, List[Issue]],
    github_id_to_uid: Dict[str, int],
    discoverer_data: Dict[str, _DiscovererData],
) -> None:
    """Merge repo-scan results into discoverer data.

    Scan issues are pre-classified as solved (case 2) or closed (case 3).
    They only contribute to credibility — no discovery score since the solving PR
    was by a non-miner (or no solver found).
    """
    for github_id, issues in scan_issues.items():
        if github_id not in github_id_to_uid:
            continue

        data = discoverer_data[github_id]
        for issue in issues:
            if issue.state == 'CLOSED' and issue.closed_at:
                # Case 2: solved by non-miner PR → positive credibility
                data.solved_count += 1
            else:
                # Case 3: closed without PR → negative credibility
                data.closed_count += 1
