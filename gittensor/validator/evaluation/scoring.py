# The MIT License (MIT)
# Copyright © 2025 Entrius

import math
from datetime import datetime, timezone
from typing import Dict, Optional

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation, PrScoringResult, PRState, PullRequest
from gittensor.constants import (
    DEFAULT_MERGED_PR_BASE_SCORE,
    EXCESSIVE_PR_MIN_MULTIPLIER,
    EXCESSIVE_PR_PENALTY_SLOPE,
    EXCESSIVE_PR_PENALTY_THRESHOLD,
    MAINTAINER_ASSOCIATIONS,
    MAINTAINER_ISSUE_BONUS,
    MAX_CODE_DENSITY_MULTIPLIER,
    MAX_ISSUE_AGE_BONUS,
    MAX_ISSUE_AGE_FOR_MAX_SCORE,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
    UNIQUE_PR_BOOST,
)
from gittensor.utils.github_api_tools import (
    FileContentPair,
    fetch_file_contents_with_base,
    get_pull_request_file_changes,
)
from gittensor.validator.configurations.tier_config import (
    TIERS,
    TIERS_ORDER,
    Tier,
    TierConfig,
    get_tier_from_config,
)
from gittensor.validator.evaluation.credibility import (
    calculate_credibility_per_tier,
    calculate_tier_stats,
    is_tier_unlocked,
)
from gittensor.validator.utils.load_weights import LanguageConfig, RepositoryConfig, TokenConfig
from gittensor.validator.utils.tree_sitter_scoring import calculate_token_score_from_file_changes


def score_miner_prs(
    miner_eval: MinerEvaluation,
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
) -> None:
    """Score all pull requests for a miner."""

    bt.logging.info('')
    bt.logging.info('-' * 50)
    bt.logging.info(
        f'Scoring UID {miner_eval.uid}: {len(miner_eval.merged_pull_requests)} merged | {len(miner_eval.open_pull_requests)} open | {len(miner_eval.closed_pull_requests)} closed'
    )
    bt.logging.info('-' * 50)

    pr_groups = [
        ('MERGED', miner_eval.merged_pull_requests),
        ('OPEN', miner_eval.open_pull_requests),
        ('CLOSED', miner_eval.closed_pull_requests),
    ]

    for label, prs in pr_groups:
        for i, pr in enumerate(prs, start=1):
            bt.logging.info(f'\n[{i}/{len(prs)}] {label} PR #{pr.number} in {pr.repository_full_name}')
            score_pull_request(pr, miner_eval, master_repositories, programming_languages, token_config)


def score_pull_request(
    pr: PullRequest,
    miner_eval: MinerEvaluation,
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
) -> None:
    """Scores a single PR and assigns the PullRequest object tier config & other fields (low value, etc)."""

    pr.repository_tier_configuration = get_tier_config(pr.repository_full_name, master_repositories)
    if not pr.repository_tier_configuration:
        bt.logging.warning('No repository configuration found.')
        return

    # Only fetch file changes from GitHub if not already loaded (they are preloaded for testing only)
    if not pr.file_changes:
        file_changes = get_pull_request_file_changes(pr.repository_full_name, pr.number, miner_eval.github_pat)
        if not file_changes:
            bt.logging.warning('No file changes found.')
            return
        pr.set_file_changes(file_changes)

    # Fetch full file contents for token-based scoring
    file_contents = fetch_file_contents_for_pr(pr, miner_eval.github_pat)

    pr.base_score = calculate_base_score(pr, programming_languages, token_config, file_contents)
    calculate_pr_multipliers(pr, miner_eval, master_repositories)

    if pr.pr_state == PRState.MERGED and not pr.low_value_pr:
        miner_eval.unique_repos_contributed_to.add(pr.repository_full_name)


def fetch_file_contents_for_pr(pr: PullRequest, github_pat: str) -> Dict[str, FileContentPair]:
    """Fetch both base and head file contents for all files in a PR using GraphQL batch fetch.

    Returns:
        Dict mapping filename to FileContentPair(old_content, new_content)
        - old_content: File content before the PR (None for new files)
        - new_content: File content after the PR (None for deleted files)
    """
    if not pr.file_changes or not pr.head_ref_oid or not pr.base_ref_oid:
        return {}

    # Extract owner and repo name
    parts = pr.repository_full_name.split('/')
    if len(parts) != 2:
        bt.logging.warning(f'Invalid repository name format: {pr.repository_full_name}')
        return {}

    owner, repo_name = parts

    return fetch_file_contents_with_base(
        owner, repo_name, pr.base_ref_oid, pr.head_ref_oid, pr.file_changes, github_pat
    )


