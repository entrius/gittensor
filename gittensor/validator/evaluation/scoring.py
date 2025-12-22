# The MIT License (MIT)
# Copyright © 2025 Entrius

import math
from datetime import datetime, timezone
from typing import Dict, Optional

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation, PRState, PullRequest
from gittensor.constants import (
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    UNIQUE_PR_BOOST,
    MAX_ISSUE_AGE_FOR_MAX_SCORE,
    EXCESSIVE_PR_PENALTY_THRESHOLD,
    EXCESSIVE_PR_PENALTY_SLOPE,
    EXCESSIVE_PR_MIN_MULTIPLIER,
    TIERS_AND_COLLATERAL_EFFECTIVE_DATE,
)
from gittensor.utils.github_api_tools import get_pull_request_file_changes
from gittensor.validator.configurations.tier_config import TierConfig, Tier, TIERS

def score_miner_prs(
    miner_eval: MinerEvaluation,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> None:
    """Score all pull requests (merged and open) for a miner."""
    pr_lists = [
        (miner_eval.merged_pull_requests, "merged", "MERGED"),
        (miner_eval.open_pull_requests, "open", "OPEN"),
    ]

    for pr_list, list_name, label in pr_lists:
        if not pr_list:
            bt.logging.info(f"No {list_name} PRs for uid {miner_eval.uid}")
            continue

        bt.logging.info(f"Scoring {len(pr_list)} {list_name} PRs for uid {miner_eval.uid}")

        scored_prs = []
        for n, pr in enumerate(pr_list, start=1):
            bt.logging.info(f"\n[{n}/{len(pr_list)}] - {label} PR #{pr.number} in {pr.repository_full_name}")
            if score_pull_request(pr, miner_eval, master_repositories, programming_languages):
                scored_prs.append(pr)
            else:
                bt.logging.warning(f"Skipping PR #{pr.number} - failed to score (missing tier config or file changes)")

        if list_name == "merged":
            miner_eval.merged_pull_requests = scored_prs
        else:
            miner_eval.open_pull_requests = scored_prs

def score_pull_request(
    pr: PullRequest,
    miner_eval: MinerEvaluation,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> bool:
    """Score a single PR (merged or open). Returns True if scored, False if skipped."""
    pr.repository_tier_configuration = get_tier_config(pr.repository_full_name, master_repositories)
    if not pr.repository_tier_configuration:
        return False

    file_changes = get_pull_request_file_changes(pr.repository_full_name, pr.number, miner_eval.github_pat)
    if not file_changes:
        bt.logging.warning("No file changes found.")
        return False

    pr.set_file_changes(file_changes)
    pr.base_score = calculate_base_score(pr, programming_languages)
    calculate_pr_multipliers(pr, miner_eval, master_repositories)

    if pr.pr_state == PRState.MERGED:
        miner_eval.unique_repos_contributed_to.add(pr.repository_full_name)

    return True


def get_tier_config(repo_full_name: str, master_repositories: Dict[str, Dict]) -> Optional[TierConfig]:
    """Get tier configuration for a repository."""
    tier_str = master_repositories.get(repo_full_name, {}).get("tier")
    tier_config = TIERS.get(Tier(tier_str)) if tier_str else None
    if not tier_config:
        bt.logging.warning(f"{repo_full_name} is not configured to a tier. Skipping...")
    return tier_config


def calculate_base_score(pr: PullRequest, programming_languages: Dict[str, float]) -> float:
    """Calculate base score from tier base + contribution bonus."""
    contribution_score = pr.calculate_score_from_file_changes(programming_languages)

    tier_config: TierConfig = pr.repository_tier_configuration

    bonus_percent = min(1.0, contribution_score / tier_config.contribution_score_for_full_bonus)
    contribution_bonus = round(bonus_percent * tier_config.contribution_score_max_bonus, 2)
    base_score = tier_config.merged_pr_base_score + contribution_bonus

    bt.logging.info(
        f"Base score: {tier_config.merged_pr_base_score} + {contribution_bonus} bonus "
        f"({bonus_percent*100:.0f}% of max {tier_config.contribution_score_max_bonus}) = {base_score:.2f}"
    )

    # TODO: Somehow ensure that this base score can't be earned from a Test only PR, comment only PR, typo fix PR, etc.
    return base_score


def calculate_pr_multipliers(pr: PullRequest, miner_eval: MinerEvaluation, master_repositories: Dict[str, Dict]) -> None:
    """Calculate all multipliers for a PR."""
    is_merged = pr.pr_state == PRState.MERGED
    repo_meta = master_repositories.get(pr.repository_full_name, {})

    pr.repo_weight_multiplier = round(repo_meta.get("weight", 0.01), 2)
    pr.issue_multiplier = round(calculate_issue_multiplier(pr), 2)
    pr.gittensor_tag_multiplier = 1.0 if pr.gittensor_tagged else 0.0

    if is_merged:
        pr.open_pr_spam_multiplier = round(calculate_pr_spam_penalty_multiplier(miner_eval.total_open_prs), 2)
        pr.time_decay_multiplier = round(calculate_time_decay_multiplier(pr), 2)

    else:
        pr.open_pr_spam_multiplier = 1.0
        pr.time_decay_multiplier = 1.0
        pr.credibility_multiplier = 1.0


def count_repository_contributors(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[str, int]:
    """
    Count how many miners contribute to each repository and log statistics.

    Returns:
        Dict[str, int]: Dictionary mapping repository names to contributor counts
    """
    repo_counts: Dict[str, int] = {}

    for evaluation in miner_evaluations.values():
        for repo in evaluation.unique_repos_contributed_to:
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


def finalize_miner_scores(miner_evaluations: Dict[int, MinerEvaluation]) -> None:
    """Finalize all miner scores: apply uniqueness multipliers, calculate totals, and deduct collateral."""
    bt.logging.info("**Finalizing miner scores**")

    repo_counts = count_repository_contributors(miner_evaluations)
    total_contributing_miners = sum(1 for ev in miner_evaluations.values() if ev.unique_repos_contributed_to)

    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        bt.logging.info(f"\n***Finalizing scores for UID {uid}***")

        evaluation.calculate_tier_credibility()

        # Process merged PRs
        for pr in evaluation.merged_pull_requests:
            pr.repository_uniqueness_multiplier = calculate_uniqueness_multiplier(
                pr.repository_full_name, repo_counts, total_contributing_miners
            )

            tier_config = pr.repository_tier_configuration
            tier = evaluation._get_tier_from_config(tier_config)
            credibility = evaluation.tier_credibility.get(tier, 1.0) if tier else 1.0
            pr.credibility_multiplier = round(credibility ** tier_config.credibility_scalar, 2)

            pr.calculate_final_earned_score()
            evaluation.base_total_score += pr.base_score
            evaluation.total_score += pr.earned_score
            evaluation.total_lines_changed += pr.total_lines_scored

        # Process open PRs for collateral
        for pr in evaluation.open_pull_requests:
            pr.collateral_score = calculate_open_pr_collateral_score(pr)
            evaluation.total_collateral_score += pr.collateral_score

        # Apply collateral deduction
        earned_score = evaluation.total_score
        evaluation.total_score = max(0.0, earned_score - evaluation.total_collateral_score)
        evaluation.unique_repos_count = len(evaluation.unique_repos_contributed_to)

        bt.logging.info(
            f"UID {uid}: earned={earned_score:.2f} - collateral={evaluation.total_collateral_score:.2f} = "
            f"final={evaluation.total_score:.2f} ({evaluation.total_merged_prs} merged, {evaluation.total_open_prs} open)"
        )

    bt.logging.info("Finalization complete.")


def calculate_uniqueness_multiplier(repo_full_name: str, repo_counts: Dict[str, int], total_contributing_miners: int) -> float:
    """Calculate repository uniqueness multiplier based on how many miners contribute to a repo."""
    if total_contributing_miners == 0:
        return 1.0
    repo_count = repo_counts.get(repo_full_name, 0)
    uniqueness_score = (total_contributing_miners - repo_count + 1) / total_contributing_miners
    return 1.0 + (uniqueness_score * UNIQUE_PR_BOOST)


def calculate_issue_multiplier(pr: PullRequest) -> float:
    """
    Calculate PR score multiplier based on the first valid linked issue's age.

    Works for both merged PRs (uses issue.closed_at) and open PRs (uses current time).
    Only the first valid issue is scored.

    Returns:
        float: Multiplier between 1.0 and 1.9
    """
    if not pr.issues:
        bt.logging.info(f"PR #{pr.number} - no linked issues")
        return 1.0

    valid_issues = [issue for issue in pr.issues if is_valid_issue(issue, pr)]
    if not valid_issues:
        bt.logging.info(f"PR #{pr.number} - no valid issues")
        return 1.0

    issue = valid_issues[0]
    is_merged = pr.pr_state == PRState.MERGED

    if not issue.created_at:
        bt.logging.info(f"Issue #{issue.number} - No creation date, using default: 0.10")
        return 1.1

    try:
        end_date = issue.closed_at if (is_merged and issue.closed_at) else datetime.now(timezone.utc)
        days_open = (end_date - issue.created_at).days
        normalized = 0.1 + math.sqrt(min(days_open, MAX_ISSUE_AGE_FOR_MAX_SCORE)) / math.sqrt(MAX_ISSUE_AGE_FOR_MAX_SCORE)
        issue_bonus = 0.9 * min(normalized, 1.0)
        bt.logging.info(f"Issue #{issue.number} - Open for {days_open} days | bonus: {issue_bonus:.2f}")
        return 1.0 + issue_bonus
    except (ValueError, AttributeError) as e:
        bt.logging.warning(f"Issue #{issue.number} - Could not calculate age. Using default: 0.10. Exception: {e}")
        return 1.1


def is_valid_issue(issue: Issue, pr: PullRequest) -> bool:
    """Check if issue is valid for bonus calculation (works for both merged and open PRs)."""
    is_merged = pr.pr_state == PRState.MERGED

    # Common checks (both merged and open)
    if not issue.author_login:
        bt.logging.warning(f"Skipping issue #{issue.number} - missing author information")
        return False

    if issue.author_login == pr.author_login:
        bt.logging.warning(f"Skipping issue #{issue.number} - same author as PR (self-created issue)")
        return False

    if issue.created_at and pr.created_at and issue.created_at > pr.created_at:
        bt.logging.warning(f"Skipping issue #{issue.number} - created after PR")
        return False

    # Merged-only checks
    if is_merged:
        if pr.last_edited_at and pr.last_edited_at > pr.merged_at:
            bt.logging.warning(f"Skipping issue #{issue.number} - PR edited after merge")
            return False

        if issue.state and issue.state != 'CLOSED':
            bt.logging.warning(f"Skipping issue #{issue.number} - not CLOSED (state: {issue.state})")
            return False

        if issue.closed_at and pr.merged_at:
            days_diff = abs((issue.closed_at - pr.merged_at).total_seconds()) / SECONDS_PER_DAY
            if days_diff > MAX_ISSUE_CLOSE_WINDOW_DAYS:
                bt.logging.warning(f"Skipping issue #{issue.number} - closed {days_diff:.1f}d from merge (max: {MAX_ISSUE_CLOSE_WINDOW_DAYS})")
                return False

    return True


# =============================================================================
# Collateral System Functions
# =============================================================================

def calculate_open_pr_collateral_score(pr: PullRequest) -> float:
    """
    Calculate collateral score for an open PR.

    Collateral = base_score * applicable_multipliers * DEFAULT_COLLATERAL_PERCENT

    Applicable multipliers: repo_weight, issue, gittensor_tag
    NOT applicable: time_decay (merge-based), credibility_multiplier (merge-based),
                    uniqueness (cross-miner), open_pr_spam (not for collateral)
    """
    from math import prod

    if pr.created_at <= TIERS_AND_COLLATERAL_EFFECTIVE_DATE:
        return 0.0

    multipliers = {
        "repo_weight": pr.repo_weight_multiplier,
        "issue": pr.issue_multiplier,
        "gittensor_tag": pr.gittensor_tag_multiplier,
    }

    potential_score = pr.base_score * prod(multipliers.values())
    collateral_percent = pr.repository_tier_configuration.open_pr_collateral_percentage
    collateral_score = potential_score * collateral_percent

    mult_str = " | ".join([f"{k}: {v:.2f}" for k, v in multipliers.items()])
    bt.logging.info(
        f"OPEN PR #{pr.number} | base: {pr.base_score:.2f} | {mult_str} | "
        f"potential: {potential_score:.2f} | collateral ({collateral_percent*100:.0f}%): {collateral_score:.2f}"
    )

    return collateral_score


