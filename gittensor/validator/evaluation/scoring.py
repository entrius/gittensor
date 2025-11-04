# The MIT License (MIT)
# Copyright © 2025 Entrius

import math
from typing import Dict, List

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation, PullRequest
from gittensor.constants import MAX_ISSUES_SCORED_IN_SINGLE_PR, PARETO_DISTRIBUTION_ALPHA_VALUE, UNIQUE_PR_BOOST
from gittensor.utils.utils import mask_secret


def normalize_rewards_with_pareto(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """
    Pareto normalization: Apply Pareto curve to raw scores, then use linear normalization

    The transformation: score_new = score^(1/alpha)
    - alpha < 1.0: Makes curve steeper (amplifies differences)
    - alpha > 1.0: Makes curve flatter (compresses differences)
    - alpha = 1.0: No change (linear)

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Dict of uid -> MinerEvaluation

    Returns:
        Dict[int, float]: Pareto-curved scores that sum to 1.0, Dict of uid ->  score.

    Notes:
        PARETO_DISTRIBUTION_ALPHA_VALUE: Pareto curve parameter
            0.5 = very steep (2x becomes 4x)
            0.8 = moderately steep (2x becomes ~2.4x)
            1.0 = no change
            1.5 = flatter (2x becomes ~1.7x)
    """

    rewards: Dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        evaluation.calculate_total_score_and_total_contributions()
        rewards[uid] = evaluation.total_score
        bt.logging.info(f"Final reward for uid {uid}: {rewards[uid]:.4f}")

    if not rewards:
        bt.logging.warning("No rewards provided for Pareto normalization")
        return {}

    bt.logging.info(f"Applying Pareto curve transformation with α={PARETO_DISTRIBUTION_ALPHA_VALUE}")

    # Filter out zero scores
    non_zero_scores = {uid: score for uid, score in rewards.items() if score > 0}

    if not non_zero_scores:
        bt.logging.info("All scores are zero, passing to linear normalization")
        return normalize_rewards_linear(rewards)

    # Apply Pareto curve directly to raw scores
    pareto_scores = {}
    for uid, score in rewards.items():
        if score > 0:
            pareto_scores[uid] = score ** (1.0 / PARETO_DISTRIBUTION_ALPHA_VALUE)
        else:
            pareto_scores[uid] = 0.0

    bt.logging.info(f"Pareto curve parameter α: {PARETO_DISTRIBUTION_ALPHA_VALUE}")

    # Use linear normalization as child function for final step
    return normalize_rewards_linear(pareto_scores)


def normalize_rewards_linear(rewards: Dict[int, float]) -> Dict[int, float]:
    """
    Simple linear normalization: normalize raw scores to sum=1.0 (preserves exact ratios)

    Args:
        rewards (Dict[int, float]): Dict mapping miner UIDs to their raw scores

    Returns:
        Dict[int, float]: Linear normalized scores that sum to 1.0
    """
    if not rewards:
        bt.logging.warning("No rewards provided for normalization")
        return {}

    non_zero_scores = {uid: score for uid, score in rewards.items() if score > 0}

    if not non_zero_scores:
        bt.logging.info("All scores are zero, returning original scores")
        return rewards

    # Simply normalize to sum=1.0 (preserves exact original ratios)
    total_score = sum(rewards.values())
    normalized_scores = {}
    for uid, score in rewards.items():
        normalized_scores[uid] = score / total_score if total_score > 0 else 0.0

    bt.logging.info("Linear normalization complete:")
    bt.logging.info(f"  - Original score sum: {total_score:.6f}")
    bt.logging.info(f"  - Normalized score sum: {sum(normalized_scores.values()):.6f}")
    bt.logging.info(f"  - Non-zero miners: {len(non_zero_scores)}/{len(rewards)}")

    return normalized_scores


def count_repository_contributors(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[str, int]:
    """
    Count how many miners contribute to each repository and log statistics.

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Dictionary of miner evaluations

    Returns:
        Dict[str, int]: Dictionary mapping repository names to contributor counts
    """
    repo_contributor_counts = {}

    for evaluation in miner_evaluations.values():
        if evaluation.get_unique_repositories():
            for repo in evaluation.get_unique_repositories():
                repo_contributor_counts[repo] = repo_contributor_counts.get(repo, 0) + 1

    # Log repository statistics
    if repo_contributor_counts:
        bt.logging.info(f"Repository contribution counts: {len(repo_contributor_counts)} total repositories")

        # Log each repository with its contributor count
        sorted_repos = sorted(repo_contributor_counts.items(), key=lambda x: x[1], reverse=True)
        for repo, count in sorted_repos:
            bt.logging.info(f"{repo}: {count}")

    return repo_contributor_counts


def apply_repository_uniqueness_boost(
    miner_evaluations: Dict[int, MinerEvaluation]
) -> Dict[int, float]:
    """
    Boost miners who contribute to repositories that fewer other miners work on.
    More unique/rare repository contributions get higher boosts.

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Evaluation data containing repository contribution info

    Note:
        This function modifies the `miner_evaluations` dictionary in-place to apply the score boost per PR.
    """

    # Create repository_name -> contributor count dictionary
    repo_contributor_counts = count_repository_contributors(miner_evaluations)

    # Skip boost if no repository contributions found
    if not repo_contributor_counts:
        bt.logging.info("No repository contributions found, skipping uniqueness boost")
        return

    # Calculate total number of miners for normalization
    total_miners = len([uid for uid, eval in miner_evaluations.items() if eval.get_unique_repositories()])

    for uid, evaluation in miner_evaluations.items():
        if not evaluation or not evaluation.get_unique_repositories():
            continue

        bt.logging.info("Applying repository uniqueness boost for uid {uid}")
        for repo in evaluation.get_unique_repositories():
            contributors_count = repo_contributor_counts[repo]

            # Uniqueness score that approaches 0 as contribution count increases
            uniqueness_score = (total_miners - contributors_count + 1) / total_miners
            boost_multiplier = 1.0 + (uniqueness_score * UNIQUE_PR_BOOST)

            repo_prs: List[PullRequest] = [pr for pr in evaluation.pull_requests if pr.repository_full_name == repo]
            
            for pr in repo_prs:
                original_score = pr.earned_score
                pr.earned_score = pr.earned_score * boost_multiplier
                
                bt.logging.info(
                    f"Applying unique repo boost to PR's earned score for uid {uid}'s contribution to {pr.repository_full_name}: "
                    f"{original_score:.4f} -> {pr.earned_score:.4f}"
                )


def apply_issue_resolvement_bonus(pr: PullRequest, base_pr_score: float) -> float:
    """
    Applies a bonus to pull request scores for pull requests which solve issues.

    Args:
        pr (PullRequest): The Pull Request that contains the potential issues

    Returns:
        float: The newly calculated PR score with the issue bonus applied.
    """

    if not pr.issues:
        bt.logging.info(
            f"PR #{mask_secret(pr.number)} in {mask_secret(pr.repository_full_name)} earned score: {base_pr_score:.5f} × issue multiplier: 1.0 = {base_pr_score:.5f}"
        )
        return base_pr_score

    issue_multiplier = calculate_issue_multiplier(pr.issues)
    new_pr_score = round(issue_multiplier * base_pr_score, 2)

    bt.logging.info(
        f"PR #{mask_secret(pr.number)} in {mask_secret(pr.repository_full_name)} earned score: {base_pr_score:.5f} × issue multiplier: {issue_multiplier:.3f} = {new_pr_score:.5f}"
    )

    return new_pr_score


def calculate_issue_multiplier(issues: List[Issue]) -> float:
    """
    Calculate score multiplier based on age and number of resolved issues.

    - Base multiplier: 1.0 (no bonus)
    - Each issue adds 0.09-0.90 to multiplier based on age
    - Maximum 3 issues counted (max multiplier: 3.7)
    - Older issues worth more, with gradual scaling
    - 100% of issue bonus earned when issue has been open for 45+ days

    Args:
        issues (List[Issue]): List of resolved issues

    Returns:
        float: Multiplier between 1.0 and 3.7
    """
    base_multiplier = 1.0
    num_issues = min(len(issues), MAX_ISSUES_SCORED_IN_SINGLE_PR)

    bt.logging.info(f"Calculating issue bonus for PR with {len(issues)} issues (counting {num_issues})")

    total_issue_score = 0.0

    for i in range(num_issues):
        issue = issues[i]
        issue_score = calculate_issue_multiplier_score(issue, i)
        total_issue_score += issue_score

    final_multiplier = base_multiplier + total_issue_score

    bt.logging.info(
        f"Issue score calculation complete - "
        f"total issue score: {total_issue_score:.3f}, "
        f"final multiplier: {final_multiplier:.3f}"
    )

    return final_multiplier


def calculate_issue_multiplier_score(issue: Issue, index: int) -> float:
    """
    Calculate score for a single issue based on its age.

    Uses square root scaling for generous early rewards:
    - 2 days:  ~19% of max bonus (0.17)
    - 5 days:  ~26% of max bonus (0.24)
    - 10 days: ~38% of max bonus (0.34)
    - 20 days: ~57% of max bonus (0.51)
    - 30 days: ~72% of max bonus (0.65)
    - 45 days: 100% of max bonus (0.90)

    Args:
        issue (Issue): Issue to score
        index (int): Issue index for logging

    Returns:
        float: Score between 0.09 and 0.90
    """

    # Default score for issues without date info
    if not (hasattr(issue, 'created_at') and hasattr(issue, 'closed_at') and issue.created_at and issue.closed_at):
        issue_score = 0.1
        bt.logging.info(
            f"Issue #{getattr(issue, 'number', index+1)}: no date info available, using default score: {issue_score:.3f}"
        )
        return issue_score

    try:
        days_open = (issue.closed_at - issue.created_at).days
        normalized_score = 0.1 + math.sqrt(min(days_open, 45)) / math.sqrt(45)
        issue_score = 0.9 * min(normalized_score, 1.0)

        bt.logging.info(
            f"Issue #{getattr(issue, 'number', index+1)}: open for {days_open} days, score: {issue_score:.3f}"
        )

    except (ValueError, AttributeError) as e:
        bt.logging.warning(f"Could not parse issue dates: {e}")
        issue_score = 0.1

    return issue_score