def get_tier_config(repo_full_name: str, master_repositories: Dict[str, RepositoryConfig]) -> Optional[TierConfig]:
    """Get tier configuration for a repository."""
    repo_config = master_repositories.get(repo_full_name)
    if not repo_config:
        return None

    tier_config = TIERS.get(repo_config.tier) if repo_config.tier else None
    if not tier_config:
        bt.logging.warning(f'{repo_full_name} is not configured to a tier. Skipping...')
    return tier_config


def calculate_base_score(
    pr: PullRequest,
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    file_contents: Dict[str, FileContentPair],
) -> float:
    """Calculate base score using code density scaling + contribution bonus."""
    scoring_result: PrScoringResult = calculate_token_score_from_file_changes(
        pr.file_changes,
        file_contents,
        token_config,
        programming_languages,
    )

    pr.total_nodes_scored = scoring_result.total_nodes_scored
    if scoring_result.score_breakdown:
        pr.token_score = scoring_result.score_breakdown.total_score
        pr.structural_count = scoring_result.score_breakdown.structural_count
        pr.structural_score = scoring_result.score_breakdown.structural_score
        pr.leaf_count = scoring_result.score_breakdown.leaf_count
        pr.leaf_score = scoring_result.score_breakdown.leaf_score

    # Calculate total lines changed across all files
    total_lines = sum(f.total_lines for f in scoring_result.file_results)

    # Check minimum token score threshold for base score. PRs below threshold get 0 base score
    if pr.token_score < MIN_TOKEN_SCORE_FOR_BASE_SCORE:
        code_density = 0.0
        initial_base_score = 0.0
    elif total_lines > 0:
        # Calculate code density (token_score / total_lines), capped
        code_density = min(pr.token_score / total_lines, MAX_CODE_DENSITY_MULTIPLIER)
        initial_base_score = DEFAULT_MERGED_PR_BASE_SCORE * code_density
    else:
        code_density = 0.0
        initial_base_score = 0.0

    # Calculate contribution bonus, capped
    tier_config: TierConfig = pr.repository_tier_configuration
    bonus_percent = min(1.0, scoring_result.total_score / tier_config.contribution_score_for_full_bonus)
    contribution_bonus = round(bonus_percent * tier_config.contribution_score_max_bonus, 2)

    # Final base score = density-scaled base + contribution bonus
    base_score = round(initial_base_score + contribution_bonus, 2)

    # Log with note if below token threshold
    threshold_note = (
        f' [below {MIN_TOKEN_SCORE_FOR_BASE_SCORE} token threshold]'
        if pr.token_score < MIN_TOKEN_SCORE_FOR_BASE_SCORE
        else ''
    )
    bt.logging.info(
        f'Base score: {initial_base_score:.2f} (density {code_density:.2f}){threshold_note} + {contribution_bonus} bonus '
        f'({bonus_percent * 100:.0f}% of max {tier_config.contribution_score_max_bonus}) = {base_score:.2f}'
    )

    return base_score


