# The MIT License (MIT)
# Copyright © 2025 Entrius

import math
from datetime import datetime, timezone
from typing import Dict

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation, PullRequest
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
)
from gittensor.utils.github_api_tools import get_pull_request_file_changes

def score_pull_requests(
    miner_eval: MinerEvaluation,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> None:
    """
    Score pull requests and populate MinerEvaluation object.
    Fetches file changes, calculates scores, applies repo weights and issue bonuses.

    Args:
        miner_eval (MinerEvaluation): MinerEvaluation object to populate
        master_repositories (Dict[str, Dict]): The incentivized repositories and their metadata (weight, inactiveAt)
        programming_languages (Dict[str, float]): The programming languages and their weights
    """
    if not miner_eval.pull_requests:
        bt.logging.info(f"No valid PRs found for uid {miner_eval.uid}")
        return

    total_prs = len(miner_eval.pull_requests)
    bt.logging.info(f"Scoring {total_prs} PRs for uid {miner_eval.uid}")

    for n, pr in enumerate(miner_eval.pull_requests, start=1):
        bt.logging.info(f"\n[{n}/{total_prs}] - Scoring PR #{pr.number} in {pr.repository_full_name}")

        file_changes = get_pull_request_file_changes(pr.repository_full_name, pr.number, miner_eval.github_pat)

        if not file_changes:
            bt.logging.warning("No file changes found for this PR.")
            continue

        pr.set_file_changes(file_changes)

        repo_weight = master_repositories.get(pr.repository_full_name, {}).get("weight", 0.01)
        file_change_score = pr.calculate_score_from_file_changes(programming_languages)
        issue_multiplier = calculate_issue_multiplier(pr)
        open_pr_spam_multiplier = calculate_pr_spam_penalty_multiplier(miner_eval.total_open_prs)
        time_decay_multiplier = calculate_time_decay_multiplier(pr)
        gittensor_tag_multiplier = GITTENSOR_TAGLINE_BOOST if pr.gittensor_tagged else 1.0
        merge_success_multiplier = calculate_merge_success_multiplier(miner_eval)

        pr.repo_weight_multiplier = round(repo_weight, 2)
        pr.base_score = round(file_change_score, 2)
        pr.issue_multiplier = round(issue_multiplier, 2)
        pr.open_pr_spam_multiplier = round(open_pr_spam_multiplier, 2)
        pr.time_decay_multiplier = round(time_decay_multiplier, 2)
        pr.gittensor_tag_multiplier = round(gittensor_tag_multiplier, 2)
        pr.merge_success_multiplier = round(merge_success_multiplier, 2)

        miner_eval.unique_repos_contributed_to.add(pr.repository_full_name)


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


def calculate_merge_success_multiplier(miner_eval: MinerEvaluation) -> float:
    """Calculate multiplier based on PR merge success ratio."""
    if (miner_eval.total_merged_prs == 0 and miner_eval.total_closed_prs == 0):
        return 1.0
        
    total_prs = miner_eval.total_merged_prs + miner_eval.total_closed_prs
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
    """Apply repository uniqueness multipliers and calculate final scores in a single pass."""
    bt.logging.info("**Applying uniqueness multipliers and finalizing scores**")

    repo_counts = count_repository_contributors(miner_evaluations)

    if not repo_counts:
        bt.logging.info("No repository contributions found, skipping uniqueness multipliers")
        return

    total_contributing_miners = sum(1 for ev in miner_evaluations.values() if ev.unique_repos_contributed_to)
    total_prs = 0

    for uid, evaluation in miner_evaluations.items():
        if not evaluation or not evaluation.pull_requests:
            continue

        total_prs += len(evaluation.pull_requests)
        bt.logging.info(f"\n***Totaling scores for uid {uid}***")

        for pr in evaluation.pull_requests:
            # Apply uniqueness multiplier (cross-miner dependent)
            uniqueness_score = (total_contributing_miners - repo_counts[pr.repository_full_name] + 1) / total_contributing_miners
            uniqueness_multiplier = 1.0 + (uniqueness_score * UNIQUE_PR_BOOST)
            pr.repository_uniqueness_multiplier = uniqueness_multiplier

            # Calculate final earned score now that all multipliers are set
            pr.calculate_final_earned_score()

            evaluation.base_total_score += pr.base_score
            evaluation.total_score += pr.earned_score
            evaluation.total_lines_changed += pr.total_lines_scored

        evaluation.unique_repos_count = len(evaluation.unique_repos_contributed_to)
        merge_success_rate = calculate_merge_success_multiplier(evaluation)

        bt.logging.info(f"Final evaluation for UID {uid}:")
        bt.logging.info(f"  - Total Score: {evaluation.total_score:.2f}")
        bt.logging.info(f"  - Total Valid PRs: {evaluation.total_prs}")
        bt.logging.info(f"  - Total Open PRs: {evaluation.total_open_prs}")
        bt.logging.info(f"  - PR Merge Success Rate: {evaluation.total_merged_prs}/{evaluation.total_merged_prs + evaluation.total_closed_prs} ({merge_success_rate:.2f}")
        bt.logging.info(f"  - Total Lines Changed (& Scored): {evaluation.total_lines_changed}")
        bt.logging.info(f"  - Unique Repositories Contributed To: {evaluation.unique_repos_contributed_to}")

    bt.logging.info(f"Completed uniqueness multipliers and score finalization for {total_contributing_miners} miners across {total_prs} PRs.")


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

    num_issues = min(len(pr.issues), MAX_ISSUES_SCORED_IN_SINGLE_PR)
    bt.logging.info(f"Calculating issue multiplier for PR #{pr.number} with {num_issues} issues")

    total_issue_multiplier = 0.0
    for i in range(num_issues):
        issue = pr.issues[i]
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