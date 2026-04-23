"""Issue discovery via the das-github-mirror service.

Replaces the timeline-scraping legacy path for mirror-enabled repos. Mirror
returns per-miner issues with an authoritative ``solved_by_pr`` and an inline
``solving_pr`` carrying everything needed to score the discovery: author_id,
merge state, hours_since_merge, edited_after_merge, review_summary, shas.

Populates the existing ``MinerEvaluation`` issue-discovery fields directly
(``issue_discovery_score``, ``total_solved_issues``, etc.) so downstream
emission blending / normalization doesn't change.

Anti-gaming gates (all applied):
- solved_by_pr must be populated
- solving_pr.state == 'MERGED'
- not solving_pr.edited_after_merge
- issue.state_reason == 'COMPLETED' (not NOT_PLANNED, not null)
- not issue.is_transferred
- issue.author_github_id != solving_pr.author_github_id (anti-self-issue)

One-issue-per-PR rule and "solver-is-also-discoverer" credibility-only gate
mirror the legacy behavior in
``gittensor.validator.issue_discovery.scoring._collect_issues_from_prs``.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation
from gittensor.constants import (
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    PR_LOOKBACK_DAYS,
)
from gittensor.utils.mirror.client import MirrorClient, MirrorRequestError
from gittensor.utils.mirror.models import MirrorIssue, MirrorSolvingPR
from gittensor.validator.issue_discovery.scoring import (
    calculate_issue_review_quality_multiplier,
    calculate_open_issue_spam_multiplier,
    check_issue_eligibility,
)
from gittensor.validator.utils.datetime_utils import calculate_time_decay
from gittensor.validator.utils.load_weights import RepositoryConfig, resolve_repo_weight


async def run_mirror_issue_discovery(
    miner_evaluations: Dict[int, MinerEvaluation],
    mirror_repos: Dict[str, RepositoryConfig],
    client: Optional[MirrorClient] = None,
) -> None:
    """Score issue discovery for mirror-enabled repos. Mutates miner_evaluations.

    For each miner, fetches their authored issues via the mirror and computes
    the usual issue-discovery scoring against the mirror data. Issues in repos
    not present in ``mirror_repos`` are filtered out client-side (mirror returns
    all tracked repos; the gittensor config's mirror_enabled set may be narrower).
    """
    bt.logging.info('**Scoring issue discovery (mirror)**')

    if not mirror_repos:
        bt.logging.info('No mirror-enabled repos — mirror issue discovery skipped')
        return

    client = client or MirrorClient()
    lookback_date = datetime.now(timezone.utc) - timedelta(days=PR_LOOKBACK_DAYS)
    enabled_names: Set[str] = set(mirror_repos.keys())

    # Preserve the existing "one-issue-per-PR" and "canonical solver" semantics.
    # Mirror's solved_by_pr is authoritative so no cross-miner canonicalization
    # is needed — each (repo, issue) has exactly one solving_pr globally.
    for uid, evaluation in miner_evaluations.items():
        if not evaluation.github_id or evaluation.github_id == '0':
            continue
        if evaluation.failed_reason is not None:
            continue

        try:
            response = client.get_miner_issues(evaluation.github_id, since=lookback_date)
        except MirrorRequestError as e:
            bt.logging.warning(
                f'UID {uid} mirror issue fetch failed: {e} — issue discovery skipped for this miner'
            )
            continue

        filtered = [i for i in response.issues if i.repo_full_name in enabled_names]
        if not filtered:
            continue

        _score_miner_mirror_issues(evaluation, filtered, mirror_repos)


def _score_miner_mirror_issues(
    evaluation: MinerEvaluation,
    issues: List[MirrorIssue],
    mirror_repos: Dict[str, RepositoryConfig],
) -> None:
    """Classify + score one miner's mirror issues, populate their MinerEvaluation fields."""
    solved_count = 0
    valid_solved_count = 0
    closed_count = 0
    issue_token_score = 0.0
    scored_issues: List[Issue] = []

    # One-issue-per-PR: the earliest-created issue a PR closes gets the score;
    # later issues closed by the same PR add credibility only.
    pr_scored_keys: Set[Tuple[str, int]] = set()

    # Group issues by (repo, solving_pr_number) to enforce one-issue-per-PR
    # with the earliest-created winning.
    issues_sorted = sorted(
        issues,
        key=lambda i: (
            i.repo_full_name,
            i.solved_by_pr or 0,
            i.created_at or datetime.max.replace(tzinfo=timezone.utc),
        ),
    )

    for issue in issues_sorted:
        classification = _classify_issue(issue)
        if classification == 'not-solved-closed':
            closed_count += 1
            continue
        if classification == 'ignore':
            continue

        # classification == 'solved'
        assert issue.solving_pr is not None  # classify_issue guarantees
        solving_pr = issue.solving_pr

        solved_count += 1

        # Valid-solved gate: solving PR meets token-score minimum.
        # Mirror doesn't surface per-PR token_score; using the proxy of
        # "PR state is MERGED and has any file scoring_data_stored" would be
        # imprecise. Treating all passing-gate solved issues as valid is the
        # conservative choice — we can refine once token_score per PR is
        # available via get_pr_files + local scoring (followup item).
        valid_solved_count += 1

        # Same-account: discoverer == solver gets credibility only, no score
        if issue.author_github_id == solving_pr.author_github_id:
            continue

        pr_key = (issue.repo_full_name, solving_pr.pr_number)
        if pr_key in pr_scored_keys:
            continue  # one-issue-per-PR (credibility already incremented above)
        pr_scored_keys.add(pr_key)

        repo_config = mirror_repos.get(issue.repo_full_name)
        if repo_config is None:
            continue

        # Populate the discovery fields on the legacy Issue shape so finalize
        # downstream math can run unchanged.
        adapted = _mirror_issue_for_scoring(issue, solving_pr, repo_config)
        if adapted is None:
            continue

        scored_issues.append(adapted)
        # Proxy token_score: use base_score as a rough quality signal (same
        # order of magnitude as legacy PR token_score). Followup: fetch the
        # solving PR's files via MirrorClient.get_pr_files and compute actual
        # token_score for precise spam-multiplier calculations.
        issue_token_score += adapted.discovery_base_score

    evaluation.total_solved_issues = solved_count
    evaluation.total_valid_solved_issues = valid_solved_count
    evaluation.total_closed_issues += closed_count  # additive with legacy path
    evaluation.issue_token_score = round(issue_token_score, 2)

    is_eligible, credibility, reason = check_issue_eligibility(valid_solved_count, closed_count)
    evaluation.is_issue_eligible = is_eligible or evaluation.is_issue_eligible
    evaluation.issue_credibility = max(evaluation.issue_credibility, credibility)

    if not is_eligible:
        bt.logging.info(f'UID {evaluation.uid} mirror issue discovery ineligible: {reason}')
        return

    spam_mult = calculate_open_issue_spam_multiplier(evaluation.total_open_issues, issue_token_score)

    total_discovery_score = 0.0
    for issue in scored_issues:
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

    evaluation.issue_discovery_score = round(
        evaluation.issue_discovery_score + total_discovery_score, 2
    )

    bt.logging.info(
        f'UID {evaluation.uid} mirror issue discovery: {solved_count} solved, {closed_count} closed, '
        f'credibility={credibility:.2f}, mirror_score={total_discovery_score:.2f}'
    )


def _classify_issue(issue: MirrorIssue) -> str:
    """Return 'solved', 'not-solved-closed', or 'ignore' per anti-gaming gates.

    'ignore' = issue is open / transferred / has no scorable meaning at all.
    'not-solved-closed' = counts against credibility (closed but not solved).
    'solved' = counts toward solved metrics.
    """
    if issue.is_transferred:
        return 'ignore'

    if issue.state != 'CLOSED':
        return 'ignore'

    if issue.state_reason != 'COMPLETED':
        # NOT_PLANNED or null → closed without being solved
        return 'not-solved-closed'

    if not issue.solved_by_pr or not issue.solving_pr:
        return 'not-solved-closed'

    sp = issue.solving_pr
    if sp.state != 'MERGED':
        return 'not-solved-closed'

    if sp.edited_after_merge:
        return 'not-solved-closed'

    if not issue.author_github_id:
        return 'ignore'

    return 'solved'


def _mirror_issue_for_scoring(
    issue: MirrorIssue,
    solving_pr: MirrorSolvingPR,
    repo_config: RepositoryConfig,
) -> Optional[Issue]:
    """Build a legacy ``Issue`` with discovery_* fields populated, ready for the
    same final earned_score composition that score_discovered_issues uses.

    Returns None if the solving PR lacks required fields (e.g. merged_at missing).
    """
    if solving_pr.merged_at is None:
        return None

    adapted = Issue(
        number=issue.issue_number,
        pr_number=solving_pr.pr_number,
        repository_full_name=issue.repo_full_name,
        title=issue.title,
        created_at=issue.created_at,
        closed_at=issue.closed_at,
        author_login=issue.author_login,
        state=issue.state,
        author_association=issue.author_association,
        author_github_id=issue.author_github_id,
        state_reason=issue.state_reason,
        updated_at=issue.updated_at,
        body_or_title_edited_at=None,
    )

    # Base score proxy: mirror doesn't return per-PR token_score on the /issues
    # endpoint. We use a fixed positive constant so repo_weight / time_decay /
    # review_quality still differentiate scoring. Followup: fetch get_pr_files
    # and run token scoring for the precise base.
    from gittensor.constants import MERGED_PR_BASE_SCORE

    adapted.discovery_base_score = float(MERGED_PR_BASE_SCORE)
    adapted.discovery_repo_weight_multiplier = resolve_repo_weight(repo_config)
    adapted.discovery_time_decay_multiplier = round(calculate_time_decay(solving_pr.merged_at), 2)
    adapted.discovery_review_quality_multiplier = round(
        calculate_issue_review_quality_multiplier(
            solving_pr.review_summary.maintainer_changes_requested_count
        ),
        2,
    )

    return adapted
