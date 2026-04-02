# The MIT License (MIT)
# Copyright © 2025 Entrius

import math
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation, PrScoringResult, PRState, PullRequest
from gittensor.constants import (
    CONTRIBUTION_SCORE_FOR_FULL_BONUS,
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAINTAINER_ASSOCIATIONS,
    MAINTAINER_ISSUE_BONUS,
    MAX_CODE_DENSITY_MULTIPLIER,
    MAX_CONTRIBUTION_BONUS,
    MAX_ISSUE_AGE_BONUS,
    MAX_ISSUE_AGE_FOR_MAX_SCORE,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    MAX_OPEN_PR_THRESHOLD,
    MERGED_PR_BASE_SCORE,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    OPEN_PR_COLLATERAL_PERCENT,
    OPEN_PR_THRESHOLD_TOKEN_SCORE,
    PIONEER_DIVIDEND_MAX_RATIO,
    PIONEER_DIVIDEND_RATE_1ST,
    PIONEER_DIVIDEND_RATE_2ND,
    PIONEER_DIVIDEND_RATE_REST,
    REVIEW_PENALTY_RATE,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
)
from gittensor.utils.github_api_tools import (
    FileContentPair,
    fetch_file_contents_with_base,
    get_pull_request_file_changes,
    get_pull_request_maintainer_changes_requested_count,
)
from gittensor.validator.oss_contributions.credibility import check_eligibility
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
    """Scores a single PR and populates relevant PullRequest fields."""

    repo_config = master_repositories.get(pr.repository_full_name)
    if not repo_config:
        bt.logging.warning(f'{pr.repository_full_name} not in master repositories. Skipping...')
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

    # Fetch review data before multiplier calculation (only for merged PRs)
    if pr.pr_state == PRState.MERGED:
        pr.changes_requested_count = get_pull_request_maintainer_changes_requested_count(
            pr.repository_full_name, pr.number, miner_eval.github_pat
        )

    calculate_pr_multipliers(pr, miner_eval, master_repositories)

    if pr.pr_state == PRState.MERGED:
        miner_eval.unique_repos_contributed_to.add(pr.repository_full_name)


def fetch_file_contents_for_pr(pr: PullRequest, github_pat: str) -> Dict[str, FileContentPair]:
    """Fetch both base and head file contents for all files in a PR using GraphQL batch fetch."""
    if not pr.file_changes or not pr.head_ref_oid or not pr.base_ref_oid:
        return {}

    parts = pr.repository_full_name.split('/')
    if len(parts) != 2:
        bt.logging.warning(f'Invalid repository name format: {pr.repository_full_name}')
        return {}

    owner, repo_name = parts

    return fetch_file_contents_with_base(
        owner, repo_name, pr.base_ref_oid, pr.head_ref_oid, pr.file_changes, github_pat
    )


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
        code_density = min(pr.token_score / total_lines, MAX_CODE_DENSITY_MULTIPLIER)
        initial_base_score = MERGED_PR_BASE_SCORE * code_density
    else:
        code_density = 0.0
        initial_base_score = 0.0

    # Calculate contribution bonus, capped
    bonus_percent = min(1.0, scoring_result.total_score / CONTRIBUTION_SCORE_FOR_FULL_BONUS)
    contribution_bonus = round(bonus_percent * MAX_CONTRIBUTION_BONUS, 2)

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
        f'({bonus_percent * 100:.0f}% of max {MAX_CONTRIBUTION_BONUS}) = {base_score:.2f}'
    )

    return base_score


def calculate_review_quality_multiplier(changes_requested_count: int) -> float:
    """Calculate the review quality multiplier based on maintainer CHANGES_REQUESTED reviews.

    Formula: max(0.0, 1.0 - REVIEW_PENALTY_RATE × N)
    """
    multiplier = max(0.0, 1.0 - REVIEW_PENALTY_RATE * changes_requested_count)
    if changes_requested_count > 0:
        bt.logging.info(
            f'{changes_requested_count} maintainer CHANGES_REQUESTED review(s) → '
            f'review_quality_multiplier={multiplier:.2f}'
        )
    return multiplier


def calculate_pr_multipliers(
    pr: PullRequest, miner_eval: MinerEvaluation, master_repositories: Dict[str, RepositoryConfig]
) -> None:
    """Calculate all multipliers for a PR."""
    is_merged = pr.pr_state == PRState.MERGED
    repo_config = master_repositories.get(pr.repository_full_name)

    pr.repo_weight_multiplier = round(repo_config.weight if repo_config else 0.01, 2)
    pr.issue_multiplier = round(calculate_issue_multiplier(pr), 2)

    if is_merged:
        # Spam multiplier is recalculated in finalize_miner_scores with total token score
        pr.open_pr_spam_multiplier = 1.0
        pr.time_decay_multiplier = round(calculate_time_decay_multiplier(pr), 2)
        pr.review_quality_multiplier = round(calculate_review_quality_multiplier(pr.changes_requested_count), 2)
    else:
        pr.open_pr_spam_multiplier = 1.0
        pr.time_decay_multiplier = 1.0
        pr.credibility_multiplier = 1.0
        pr.review_quality_multiplier = 1.0


