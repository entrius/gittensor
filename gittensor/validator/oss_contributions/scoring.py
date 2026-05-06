# The MIT License (MIT)
# Copyright © 2025 Entrius

from datetime import datetime
from typing import TYPE_CHECKING, Dict, Optional, Tuple, Union

import bittensor as bt

from gittensor.classes import (
    Issue,
    MinerEvaluation,
    PRState,
    PullRequest,
)

if TYPE_CHECKING:
    from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredMirrorPR
from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAINTAINER_ASSOCIATIONS,
    MAINTAINER_ISSUE_MULTIPLIER,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER,
    MAX_OPEN_PR_THRESHOLD,
    OPEN_PR_COLLATERAL_PERCENT,
    OPEN_PR_THRESHOLD_TOKEN_SCORE,
    PIONEER_DIVIDEND_MAX_RATIO,
    PIONEER_DIVIDEND_RATE_1ST,
    PIONEER_DIVIDEND_RATE_2ND,
    PIONEER_DIVIDEND_RATE_REST,
    REVIEW_PENALTY_RATE,
    SECONDS_PER_DAY,
    STANDARD_ISSUE_MULTIPLIER,
)
from gittensor.utils.github_api_tools import (
    FileContentPair,
    fetch_file_contents_with_base,
    get_merge_base_sha,
    get_pull_request_file_changes,
)
from gittensor.validator.oss_contributions.credibility import check_eligibility
from gittensor.validator.oss_contributions.label_resolution import resolve_legacy_label_multiplier
from gittensor.validator.utils.datetime_utils import calculate_time_decay
from gittensor.validator.utils.load_weights import LanguageConfig, RepositoryConfig, TokenConfig, resolve_repo_weight


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
    assert miner_eval.github_pat is not None, f'UID {miner_eval.uid} has no github_pat'

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

    calculate_pr_multipliers(pr, miner_eval, master_repositories)

    if pr.pr_state == PRState.MERGED:
        miner_eval.unique_repos_contributed_to.add(pr.repository_full_name)


def fetch_file_contents_for_pr(pr: PullRequest, github_pat: str) -> Dict[str, FileContentPair]:
    """Fetch both base and head file contents for all files in a PR using GraphQL batch fetch.

    Uses the merge-base commit (common ancestor) as the "before" state rather than
    the base branch tip, so the tree-diff only scores the PR's own changes.

    Returns:
        Dict mapping filename to FileContentPair(old_content, new_content)
        - old_content: File content before the PR (None for new files)
        - new_content: File content after the PR (None for deleted files)
    """
    if not pr.file_changes or not pr.head_ref_oid or not pr.base_ref_oid:
        return {}

    parts = pr.repository_full_name.split('/')
    if len(parts) != 2:
        bt.logging.warning(f'Invalid repository name format: {pr.repository_full_name}')
        return {}

    owner, repo_name = parts

    # Resolve merge-base to avoid scoring unrelated changes from the base branch.
    # baseRefOid is the base branch tip, which may include commits not in this PR.
    merge_base = get_merge_base_sha(pr.repository_full_name, pr.base_ref_oid, pr.head_ref_oid, github_pat)
    base_sha = merge_base if merge_base else pr.base_ref_oid
    if merge_base and merge_base != pr.base_ref_oid:
        bt.logging.debug(
            f'PR #{pr.number}: using merge-base {merge_base[:8]} instead of base_ref {pr.base_ref_oid[:8]}'
        )

    return fetch_file_contents_with_base(
        owner, repo_name, base_sha, pr.head_ref_oid, pr.file_changes, github_pat, pr_number=pr.number
    )


def calculate_base_score(
    pr: PullRequest,
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    file_contents: Dict[str, FileContentPair],
) -> float:
    """Run the shared base-score formula and copy fields onto the legacy PullRequest.

    Lazy import of the mirror helper avoids a legacy↔mirror cycle (mirror/scoring.py
    imports calculate_review_quality_multiplier from this module). On flip-day this
    whole legacy module deletes; the helper stays put in mirror.
    """
    from gittensor.validator.oss_contributions.mirror.scoring import calculate_base_score_for_pr_files

    result = calculate_base_score_for_pr_files(
        pr.file_changes or [], file_contents, programming_languages, token_config
    )
    pr.token_score = result.token_score
    pr.source_token_score = result.source_token_score
    pr.structural_count = result.structural_count
    pr.structural_score = result.structural_score
    pr.leaf_count = result.leaf_count
    pr.leaf_score = result.leaf_score
    pr.total_nodes_scored = result.total_nodes_scored
    pr.code_density = result.code_density
    return result.base_score