def calculate_pr_multipliers(
    pr: PullRequest, miner_eval: MinerEvaluation, master_repositories: Dict[str, RepositoryConfig]
) -> None:
    """Calculate all multipliers for a PR."""
    is_merged = pr.pr_state == PRState.MERGED
    repo_config = master_repositories.get(pr.repository_full_name)

    pr.repo_weight_multiplier = round(repo_config.weight if repo_config else 0.01, 2)
    pr.issue_multiplier = round(calculate_issue_multiplier(pr), 2)

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
        bt.logging.info(f'Repository contribution counts: {len(repo_counts)} total repositories')
        for repo, count in sorted(repo_counts.items(), key=lambda x: -x[1]):
            bt.logging.info(f'{repo}: {count}')

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
    bt.logging.info('**Finalizing miner scores**')

    repo_counts = count_repository_contributors(miner_evaluations)
    total_contributing_miners = sum(1 for ev in miner_evaluations.values() if ev.unique_repos_contributed_to)

    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        bt.logging.info('')
        bt.logging.info('=' * 50)
        bt.logging.info(f'UID {uid}')
        bt.logging.info('=' * 50)

        evaluation.credibility_by_tier = calculate_credibility_per_tier(
            evaluation.merged_pull_requests, evaluation.closed_pull_requests
        )

        # Process merged PRs
        for pr in evaluation.merged_pull_requests:
            # Skip over low value PRs
            if pr.low_value_pr:
                continue

            pr.repository_uniqueness_multiplier = calculate_uniqueness_multiplier(
                pr.repository_full_name, repo_counts, total_contributing_miners
            )

            # Apply tier level credibility^k to each PRs score
            tier_config = pr.repository_tier_configuration
            tier = get_tier_from_config(tier_config)
            credibility = evaluation.credibility_by_tier.get(tier, 1.0) if tier else 1.0
            pr.raw_credibility = credibility
            pr.credibility_scalar = tier_config.credibility_scalar
            pr.credibility_multiplier = round(credibility**tier_config.credibility_scalar, 2)

            pr.calculate_final_earned_score()
            evaluation.base_total_score += pr.base_score
            evaluation.total_score += pr.earned_score
            evaluation.total_nodes_scored += pr.total_nodes_scored

            # Aggregate token scoring breakdown
            evaluation.total_token_score += pr.token_score
            evaluation.total_structural_count += pr.structural_count
            evaluation.total_structural_score += pr.structural_score
            evaluation.total_leaf_count += pr.leaf_count
            evaluation.total_leaf_score += pr.leaf_score

        # Process open PRs for collateral
        for pr in evaluation.open_pull_requests:
            pr.collateral_score = calculate_open_pr_collateral_score(pr)
            evaluation.total_collateral_score += pr.collateral_score

        # Apply collateral deduction
        earned_score = evaluation.total_score
        evaluation.total_score = max(0.0, earned_score - evaluation.total_collateral_score)
        evaluation.unique_repos_count = len(evaluation.unique_repos_contributed_to)

        # Calculate tier stats one more time now that scoring is fully applied (for logging + dashboard).
        # This also calculates qualified_unique_repo_count per tier.
        tier_stats = calculate_tier_stats(
            merged_prs=evaluation.merged_pull_requests,
            closed_prs=evaluation.closed_pull_requests,
            open_prs=evaluation.open_pull_requests,
            include_scoring_details=True,
        )

        # Determine miner's current tier based on what tiers they've unlocked
        for tier in TIERS.keys():
            evaluation.stats_by_tier[tier] = tier_stats[tier]
            if is_tier_unlocked(tier, tier_stats):
                evaluation.current_tier = tier

        # Set overall qualified unique repos count (Bronze threshold is lowest, so use that for overall count)
        evaluation.qualified_unique_repos_count = tier_stats[Tier.BRONZE].qualified_unique_repo_count

        # Determine next tier for display
        current_tier_str = evaluation.current_tier.value if evaluation.current_tier else 'None'
        if evaluation.current_tier is None:
            next_tier_str = f' (Next: {TIERS_ORDER[0].value})'
        elif evaluation.current_tier == TIERS_ORDER[-1]:
            next_tier_str = ' (Max)'
        else:
            next_idx = TIERS_ORDER.index(evaluation.current_tier) + 1
            next_tier_str = f' (Next: {TIERS_ORDER[next_idx].value})'

        # UID summary
        bt.logging.info('')
        bt.logging.info('Summary:')
        bt.logging.info(
            f'├─ Score: {earned_score:.2f} - {evaluation.total_collateral_score:.2f} collateral = {evaluation.total_score:.2f}'
        )
        bt.logging.info(
            f'├─ PRs: {evaluation.total_merged_prs} merged | {evaluation.total_open_prs} open | {evaluation.total_closed_prs} closed'
        )
        bt.logging.info(f'├─ Tier: {current_tier_str}{next_tier_str}')
        bronze = evaluation.stats_by_tier[Tier.BRONZE]
        silver = evaluation.stats_by_tier[Tier.SILVER]
        gold = evaluation.stats_by_tier[Tier.GOLD]
        bt.logging.info(
            f'└─ Per-Tier: Bronze({bronze.merged_count}/{bronze.total_attempts}) | Silver({silver.merged_count}/{silver.total_attempts}) | Gold({gold.merged_count}/{gold.total_attempts})'
        )

    bt.logging.info('Finalization complete.')


