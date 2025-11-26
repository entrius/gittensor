# The MIT License (MIT)
# Copyright © 2025 Entrius

import math
from datetime import datetime, timezone
from typing import Dict, List

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation, PullRequest
from gittensor.constants import (
    SECONDS_PER_DAY,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    MAX_ISSUES_SCORED_IN_SINGLE_PR,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
    UNIQUE_PR_BOOST,
    MAX_ISSUE_AGE_FOR_MAX_SCORE,
)
from gittensor.validator.evaluation.inspections import apply_typo_detection_penalties
from gittensor.utils.github_api_tools import get_pull_request_file_changes


def score_pull_requests(
    uid: int,
    miner_eval: MinerEvaluation,
    valid_raw_prs: list,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> MinerEvaluation:
    """
    Score pull requests and populate MinerEvaluation object.
    Fetches file changes, calculates scores, applies repo weights and issue bonuses.

    Args:
        uid (int): Miner UID for logging
        miner_eval (MinerEvaluation): MinerEvaluation object to populate
        valid_raw_prs (list): List of raw PR data from GraphQL API
        master_repositories (Dict[str, Dict]): The incentivized repositories and their metadata (weight, inactiveAt)
        programming_languages (Dict[str, float]): The programming languages and their weights

    Returns:
        MinerEvaluation: The populated evaluation object
    """
    valid_prs = [
        PullRequest.from_graphql_response(raw_pr, uid, miner_eval.hotkey, miner_eval.github_id)
        for raw_pr in valid_raw_prs
    ]

    if not valid_prs:
        bt.logging.info(f"No valid PRs found for miner {uid}: setting default score of 0.")
        return miner_eval

    total_prs = len(valid_prs)
    bt.logging.info(f"Scoring {total_prs} PRs for miner {uid}")

    for n, pr in enumerate(valid_prs, start=1):
        # if repo not in master list, default to .01 (shouldn't happen bc already filtered in github graphql method)
        repo_weight = master_repositories.get(pr.repository_full_name, {}).get("weight", 0.01)

        bt.logging.info(f"[{n}/{total_prs}] - Scoring PR #{pr.number} in {pr.repository_full_name} (weight: {repo_weight})")

        file_changes = get_pull_request_file_changes(pr.repository_full_name, pr.number, miner_eval.github_pat)
        if not file_changes:
            bt.logging.warning("No file changes found for this PR.")
            continue

        pr.set_file_changes(file_changes)
        pr.set_base_score(pr.calculate_score_from_file_changes(programming_languages))

        apply_issue_resolvement_bonus(pr)

        apply_typo_detection_penalties(pr, uid)

        score_before = pr.base_score
        final_score = score_before * float(repo_weight)
        bt.logging.info(f"Applying repo weight: {score_before:.2f} x {repo_weight} -> {final_score:.2f}")
        
        pr.set_base_score(final_score)
        pr.set_earned_score(final_score)

        miner_eval.add_pull_request(pr)

    return miner_eval


def count_repository_contributors(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[str, int]:
    """
    Count how many miners contribute to each repository and log statistics.

    Returns:
        Dict[str, int]: Dictionary mapping repository names to contributor counts
    """
    repo_counts: Dict[str, int] = {}
    
    for evaluation in miner_evaluations.values():
        for repo in evaluation.get_unique_repositories() or []:
            repo_counts[repo] = repo_counts.get(repo, 0) + 1


    if repo_counts:
        bt.logging.info(f"Repository contribution counts: {len(repo_counts)} total repositories")
        for repo, count in sorted(repo_counts.items(), key=lambda x: -x[1]):
            bt.logging.info(f"{repo}: {count}")

    return repo_counts


def apply_repository_uniqueness_boost(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """Boost miners who contribute to repositories that fewer other miners work on."""
    repo_counts = count_repository_contributors(miner_evaluations)

    if not repo_counts:
        bt.logging.info("No repository contributions found, skipping uniqueness boost")
        return

    total_miners = sum(1 for ev in miner_evaluations.values() if ev.get_unique_repositories())

    for uid, evaluation in miner_evaluations.items():
        repos = evaluation.get_unique_repositories() if evaluation else None

        for repo in repos:
            uniqueness_score = (total_miners - repo_counts[repo] + 1) / total_miners
            boost = 1.0 + (uniqueness_score * UNIQUE_PR_BOOST)

            for pr in evaluation.pull_requests:
                if pr.repository_full_name == repo:
                    original = pr.earned_score
                    pr.set_earned_score(original * boost)
                    bt.logging.info(f"Unique repo boost for {repo}: {original:.2f} -> {pr.earned_score:.2f}")

    bt.logging.info(f"Completed applying repository uniqueness boost for {total_miners} total contributing miners.")


def apply_time_decay_for_repository_contributions(miner_evaluations: Dict[int, MinerEvaluation]) -> None:
    """Apply sigmoid curve time decay to PR scores based on merge date."""
    
    bt.logging.info("Applying sigmoid curve time decay to PRs")
    now = datetime.now(timezone.utc)
    total_prs_modified = 0
    
    for uid, evaluation in miner_evaluations.items():
        if not evaluation or not evaluation.pull_requests:
            continue

        for pr in evaluation.pull_requests:
            days_since_merge = (now - pr.merged_at).total_seconds() / SECONDS_PER_DAY

            # Produces a scalar between 0 and 1
            sigmoid = 1 / (1 + math.exp(TIME_DECAY_SIGMOID_STEEPNESS_SCALAR * (days_since_merge - TIME_DECAY_SIGMOID_MIDPOINT)))
            decay = max(sigmoid, TIME_DECAY_MIN_MULTIPLIER) 

            original_score = pr.earned_score
            pr.set_earned_score(original_score * decay)
            total_prs_modified += 1

            bt.logging.info(
                f"UID {uid} PR (merged {days_since_merge:.1f}d ago): "
                f"decay={decay:.2f}, score {original_score:.2f} -> {pr.earned_score:.2f}"
            )

    bt.logging.info(f"Applied time decay to {total_prs_modified} PRs.")


def apply_issue_resolvement_bonus(pr: PullRequest) -> None:
    """Apply bonus to PR scores for resolved issues."""
    if not pr.issues:
        bt.logging.info(f"PR #{pr.number} in {pr.repository_full_name} resolved no issue.")
        return

    valid_issues = [issue for issue in pr.issues if _is_valid_issue(issue, pr)]

    if not valid_issues:
        bt.logging.info(f"PR #{pr.number} in {pr.repository_full_name}: {pr.base_score:.2f} × 1.0 = {pr.base_score:.2f} (no valid issues)")
        return

    issue_multiplier = calculate_issue_multiplier(valid_issues)
    new_pr_score = round(issue_multiplier * pr.base_score, 2)

    bt.logging.info(
        f"PR #{pr.number} in {pr.repository_full_name} earned score: {pr.base_score:.2f} × issue multiplier: {issue_multiplier:.2f} = {new_pr_score:.2f} ({len(valid_issues)}/{len(pr.issues)} valid issues)"
    )

    pr.set_base_score(new_pr_score)
    return

def calculate_issue_multiplier(issues: List[Issue]) -> float:
    """
    Calculate score multiplier based on age and number of resolved issues.

    - Base multiplier: 1.0 (no bonus)
    - Each issue adds 0.09-0.90 to multiplier based on age (sqrt scaling)
    - Maximum 3 issues counted (max multiplier: 3.7)
    - 100% of issue bonus earned when issue has been open for MAX_ISSUE_AGE_FOR_MAX_SCORE+ days

    Returns:
        float: Multiplier between 1.0 and 3.7
    """
    num_issues = min(len(issues), MAX_ISSUES_SCORED_IN_SINGLE_PR)
    bt.logging.info(f"Calculating issue bonus for PR with {len(issues)} issues (counting {num_issues})")

    total_issue_score = 0.0
    for i in range(num_issues):
        issue = issues[i]
        issue_num = getattr(issue, 'number', i + 1)

        if not (issue.created_at and issue.closed_at):
            bt.logging.info(f"Issue #{issue_num}: no date info, using default score: 0.10")
            total_issue_score += 0.1
            continue

        try:
            days_open = (issue.closed_at - issue.created_at).days
            normalized = 0.1 + math.sqrt(min(days_open, MAX_ISSUE_AGE_FOR_MAX_SCORE)) / math.sqrt(MAX_ISSUE_AGE_FOR_MAX_SCORE)
            score = 0.9 * min(normalized, 1.0)
            bt.logging.info(f"Issue #{issue_num}: open for {days_open} days, score: {score:.2f}")
            total_issue_score += score
            
        except (ValueError, AttributeError) as e:
            bt.logging.warning(f"Could not parse issue dates: {e}")
            total_issue_score += 0.1

    final_multiplier = 1.0 + total_issue_score
    bt.logging.info(f"Issue score calculation complete - total: {total_issue_score:.2f}, multiplier: {final_multiplier:.2f}")

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