def calculate_review_quality_multiplier(changes_requested_count: int, pr_number: Optional[int] = None) -> float:
    """Calculate the review quality multiplier based on maintainer CHANGES_REQUESTED reviews.

    Formula: max(0.0, 1.0 - REVIEW_PENALTY_RATE × N)
    """
    multiplier = max(0.0, 1.0 - REVIEW_PENALTY_RATE * changes_requested_count)
    if changes_requested_count > 0:
        ctx = f' (PR #{pr_number})' if pr_number else ''
        bt.logging.info(
            f'{changes_requested_count} maintainer CHANGES_REQUESTED review(s){ctx} → '
            f'review_quality_multiplier={multiplier:.2f}'
        )
    return multiplier


def calculate_review_collateral_multiplier(changes_requested_count: int, pr_number: Optional[int] = None) -> float:
    """Calculate the open-PR collateral multiplier from maintainer CHANGES_REQUESTED reviews.

    Unlike ``review_quality_multiplier`` for earned scores, this increases
    collateral so non-merge-ready open PRs reserve more score instead of less.
    Formula: min(MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER, 1.0 + REVIEW_PENALTY_RATE × N)
    """
    multiplier = min(
        MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER,
        1.0 + REVIEW_PENALTY_RATE * changes_requested_count,
    )
    if changes_requested_count > 0:
        ctx = f' (PR #{pr_number})' if pr_number else ''
        bt.logging.info(
            f'{changes_requested_count} maintainer CHANGES_REQUESTED review(s){ctx} → '
            f'review_collateral_multiplier={multiplier:.2f}'
        )
    return multiplier


def calculate_pr_multipliers(
    pr: PullRequest, miner_eval: MinerEvaluation, master_repositories: Dict[str, RepositoryConfig]
) -> None:
    """Calculate all multipliers for a PR."""
    is_merged = pr.pr_state == PRState.MERGED
    repo_config = master_repositories.get(pr.repository_full_name)

    pr.repo_weight_multiplier = resolve_repo_weight(repo_config)
    pr.issue_multiplier = round(calculate_issue_multiplier(pr), 2)
    pr.label, pr.label_multiplier = resolve_legacy_label_multiplier(
        pr.label_timeline_order,
        pr.current_labels,
        repo_config,
    )

    if is_merged:
        # Spam multiplier is recalculated in finalize_miner_scores with total token score
        pr.open_pr_spam_multiplier = 1.0
        assert pr.merged_at is not None, f'PR #{pr.number} has no merged_at'
        pr.time_decay_multiplier = round(calculate_time_decay(pr.merged_at), 2)
        pr.review_quality_multiplier = round(
            calculate_review_quality_multiplier(pr.changes_requested_count, pr.number), 2
        )
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