def calculate_open_pr_threshold(total_token_score: float = 0.0) -> int:
    """Calculate dynamic open PR threshold based on total token score.

    Bonus = floor(total_token_score / OPEN_PR_THRESHOLD_TOKEN_SCORE)
    Threshold = min(BASE_THRESHOLD + bonus, MAX_OPEN_PR_THRESHOLD)
    """
    bonus = int(total_token_score // OPEN_PR_THRESHOLD_TOKEN_SCORE)
    return min(EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + bonus, MAX_OPEN_PR_THRESHOLD)


def calculate_pr_spam_penalty_multiplier(total_open_prs: int, total_token_score: float = 0.0) -> float:
    """Apply penalty for excessive open PRs.

    Binary multiplier: 1.0 if open PRs <= threshold, 0.0 otherwise.
    """
    threshold = calculate_open_pr_threshold(total_token_score)
    return 1.0 if total_open_prs <= threshold else 0.0


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


def calculate_pioneer_dividends(
    miner_evaluations: Dict[int, MinerEvaluation],
) -> None:
    """Determine pioneers and set pioneer_rank + pioneer_dividend on each PR.

    For each repo, the pioneer is the miner with the earliest merged PR that
    passes the quality gate (is_pioneer_eligible). The pioneer's earliest PR
    on that repo earns a dividend based on ALL followers' earned_scores (post-
    multiplier), using per-position rates (30%/20%/10%). The dividend uses the
    follower's multipliers, not the pioneer's — so it reflects follower quality.

    Must be called AFTER all earned_scores have been computed.
    """
    pr_index: Dict[str, Dict[int, list]] = {}
    repo_contributions: Dict[str, Dict[int, Tuple[datetime, int, float]]] = {}

    for evaluation in miner_evaluations.values():
        for pr in evaluation.merged_pull_requests:
            if not pr.is_pioneer_eligible():
                continue
            repo = pr.repository_full_name
            pr_index.setdefault(repo, {}).setdefault(pr.uid, []).append(pr)

            current = repo_contributions.setdefault(repo, {}).get(pr.uid)
            if current is None:
                repo_contributions[repo][pr.uid] = (pr.merged_at, pr.number, pr.earned_score)
            else:
                earliest_at, earliest_num, total_score = current
                new_total = total_score + pr.earned_score
                if pr.merged_at < earliest_at or (pr.merged_at == earliest_at and pr.number < earliest_num):
                    repo_contributions[repo][pr.uid] = (pr.merged_at, pr.number, new_total)
                else:
                    repo_contributions[repo][pr.uid] = (earliest_at, earliest_num, new_total)

    for repo, uid_entries in repo_contributions.items():
        sorted_uids = sorted(uid_entries.items(), key=lambda x: (x[1][0], x[1][1]))

        for rank_pos, (uid, _) in enumerate(sorted_uids):
            for pr in pr_index[repo][uid]:
                pr.pioneer_rank = rank_pos + 1

        dividend = 0.0
        for pos, (_, entry) in enumerate(sorted_uids[1:]):
            follower_earned = entry[2]
            if pos == 0:
                dividend += follower_earned * PIONEER_DIVIDEND_RATE_1ST
            elif pos == 1:
                dividend += follower_earned * PIONEER_DIVIDEND_RATE_2ND
            else:
                dividend += follower_earned * PIONEER_DIVIDEND_RATE_REST

        if dividend <= 0:
            continue

        pioneer_uid = sorted_uids[0][0]
        pioneer_pr_number = sorted_uids[0][1][1]
        pioneer_pr = next(pr for pr in pr_index[repo][pioneer_uid] if pr.number == pioneer_pr_number)
        max_dividend = pioneer_pr.earned_score * PIONEER_DIVIDEND_MAX_RATIO
        capped = min(dividend, max_dividend)
        pioneer_pr.pioneer_dividend = round(capped, 2)
        pioneer_pr.earned_score += pioneer_pr.pioneer_dividend

        cap_note = f' (capped from {dividend:.2f})' if capped < dividend else ''
        bt.logging.info(
            f'Pioneer dividend | repo={repo} pioneer=uid {pioneer_uid} '
            f'followers={len(sorted_uids) - 1} dividend={capped:.2f}{cap_note}'
        )


def finalize_miner_scores(miner_evaluations: Dict[int, MinerEvaluation]) -> None:
    """Finalize all miner scores: compute earned_scores, then apply pioneer dividends, then collateral."""
    bt.logging.info('**Finalizing miner scores**')

    # Phase 1: Compute all earned_scores (base × multipliers) for every miner
    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        bt.logging.info('')
        bt.logging.info('=' * 50)
        bt.logging.info(f'UID {uid}')
        bt.logging.info('=' * 50)

        # Process open PRs for collateral
        for pr in evaluation.open_pull_requests:
            pr.collateral_score = calculate_open_pr_collateral_score(pr)
            evaluation.total_collateral_score += pr.collateral_score

        has_contributions = len(evaluation.merged_pull_requests) > 0 or len(evaluation.closed_pull_requests) > 0

        if not has_contributions:
            bt.logging.info('No merged or closed PRs - skipping evaluation')
            continue

        # Check eligibility gate (credibility with mulligan + min valid PRs)
        is_eligible, credibility, reason = check_eligibility(
            evaluation.merged_pull_requests, evaluation.closed_pull_requests
        )
        evaluation.is_eligible = is_eligible
        evaluation.credibility = credibility

        if not is_eligible:
            bt.logging.info(f'UID {uid} ineligible: {reason} — score set to 0')
            continue

        # Calculate spam multiplier once per miner using total token score
        # We need to compute total_token_score first from all merged PRs
        preliminary_token_score = sum(pr.token_score for pr in evaluation.merged_pull_requests)
        spam_multiplier = calculate_pr_spam_penalty_multiplier(evaluation.total_open_prs, preliminary_token_score)

        # Process merged PRs
        for pr in evaluation.merged_pull_requests:
            pr.open_pr_spam_multiplier = spam_multiplier

            # Apply linear credibility multiplier (k=1)
            pr.credibility_multiplier = round(credibility, 2)

            pr.calculate_final_earned_score()

            # Aggregate token scoring breakdown
            evaluation.total_token_score += pr.token_score
            evaluation.total_structural_count += pr.structural_count
            evaluation.total_structural_score += pr.structural_score
            evaluation.total_leaf_count += pr.leaf_count
            evaluation.total_leaf_score += pr.leaf_score

    # Phase 2: Calculate pioneer dividends from follower earned_scores
    calculate_pioneer_dividends(miner_evaluations)

    # Phase 3: Aggregate totals (including dividends), collateral, logging
    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        has_contributions = len(evaluation.merged_pull_requests) > 0 or len(evaluation.closed_pull_requests) > 0
        if not has_contributions:
            continue

        # Aggregate scores (earned_score now includes pioneer_dividend from Phase 2)
        for pr in evaluation.merged_pull_requests:
            evaluation.base_total_score += pr.base_score
            evaluation.total_score += pr.earned_score
            evaluation.total_nodes_scored += pr.total_nodes_scored

        # Apply collateral deduction
        earned_score = evaluation.total_score
        evaluation.total_score = max(0.0, earned_score - evaluation.total_collateral_score)
        evaluation.unique_repos_count = len(evaluation.unique_repos_contributed_to)

        # UID summary
        bt.logging.info('')
        bt.logging.info('Summary:')
        bt.logging.info(
            f'├─ Score: {earned_score:.2f} - {evaluation.total_collateral_score:.2f} collateral = {evaluation.total_score:.2f}'
        )
        bt.logging.info(
            f'├─ PRs: {evaluation.total_merged_prs} merged | {evaluation.total_open_prs} open | {evaluation.total_closed_prs} closed'
        )
        bt.logging.info(
            f'└─ Eligible: {evaluation.is_eligible} | Credibility: {evaluation.credibility:.2f}'
        )

    bt.logging.info('Finalization complete.')


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

    if not issue.author_login:
        bt.logging.warning(f'Skipping issue #{issue.number} - Issue is missing author information')
        return False

    if issue.author_login == pr.author_login:
        bt.logging.warning(f'Skipping issue #{issue.number} - Issue has same author as PR (self-created issue)')
        return False

    if issue.created_at and pr.created_at and issue.created_at > pr.created_at:
        bt.logging.warning(f'Skipping issue #{issue.number} - Issue was created after PR was created')
        return False

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

    Collateral = base_score * applicable_multipliers * OPEN_PR_COLLATERAL_PERCENT

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
    collateral_score = potential_score * OPEN_PR_COLLATERAL_PERCENT

    mult_str = ' | '.join([f'{k}: {v:.2f}' for k, v in multipliers.items()])
    bt.logging.info(
        f'OPEN PR #{pr.number} | base: {pr.base_score:.2f} | {mult_str} | '
        f'potential: {potential_score:.2f} | collateral ({OPEN_PR_COLLATERAL_PERCENT * 100:.0f}%): {collateral_score:.2f}'
    )

    return collateral_score
