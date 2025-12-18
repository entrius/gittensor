# The MIT License (MIT)
# Copyright © 2025 Entrius

import math
from datetime import datetime, timezone
from typing import Dict

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation, PRState, PullRequest
from gittensor.constants import (
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    MAX_ISSUES_SCORED_IN_SINGLE_PR,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    UNIQUE_PR_BOOST,
    MAX_ISSUE_AGE_FOR_MAX_SCORE,
    EXCESSIVE_PR_PENALTY_THRESHOLD,
    EXCESSIVE_PR_PENALTY_SLOPE,
    EXCESSIVE_PR_MIN_MULTIPLIER,
    GITTENSOR_TAGLINE_BOOST,
    GITTENSOR_REPOSITORY,
    MERGE_SUCCESS_RATIO_ATTEMPTS_THRESHOLD,
    MERGE_SUCCESS_RATIO_APPLICATION_DATE,
    POTENTIAL_SCORE_COLLATERAL_PERCENT,
    COLLATERAL_EFFECTIVE_DATE,  # Used in apply_cross_miner_multipliers_and_finalize for reinflation
    COLLATERAL_REINFLATION_MULTIPLIER,
)
from gittensor.utils.github_api_tools import get_pull_request_file_changes, normalize_repo_name

def score_merged_pull_requests(
    miner_eval: MinerEvaluation,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> None:
    """Score merged pull requests and populate MinerEvaluation object."""
    if not miner_eval.merged_pull_requests:
        bt.logging.info(f"No merged PRs for uid {miner_eval.uid}")
        return

    total_prs = len(miner_eval.merged_pull_requests)
    bt.logging.info(f"Scoring {total_prs} merged PRs for uid {miner_eval.uid}")

    for n, pr in enumerate(miner_eval.merged_pull_requests, start=1):
        bt.logging.info(f"\n[{n}/{total_prs}] - MERGED PR #{pr.number} in {pr.repository_full_name}")

        file_changes = get_pull_request_file_changes(pr.repository_full_name, pr.number, miner_eval.github_pat)
        if not file_changes:
            bt.logging.warning("No file changes found.")
            continue

        pr.set_file_changes(file_changes)

        normalized_repo = normalize_repo_name(pr.repository_full_name)
        pr.repo_weight_multiplier = round(master_repositories.get(pr.repository_full_name, {}).get("weight", 0.01), 2)
        pr.base_score = round(pr.calculate_score_from_file_changes(programming_languages), 2)
        pr.issue_multiplier = round(calculate_issue_multiplier(pr), 2)
        pr.open_pr_spam_multiplier = round(calculate_pr_spam_penalty_multiplier(miner_eval.total_open_prs), 2)
        pr.time_decay_multiplier = round(calculate_time_decay_multiplier(pr), 2)
        pr.gittensor_tag_multiplier = round(GITTENSOR_TAGLINE_BOOST if (pr.gittensor_tagged and pr.repository_full_name.lower() != GITTENSOR_REPOSITORY.lower()) else 1.0, 2)
        pr.merge_success_multiplier = round(calculate_merge_success_multiplier(miner_eval) if pr.merged_at > MERGE_SUCCESS_RATIO_APPLICATION_DATE else 1.0, 2)

        miner_eval.unique_repos_contributed_to.add(pr.repository_full_name)


def count_repository_contributors(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[str, int]:
    """
    Count how many miners contribute to each repository and log statistics.
    Uses normalized (lowercase) repository names for case-insensitive counting.

    Returns:
        Dict[str, int]: Dictionary mapping normalized repository names to contributor counts
    """
    repo_counts: Dict[str, int] = {}

    for evaluation in miner_evaluations.values():
        for repo in evaluation.unique_repos_contributed_to:
            # Repository names are already normalized when added to unique_repos_contributed_to
            repo_counts[repo] = repo_counts.get(repo, 0) + 1

    if repo_counts:
        bt.logging.info(f"Repository contribution counts: {len(repo_counts)} total repositories")
        for repo, count in sorted(repo_counts.items(), key=lambda x: -x[1]):
            bt.logging.info(f"{repo}: {count}")

    return repo_counts


def calculate_pr_spam_penalty_multiplier(total_open_prs: int) -> float:
    """Apply penalty for excessive open PRs"""
    if total_open_prs <= EXCESSIVE_PR_PENALTY_THRESHOLD:
        return 1.0

    excess_pr_count = total_open_prs - EXCESSIVE_PR_PENALTY_THRESHOLD
    calculated_multiplier = 1.0 - (excess_pr_count * EXCESSIVE_PR_PENALTY_SLOPE)
    return max(EXCESSIVE_PR_MIN_MULTIPLIER, calculated_multiplier)


def calculate_merge_success_multiplier(miner_eval: MinerEvaluation) -> float:
    """Calculate multiplier based on PR merge success ratio."""
    total_prs = miner_eval.total_merged_prs + miner_eval.total_closed_prs

    if total_prs <= 0 or total_prs < MERGE_SUCCESS_RATIO_ATTEMPTS_THRESHOLD:
        return 1.0
    
    merge_ratio = miner_eval.total_merged_prs / total_prs
    return merge_ratio


def calculate_time_decay_multiplier(pr: PullRequest) -> float:
    """Calculate time decay multiplier for a single PR based on merge date."""

    now = datetime.now(timezone.utc)
    hours_since_merge = (now - pr.merged_at).total_seconds() / SECONDS_PER_HOUR

    # No decay for PRs merged within the grace period
    if hours_since_merge < TIME_DECAY_GRACE_PERIOD_HOURS:
        return 1.0

    days_since_merge = hours_since_merge / 24
    sigmoid = 1 / (1 + math.exp(TIME_DECAY_SIGMOID_STEEPNESS_SCALAR * (days_since_merge - TIME_DECAY_SIGMOID_MIDPOINT)))
    return max(sigmoid, TIME_DECAY_MIN_MULTIPLIER)


def apply_cross_miner_multipliers_and_finalize(miner_evaluations: Dict[int, MinerEvaluation]) -> None:
    """Apply uniqueness multipliers and finalize scores for merged PRs.

    Merged PRs created after COLLATERAL_EFFECTIVE_DATE receive reinflation boost.
    """
    bt.logging.info("**Finalizing merged PR scores**")

    repo_counts = count_repository_contributors(miner_evaluations)
    if not repo_counts:
        bt.logging.info("No repository contributions found")
        return

    total_contributing_miners = sum(1 for ev in miner_evaluations.values() if ev.unique_repos_contributed_to)
    total_prs = 0

    for uid, evaluation in miner_evaluations.items():
        if not evaluation or not evaluation.merged_pull_requests:
            continue

        total_prs += len(evaluation.merged_pull_requests)
        bt.logging.info(f"\n***Finalizing scores for uid {uid}***")

        for pr in evaluation.merged_pull_requests:
            # Apply uniqueness multiplier (cross-miner dependent)
            # Use normalized repository name for case-insensitive lookup
            normalized_repo = normalize_repo_name(pr.repository_full_name)
            repo_count = repo_counts.get(normalized_repo, 0)
            uniqueness_score = (total_contributing_miners - repo_count + 1) / total_contributing_miners
            uniqueness_multiplier = 1.0 + (uniqueness_score * UNIQUE_PR_BOOST)
            pr.repository_uniqueness_multiplier = uniqueness_multiplier

            # Calculate final earned score now that all multipliers are set
            pr.calculate_final_earned_score()

            # Reinflation boost for PRs created after collateral effective date
            if pr.created_at > COLLATERAL_EFFECTIVE_DATE:
                original = pr.earned_score
                pr.earned_score *= COLLATERAL_REINFLATION_MULTIPLIER
                bt.logging.info(f"  PR #{pr.number} reinflation: {original:.2f} -> {pr.earned_score:.2f}")

            evaluation.base_total_score += pr.base_score
            evaluation.total_score += pr.earned_score
            evaluation.total_lines_changed += pr.total_lines_scored

        evaluation.unique_repos_count = len(evaluation.unique_repos_contributed_to)
        bt.logging.info(f"UID {uid} total score: {evaluation.total_score:.2f} ({len(evaluation.merged_pull_requests)} PRs)")

    bt.logging.info(f"Finalized {total_prs} merged PRs across {total_contributing_miners} miners")


def calculate_issue_multiplier(pr: PullRequest) -> float:
    """
    Calculate pr score multiplier based on age and number of resolved issues.

    - Base multiplier: 1.0 (no bonus)
    - Each issue adds 0.09-0.90 to multiplier based on age (sqrt scaling)
    - Maximum 3 issues counted (max multiplier: 3.7)
    - 100% of issue bonus earned when issue has been open for MAX_ISSUE_AGE_FOR_MAX_SCORE+ days

    Returns:
        float: Multiplier between 1.0 and 3.7
    """
    if not pr.issues:
        bt.logging.info(f"PR #{pr.number} - resolved no issues.")
        return 1.0

    valid_issues = [issue for issue in pr.issues if _is_valid_issue(issue, pr)]

    if not valid_issues:
        bt.logging.info(f"PR #{pr.number} - found no valid issues")
        return 1.0

    num_issues = min(len(valid_issues), MAX_ISSUES_SCORED_IN_SINGLE_PR)
    bt.logging.info(f"Calculating issue multiplier for PR #{pr.number} with {num_issues} issues")

    total_issue_multiplier = 0.0
    for i in range(num_issues):
        issue = valid_issues[i]
        issue_num = getattr(issue, 'number', i + 1)

        if not (issue.created_at and issue.closed_at):
            bt.logging.info(f"Issue #{issue_num} - No date info, using default score: 0.10")
            total_issue_multiplier += 0.1
            continue

        try:
            days_open = (issue.closed_at - issue.created_at).days
            normalized = 0.1 + math.sqrt(min(days_open, MAX_ISSUE_AGE_FOR_MAX_SCORE)) / math.sqrt(MAX_ISSUE_AGE_FOR_MAX_SCORE)
            multiplier = 0.9 * min(normalized, 1.0)
            bt.logging.info(f"Issue #{issue_num} - Open for {days_open} days | multiplier: {multiplier:.2f}")
            total_issue_multiplier += multiplier
            
        except (ValueError, AttributeError) as e:
            bt.logging.warning(f"Issue #{issue_num} - Could not parse issue dates. Using default score: 0.10. Exception: {e}")
            total_issue_multiplier += 0.1

    final_multiplier = 1.0 + total_issue_multiplier
    bt.logging.info(f"Issue multiplier for pr #{pr.number} | multiplier: {final_multiplier:.2f}")

    return final_multiplier


def _is_valid_issue(issue: Issue, pr: PullRequest) -> bool:
    """Check if issue is valid for bonus calculation."""

    # Only set valid to True if PR was NOT edited after being merged
    # (to prevent miners from editing after merge to add irrelevant closed issues)
    if pr.last_edited_at and pr.last_edited_at > pr.merged_at:
        bt.logging.warning(f"Skipping issue #{issue.number} - edited after PR merge")
        return False

    if issue.state and issue.state != 'CLOSED':
        bt.logging.warning(f"Skipping issue #{issue.number} - not CLOSED (state: {issue.state})")
        return False

    if not issue.author_login:
        bt.logging.warning(f"Skipping issue #{issue.number} - missing author information")
        return False

    if issue.author_login == pr.author_login:
        bt.logging.warning(f"Skipping issue #{issue.number} - same author as PR (self-created issue gaming)")
        return False

    if issue.created_at and pr.created_at and issue.created_at > pr.created_at:
        bt.logging.warning(f"Skipping issue #{issue.number} - created after PR")
        return False

    if issue.closed_at and pr.merged_at:
        days_diff = abs((issue.closed_at - pr.merged_at).total_seconds()) / SECONDS_PER_DAY
        if days_diff > MAX_ISSUE_CLOSE_WINDOW_DAYS:
            bt.logging.warning(f"Skipping issue #{issue.number} - closed {days_diff:.1f}d from PR merge (max: {MAX_ISSUE_CLOSE_WINDOW_DAYS})")
            return False

    return True


def calculate_issue_multiplier_for_open_pr(pr: PullRequest) -> float:
    """
    Calculate issue multiplier for an open PR.

    Treats linked issues as if they would be closed when the PR merges.
    Uses current time instead of closed_at since the issues aren't closed yet.

    Returns:
        float: Multiplier between 1.0 and 3.7 (same scale as merged PRs)
    """
    if not pr.issues:
        bt.logging.info(f"OPEN PR #{pr.number} - no linked issues")
        return 1.0

    valid_issues = [issue for issue in pr.issues if _is_valid_issue_for_open_pr(issue, pr)]

    if not valid_issues:
        bt.logging.info(f"OPEN PR #{pr.number} - no valid linked issues")
        return 1.0

    num_issues = min(len(valid_issues), MAX_ISSUES_SCORED_IN_SINGLE_PR)
    bt.logging.info(f"Calculating issue multiplier for OPEN PR #{pr.number} with {num_issues} linked issues")

    now = datetime.now(timezone.utc)
    total_issue_multiplier = 0.0

    for i in range(num_issues):
        issue = valid_issues[i]
        issue_num = getattr(issue, 'number', i + 1)

        if not issue.created_at:
            bt.logging.info(f"Issue #{issue_num} - No creation date, using default: 0.10")
            total_issue_multiplier += 0.1
            continue

        try:
            # For open PRs: calculate age from creation to NOW (issue isn't closed yet)
            days_open = (now - issue.created_at).days
            normalized = 0.1 + math.sqrt(min(days_open, MAX_ISSUE_AGE_FOR_MAX_SCORE)) / math.sqrt(MAX_ISSUE_AGE_FOR_MAX_SCORE)
            multiplier = 0.9 * min(normalized, 1.0)
            bt.logging.info(f"Issue #{issue_num} - Open for {days_open} days | multiplier: {multiplier:.2f}")
            total_issue_multiplier += multiplier

        except (ValueError, AttributeError) as e:
            bt.logging.warning(f"Issue #{issue_num} - Could not calculate age. Using default: 0.10. Exception: {e}")
            total_issue_multiplier += 0.1

    final_multiplier = 1.0 + total_issue_multiplier
    bt.logging.info(f"Issue multiplier for OPEN PR #{pr.number}: {final_multiplier:.2f}")

    return final_multiplier


def _is_valid_issue_for_open_pr(issue: Issue, pr: PullRequest) -> bool:
    """Check if issue is valid for open PR collateral calculation.

    Similar to _is_valid_issue but doesn't require the issue to be CLOSED.
    """
    # Don't allow self-created issues (gaming prevention)
    if not issue.author_login:
        bt.logging.warning(f"Skipping issue #{issue.number} - missing author information")
        return False

    if issue.author_login == pr.author_login:
        bt.logging.warning(f"Skipping issue #{issue.number} - same author as PR (self-created issue)")
        return False

    # Issue must have been created before the PR
    if issue.created_at and pr.created_at and issue.created_at > pr.created_at:
        bt.logging.warning(f"Skipping issue #{issue.number} - created after PR")
        return False

    return True


# =============================================================================
# Collateral System Functions
# =============================================================================

def score_open_prs_for_collateral(
    miner_eval: MinerEvaluation,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> None:
    """
    Score open pull requests for collateral calculation.

    Only PRs created after COLLATERAL_EFFECTIVE_DATE are eligible (filtered at add time in reward.py).

    Collateral multipliers applied:
    - repo_weight_multiplier: Applied (incentivized repos)
    - base_score: Calculated from file changes
    - issue_multiplier: Applied (treats linked issues as if they would be closed)
    - gittensor_tag_multiplier: Applied

    Multipliers NOT applied (merge-dependent or cross-miner):
    - time_decay_multiplier: Not applicable (not merged)
    - merge_success_multiplier: Not applicable (not merged)
    - open_pr_spam_multiplier: Not applied to collateral
    - repository_uniqueness_multiplier: Not applicable (cross-miner)

    Args:
        miner_eval (MinerEvaluation): MinerEvaluation object with open_pull_requests
        master_repositories (Dict[str, Dict]): The incentivized repositories and their metadata
        programming_languages (Dict[str, float]): The programming languages and their weights
    """
    if not miner_eval.open_pull_requests:
        bt.logging.info(f"No open PRs for collateral scoring for uid {miner_eval.uid}")
        return

    total_prs = len(miner_eval.open_pull_requests)
    bt.logging.info(f"Scoring {total_prs} open PRs for collateral for uid {miner_eval.uid}")

    for n, pr in enumerate(miner_eval.open_pull_requests, start=1):
        bt.logging.info(f"\n[{n}/{total_prs}] - Scoring OPEN PR #{pr.number} in {pr.repository_full_name} for collateral")

        file_changes = get_pull_request_file_changes(pr.repository_full_name, pr.number, miner_eval.github_pat)

        if not file_changes:
            bt.logging.warning("No file changes found for this open PR.")
            continue

        pr.set_file_changes(file_changes)

        repo_weight = master_repositories.get(pr.repository_full_name, {}).get("weight", 0.01)
        file_change_score = pr.calculate_score_from_file_changes(programming_languages)
        gittensor_tag_multiplier = GITTENSOR_TAGLINE_BOOST if (pr.gittensor_tagged and pr.repository_full_name.lower() != GITTENSOR_REPOSITORY.lower()) else 1.0

        pr.repo_weight_multiplier = round(repo_weight, 2)
        pr.base_score = round(file_change_score, 2)
        pr.issue_multiplier = round(calculate_issue_multiplier_for_open_pr(pr), 2)
        pr.open_pr_spam_multiplier = 1.0  # Not applied to collateral
        pr.time_decay_multiplier = 1.0  # Not applicable to open PRs
        pr.gittensor_tag_multiplier = round(gittensor_tag_multiplier, 2)
        pr.merge_success_multiplier = 1.0  # Not applicable to open PRs


def calculate_open_pr_collateral_score(pr: PullRequest) -> float:
    """
    Calculate collateral score for an open PR.

    Collateral = base_score * applicable_multipliers * POTENTIAL_SCORE_COLLATERAL_PERCENT

    Applicable multipliers: repo_weight, issue, gittensor_tag
    NOT applicable: time_decay (merge-based), merge_success (merge-based),
                    uniqueness (cross-miner), open_pr_spam (not for collateral)
    """
    from math import prod

    multipliers = {
        "repo_weight": pr.repo_weight_multiplier,
        "issue": pr.issue_multiplier,
        "gittensor_tag": pr.gittensor_tag_multiplier,
    }

    potential_score = pr.base_score * prod(multipliers.values())
    collateral_score = potential_score * POTENTIAL_SCORE_COLLATERAL_PERCENT

    mult_str = " | ".join([f"{k}: {v:.2f}" for k, v in multipliers.items()])
    bt.logging.info(
        f"OPEN PR #{pr.number} | base: {pr.base_score:.2f} | {mult_str} | "
        f"potential: {potential_score:.2f} | collateral ({POTENTIAL_SCORE_COLLATERAL_PERCENT*100:.0f}%): {collateral_score:.2f}"
    )

    return collateral_score


def apply_collateral_deduction(miner_evaluations: Dict[int, MinerEvaluation]) -> None:
    """
    Apply collateral deduction to miner total scores.

    Date eligibility is checked at add time in reward.py - all open_pull_requests are eligible.
    Collateral is calculated per-PR and summed, then deducted from total_score (never below 0).
    """
    bt.logging.info("**Applying collateral deduction**")

    for uid, evaluation in miner_evaluations.items():
        # Only process PRs that were scored (have base_score > 0)
        scored_open_prs = [pr for pr in evaluation.open_pull_requests if pr.base_score > 0.0]

        if not scored_open_prs:
            evaluation.total_collateral_score = 0.0
            continue

        bt.logging.info(f"\n***Collateral for UID {uid} ({len(scored_open_prs)} scored open PRs)***")

        total_collateral = 0.0
        for pr in scored_open_prs:
            collateral = calculate_open_pr_collateral_score(pr)
            pr.collateral_score = collateral
            total_collateral += collateral

        evaluation.total_collateral_score = round(total_collateral, 2)
        original_score = evaluation.total_score
        evaluation.total_score = max(0.0, evaluation.total_score - total_collateral)

        bt.logging.info(
            f"UID {uid}: earned={original_score:.2f} - collateral={total_collateral:.2f} = final={evaluation.total_score:.2f}"
        )

    bt.logging.info("Collateral deduction complete.")