def calculate_pioneer_dividends(
    miner_evaluations: Dict[int, MinerEvaluation],
) -> None:
    """Determine pioneers and set pioneer_rank + pioneer_dividend on each PR.

    For each repo, the pioneer is the miner with the earliest merged PR that
    passes the quality gate (is_pioneer_eligible). The pioneer's earliest PR
    on that repo earns a dividend based on ALL followers' earned_scores (post-
    multiplier), using per-position rates (30%/20%/10%). The dividend uses the
    follower's multipliers, not the pioneer's — so it reflects follower quality.

    Walks both legacy ``merged_pull_requests`` and mirror ``mirror_merged_prs``;
    a repo is mirror-enabled or legacy, never both, so the per-repo grouping
    sees a single path's PRs only.

    Must be called AFTER all earned_scores have been computed.
    """
    pr_index: Dict[str, Dict[int, list]] = {}
    repo_contributions: Dict[str, Dict[int, Tuple[datetime, int, float]]] = {}

    for uid, evaluation in miner_evaluations.items():
        for pr in evaluation.merged_pull_requests + evaluation.mirror_merged_prs:
            if not pr.is_pioneer_eligible():
                continue
            assert pr.merged_at is not None
            repo = pr.repository_full_name
            pr_index.setdefault(repo, {}).setdefault(uid, []).append(pr)

            current = repo_contributions.setdefault(repo, {}).get(uid)
            if current is None:
                repo_contributions[repo][uid] = (pr.merged_at, pr.number, pr.earned_score)
            else:
                earliest_at, earliest_num, total_score = current
                new_total = total_score + pr.earned_score
                if pr.merged_at < earliest_at or (pr.merged_at == earliest_at and pr.number < earliest_num):
                    repo_contributions[repo][uid] = (pr.merged_at, pr.number, new_total)
                else:
                    repo_contributions[repo][uid] = (earliest_at, earliest_num, new_total)

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
        pioneer_pr.earned_score = round(pioneer_pr.earned_score + pioneer_pr.pioneer_dividend, 2)

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

        # Process open PRs for collateral (legacy + mirror — both paths contribute)
        for pr in evaluation.open_pull_requests + evaluation.mirror_open_prs:
            pr.collateral_score = calculate_open_pr_collateral_score(pr)
            evaluation.total_collateral_score += pr.collateral_score

        has_contributions = evaluation.total_merged_prs > 0 or evaluation.total_closed_prs > 0

        if not has_contributions:
            bt.logging.info('No merged or closed PRs - skipping evaluation')
            continue

        # Check eligibility gate across both paths. check_eligibility uses the
        # SOURCE-quality token score when present, falling back to token_score
        # for older/test objects, so combining the lists works without adapting types.
        is_eligible, credibility, reason = check_eligibility(
            evaluation.merged_pull_requests + evaluation.mirror_merged_prs,
            evaluation.closed_pull_requests + evaluation.mirror_closed_prs,
        )
        evaluation.is_eligible = is_eligible
        evaluation.credibility = credibility

        if not is_eligible:
            bt.logging.info(f'UID {uid} ineligible: {reason} — score set to 0')
            continue

        # Calculate spam multiplier once per miner using combined total token score
        merged_prs = evaluation.merged_pull_requests + evaluation.mirror_merged_prs
        preliminary_token_score = sum(pr.token_score for pr in merged_prs)
        spam_multiplier = calculate_pr_spam_penalty_multiplier(evaluation.total_open_prs, preliminary_token_score)

        for pr in merged_prs:
            pr.open_pr_spam_multiplier = spam_multiplier
            pr.credibility_multiplier = round(credibility, 2)
            pr.calculate_final_earned_score()

            evaluation.total_token_score += pr.token_score
            evaluation.total_structural_count += pr.structural_count
            evaluation.total_structural_score += pr.structural_score
            evaluation.total_leaf_count += pr.leaf_count
            evaluation.total_leaf_score += pr.leaf_score

    # Phase 2: Calculate pioneer dividends from follower earned_scores
    # (walks legacy + mirror merged PRs in one pass).
    calculate_pioneer_dividends(miner_evaluations)

    # Phase 3: Aggregate totals (including dividends), collateral, logging
    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        has_contributions = evaluation.total_merged_prs > 0 or evaluation.total_closed_prs > 0
        if not has_contributions:
            continue

        # Aggregate scores (earned_score now includes pioneer_dividend from Phase 2).
        # ScoredMirrorPR shares base_score / earned_score / total_nodes_scored with PullRequest.
        for pr in evaluation.merged_pull_requests + evaluation.mirror_merged_prs:
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
        bt.logging.info(f'└─ Eligible: {evaluation.is_eligible} | Credibility: {evaluation.credibility:.2f}')

    bt.logging.info('Finalization complete.')