def calculate_uniqueness_multiplier(
    repo_full_name: str, repo_counts: Dict[str, int], total_contributing_miners: int
) -> float:
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
    Only the first valid issue is scored. Adds bonus if issue was created by a maintainer.

    Returns:
        float: Multiplier between 1.0 and 2.0
    """
    if not pr.issues:
        bt.logging.info(f'PR #{pr.number} - Contains no linked issues')
        return 1.0

    valid_issues = [issue for issue in pr.issues if is_valid_issue(issue, pr)]
    if not valid_issues:
        bt.logging.info(f'PR #{pr.number} - Solved no valid issues')
        return 1.0

    issue = valid_issues[0]
    is_merged = pr.pr_state == PRState.MERGED

    # Check if issue was created by a maintainer (extra bonus)
    is_maintainer_issue = issue.author_association in MAINTAINER_ASSOCIATIONS if issue.author_association else False
    maintainer_bonus = MAINTAINER_ISSUE_BONUS if is_maintainer_issue else 0.0
    maintainer_str = ' (maintainer)' if is_maintainer_issue else ''

    if not issue.created_at:
        bonus = maintainer_bonus
        bt.logging.info(f'Issue #{issue.number} - No creation date | bonus: {bonus:.2f}{maintainer_str}')
        return 1.0 + bonus

    try:
        end_date = issue.closed_at if (is_merged and issue.closed_at) else datetime.now(timezone.utc)
        days_open = (end_date - issue.created_at).days
        # Scale age bonus from 0 to MAX_ISSUE_AGE_BONUS based on sqrt of days open
        age_ratio = math.sqrt(min(days_open, MAX_ISSUE_AGE_FOR_MAX_SCORE)) / math.sqrt(MAX_ISSUE_AGE_FOR_MAX_SCORE)
        age_bonus = MAX_ISSUE_AGE_BONUS * age_ratio
        total_bonus = age_bonus + maintainer_bonus
        bt.logging.info(f'Issue #{issue.number} - Open for {days_open} days | bonus: {total_bonus:.2f}{maintainer_str}')
        return 1.0 + total_bonus
    except (ValueError, AttributeError) as e:
        bt.logging.warning(
            f'Issue #{issue.number} - Could not calculate age. Using maintainer bonus only: {maintainer_bonus:.2f}. Exception: {e}'
        )
        return 1.0 + maintainer_bonus


def is_valid_issue(issue: Issue, pr: PullRequest) -> bool:
    """Check if issue is valid for bonus calculation (works for both merged and open PRs)."""
    is_merged = pr.pr_state == PRState.MERGED

    # Common checks (both merged and open)
    if not issue.author_login:
        bt.logging.warning(f'Skipping issue #{issue.number} - Issue is missing author information')
        return False

    if issue.author_login == pr.author_login:
        bt.logging.warning(f'Skipping issue #{issue.number} - Issue has same author as PR (self-created issue)')
        return False

    if issue.created_at and pr.created_at and issue.created_at > pr.created_at:
        bt.logging.warning(f'Skipping issue #{issue.number} - Issue was created after PR was created')
        return False

    # Merged-only checks
    if is_merged:
        if pr.last_edited_at and pr.last_edited_at > pr.merged_at:
            bt.logging.warning(f'Skipping issue #{issue.number} - PR was edited after merge')
            return False

        if issue.state and issue.state != 'CLOSED':
            bt.logging.warning(f'Skipping issue #{issue.number} - Issue state not CLOSED (state: {issue.state})')
            return False

        if issue.closed_at and pr.merged_at:
            days_diff = abs((issue.closed_at - pr.merged_at).total_seconds()) / SECONDS_PER_DAY
            if days_diff > MAX_ISSUE_CLOSE_WINDOW_DAYS:
                bt.logging.warning(
                    f'Skipping issue #{issue.number} - Issue closed {days_diff:.1f}d from merge (max: {MAX_ISSUE_CLOSE_WINDOW_DAYS})'
                )
                return False

    return True


# =============================================================================
# Collateral System Functions
# =============================================================================


def calculate_open_pr_collateral_score(pr: PullRequest) -> float:
    """
    Calculate collateral score for an open PR.

    Collateral = base_score * applicable_multipliers * DEFAULT_COLLATERAL_PERCENT

    Applicable multipliers: repo_weight, issue
    NOT applicable: time_decay (merge-based), credibility_multiplier (merge-based),
                    uniqueness (cross-miner), open_pr_spam (not for collateral)
    """
    from math import prod

    multipliers = {
        'repo_weight': pr.repo_weight_multiplier,
        'issue': pr.issue_multiplier,
    }

    potential_score = pr.base_score * prod(multipliers.values())
    collateral_percent = pr.repository_tier_configuration.open_pr_collateral_percentage
    collateral_score = potential_score * collateral_percent

    mult_str = ' | '.join([f'{k}: {v:.2f}' for k, v in multipliers.items()])
    bt.logging.info(
        f'OPEN PR #{pr.number} | base: {pr.base_score:.2f} | {mult_str} | '
        f'potential: {potential_score:.2f} | collateral ({collateral_percent * 100:.0f}%): {collateral_score:.2f}'
    )

    return collateral_score