def calculate_issue_multiplier(pr: PullRequest) -> float:
    """
    Calculate PR score multiplier from the best valid linked issue.

    Returns a flat multiplier: MAINTAINER_ISSUE_MULTIPLIER (1.66) if any valid issue
    author is a maintainer (OWNER/MEMBER/COLLABORATOR), otherwise
    STANDARD_ISSUE_MULTIPLIER (1.33). Returns 1.0 if no valid linked issues.

    Mirror has a near-identical ``_calculate_issue_multiplier`` in mirror/scoring.py.
    Kept separate intentionally: ``Issue`` and ``MirrorLinkedIssue`` differ on field
    names (``issues`` / ``number`` vs ``linked_issues`` / ``pr_number``) and the
    validators (``is_valid_issue`` vs ``_is_valid_linked_issue``) apply different
    anti-gaming gates (transferred check, edited_after_merge source, github_id-based
    self-issue check). Unifying would require accessor adapters that cost more than
    the duplicate saves.
    """
    if not pr.issues:
        bt.logging.info(f'PR #{pr.number} - Contains no linked issues')
        return 1.0

    valid_issues = [issue for issue in pr.issues if is_valid_issue(issue, pr)]
    if not valid_issues:
        bt.logging.info(f'PR #{pr.number} - Solved no valid issues')
        return 1.0

    # Prefer a maintainer-authored valid issue if one exists, so the multiplier
    # doesn't depend on GraphQL closingIssuesReferences ordering.
    issue = next(
        (i for i in valid_issues if i.author_association in MAINTAINER_ASSOCIATIONS),
        valid_issues[0],
    )
    is_maintainer = issue.author_association in MAINTAINER_ASSOCIATIONS if issue.author_association else False
    multiplier = MAINTAINER_ISSUE_MULTIPLIER if is_maintainer else STANDARD_ISSUE_MULTIPLIER
    label = 'maintainer' if is_maintainer else 'standard'
    bt.logging.info(f'Issue #{issue.number} - {label} issue | multiplier: {multiplier}')
    return multiplier


def _is_completed_when_closed(issue: Issue) -> bool:
    if issue.state != 'CLOSED':
        return True
    if issue.state_reason != 'COMPLETED':
        bt.logging.warning(
            f'Skipping issue #{issue.number} - state_reason={issue.state_reason}, only COMPLETED grants multiplier'
        )
        return False
    return True


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

    if not _is_completed_when_closed(issue):
        return False

    if is_merged and pr.merged_at:
        if pr.last_edited_at and pr.last_edited_at > pr.merged_at:
            bt.logging.warning(f'Skipping issue #{issue.number} - PR was edited after merge')
            return False

        if issue.state and issue.state != 'CLOSED':
            bt.logging.warning(f'Skipping issue #{issue.number} - Issue state not CLOSED (state: {issue.state})')
            return False

        if issue.closed_at and pr.merged_at:
            days_diff = (issue.closed_at - pr.merged_at).total_seconds() / SECONDS_PER_DAY
            if days_diff > MAX_ISSUE_CLOSE_WINDOW_DAYS or days_diff < 0:
                bt.logging.warning(
                    f'Skipping issue #{issue.number} - Issue closed {days_diff:+.2f}d from merge (max: {MAX_ISSUE_CLOSE_WINDOW_DAYS})'
                )
                return False

    return True


# =============================================================================
# Collateral System Functions
# =============================================================================


def calculate_open_pr_collateral_score(
    pr: Union[PullRequest, 'ScoredMirrorPR'],
) -> float:  # TODO: drop Union on legacy delete day
    """
    Calculate collateral score for an open PR.

    Collateral = base_score * applicable_multipliers * OPEN_PR_COLLATERAL_PERCENT

    Applicable multipliers: repo_weight, issue, label, review_collateral
    NOT applicable: time_decay (merge-based), credibility_multiplier (merge-based),
                    open_pr_spam (not for collateral)
    """
    from math import prod

    multipliers = {
        'repo_weight': pr.repo_weight_multiplier,
        'issue': pr.issue_multiplier,
        'label': pr.label_multiplier,
        'review_collateral': calculate_review_collateral_multiplier(pr.changes_requested_count, pr.number),
    }

    potential_score = pr.base_score * prod(multipliers.values())
    collateral_score = potential_score * OPEN_PR_COLLATERAL_PERCENT

    mult_str = ' | '.join([f'{k}: {v:.2f}' for k, v in multipliers.items()])
    bt.logging.info(
        f'OPEN PR #{pr.number} | base: {pr.base_score:.2f} | {mult_str} | '
        f'potential: {potential_score:.2f} | collateral ({OPEN_PR_COLLATERAL_PERCENT * 100:.0f}%): {collateral_score:.2f}'
    )

    return collateral_score
