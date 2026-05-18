"""Issue discovery via the das-github-mirror service.

The mirror returns per-miner issues with an authoritative ``solved_by_pr`` and
an inline ``solving_pr`` carrying everything needed to classify the discovery:
author_id, merge state, hours_since_merge, edited_after_merge, review_summary,
shas.

Populates the issue-discovery fields on ``MinerEvaluation``
(``issue_discovery_score``, ``total_solved_issues``, etc.) so downstream
emission blending / normalization doesn't change.

Anti-gaming gates (all applied):
- solved_by_pr must be populated
- solving_pr.state == 'MERGED'
- not solving_pr.edited_after_merge
- issue.last_edited_at <= solving_pr.merged_at (anti-spec-rewrite)
- issue.state_reason == 'COMPLETED' (not NOT_PLANNED, not null)
- not issue.is_transferred
- issue.author_github_id != solving_pr.author_github_id (anti-self-issue)

Same-account ("solver is also discoverer") gives credibility only — no
discovery score. One-issue-per-PR rule is round-global: a single solving PR
awards at most one discovery score across the entire validator round, even
when it closes issues authored by different miners. The earliest-created
qualifying issue across all miners wins; the rest are credibility only.

Base-score resolution uses a per-cycle cross-miner cache. Most solving PRs
will be miners' own PRs that OSS scoring already tokenized; the cache
pre-populates from every miner's ``merged_prs`` so those hits require
no HTTP. Non-miner-solved PRs (cache misses) trigger
``MirrorClient.get_pr_files`` + token scoring, with the result written back
to the cache so sibling discoveries benefit.
"""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import bittensor as bt

from gittensor.classes import Issue, MinerEvaluation, MinerEvaluationCache, RepoEvaluation
from gittensor.constants import (
    MAINTAINER_ASSOCIATIONS,
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
from gittensor.validator.oss_contributions.mirror.adapters import mirror_files_to_legacy
from gittensor.validator.oss_contributions.mirror.scoring import (
    calculate_base_score_for_pr_files,
)
from gittensor.validator.utils.datetime_utils import calculate_time_decay
from gittensor.validator.utils.load_weights import (
    LanguageConfig,
    RepositoryConfig,
    TokenConfig,
    resolve_eligibility,
)


@dataclass
class CachedSolvingPR:
    """Per-cycle cached token-scoring output for a solving PR.

    Populated from miners' ``merged_prs`` before the per-miner loop
    runs; cache-missed entries are filled on demand from
    ``MirrorClient.get_pr_files`` and written back so other miners' discoveries
    that reference the same solving PR hit the cache.

    A cache miss that fails to fetch files is NOT cached — leaving it unset so
    a later miner in the same cycle could retry. In practice the fetch is
    unlikely to flip between success and failure within a single cycle, but
    keeping the cache free of negative entries is a minor safety.
    """

    base_score: float
    token_score: float


@dataclass
class _CacheStats:
    """Per-cycle counters for the solving-PR cache.

    Tracked at the module level so end-of-phase logging can report observable
    metrics: cache hits (free), misses (triggered a fetch), and fetch failures
    (issue not scored). Helps tune cache effectiveness and surface mirror
    flakiness without scraping logs for individual fetch warnings.
    """

    hits: int = 0
    misses: int = 0
    fetch_failures: int = 0


@dataclass
class _RepoIssueAcc:
    """Per-repository issue-discovery accumulator for one miner."""

    solved: int = 0
    valid_solved: int = 0
    closed: int = 0
    issue_token_score: float = 0.0
    fetch_failed: bool = False
    scored_issues: List[Issue] = field(default_factory=list)


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _should_include_issue(issue: MirrorIssue) -> bool:
    """Drop maintainer-discovered issues so repo maintainers cannot earn issue-
    discovery rewards in repos they maintain — mirrors the PR-side maintainer
    skip in ``oss_contributions/mirror/load.py``. Bypassed under DEV_MODE.
    """
    if not os.environ.get('DEV_MODE') and issue.author_association in MAINTAINER_ASSOCIATIONS:
        return False
    return True


async def run_issue_discovery(
    miner_evaluations: Dict[int, MinerEvaluation],
    mirror_repos: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    client: Optional[MirrorClient] = None,
    evaluation_cache: Optional[MinerEvaluationCache] = None,
) -> None:
    """Score issue discovery. Mutates miner_evaluations.

    For each miner, fetches their authored issues via the mirror and classifies
    each. Issues in repos not present in ``mirror_repos`` are filtered out
    client-side (mirror returns all tracked repos; the master list may be narrower).

    Depends on OSS scoring (``score_miner_prs``) having already run for
    this cycle — the cross-miner solving-PR cache is built by walking every
    miner's populated ``merged_prs``.
    """
    bt.logging.info('')
    bt.logging.info('=' * 50)
    bt.logging.info(f'Issue discovery | {len(mirror_repos)} repo(s)')
    bt.logging.info('=' * 50)

    if not mirror_repos:
        bt.logging.info('No scoring repos — issue discovery skipped')
        return

    client = client or MirrorClient()
    lookback_date = datetime.now(timezone.utc) - timedelta(days=PR_LOOKBACK_DAYS)
    issue_enabled_names: Set[str] = {
        name for name, config in mirror_repos.items() if config.issue_discovery_share > 0.0
    }

    solving_pr_cache: Dict[Tuple[str, int], CachedSolvingPR] = _build_solving_pr_cache(miner_evaluations)
    cache_stats = _CacheStats()
    bt.logging.info(
        f'Cross-miner solving-PR cache: {len(solving_pr_cache)} entries from '
        f'{sum(len(ev.merged_prs) for ev in miner_evaluations.values())} scored PRs'
    )

    skipped_no_gh = 0
    skipped_failed = 0
    fetch_errors = 0
    no_issues = 0
    cacheable_uids: Set[int] = set()

    # Phase 1: fetch each miner's issues.  The one-issue-per-PR rule is round-
    # global, so scoring is deferred until every miner's batch is in hand.
    pending: List[Tuple[MinerEvaluation, List[MirrorIssue], Dict[str, int]]] = []
    for uid, evaluation in miner_evaluations.items():
        if not evaluation.github_id or evaluation.github_id == '0':
            skipped_no_gh += 1
            continue
        if evaluation.failed_reason is not None:
            skipped_failed += 1
            continue

        try:
            response = await asyncio.to_thread(client.get_miner_issues, evaluation.github_id, since=lookback_date)
        except MirrorRequestError as e:
            bt.logging.warning(f'├─ UID {uid}: issue fetch failed ({e}) — skipped this miner')
            _restore_issue_discovery_from_cache(evaluation, evaluation_cache)
            fetch_errors += 1
            continue

        try:
            current_response = await asyncio.to_thread(client.get_miner_issues, evaluation.github_id)
        except MirrorRequestError as e:
            bt.logging.warning(f'├─ UID {uid}: open-issue count fetch failed ({e}) — skipped this miner')
            _restore_issue_discovery_from_cache(evaluation, evaluation_cache)
            fetch_errors += 1
            continue

        open_issue_count = _count_open_issues(current_response.issues, issue_enabled_names)
        filtered = [i for i in response.issues if i.repo_full_name in issue_enabled_names]
        if not filtered:
            _clear_issue_discovery_fields(evaluation)
            _apply_open_issue_counts(evaluation, open_counts)
            cacheable_uids.add(uid)
            no_issues += 1
            continue

        pending.append((evaluation, filtered, open_counts))

    canonical_pr_owners = _build_canonical_pr_owners(pending)
    for evaluation, filtered, open_counts in pending:
        complete = await _score_miner_issues(
            evaluation,
            filtered,
            mirror_repos,
            solving_pr_cache,
            cache_stats,
            client,
            programming_languages,
            token_config,
            open_counts=open_counts,
            canonical_pr_owners=canonical_pr_owners,
        )
        if complete:
            cacheable_uids.add(evaluation.uid)

    if evaluation_cache is not None:
        # Issue-discovery is not authoritative for the PR-side fields, so we
        # write through update_issue_discovery() rather than store(). The OSS
        # phase already stored a fresh entry for this UID (or restored from
        # cache on OSS failure); we only refresh the issue-discovery fields.
        for uid in cacheable_uids:
            evaluation_cache.update_issue_discovery(miner_evaluations[uid])

    bt.logging.info('')
    bt.logging.info(
        f'Issue discovery complete | {len(pending)} processed | {no_issues} no issues | '
        f'{fetch_errors} fetch errors | {skipped_no_gh} no github_id | {skipped_failed} prior OSS failure'
    )
    bt.logging.info(
        f'Solving-PR cache: {cache_stats.hits} hits | {cache_stats.misses} misses '
        f'({cache_stats.misses - cache_stats.fetch_failures} fetched OK, '
        f'{cache_stats.fetch_failures} fetch failures)'
    )


def _clear_issue_discovery_fields(evaluation: MinerEvaluation) -> None:
    """Reset issue-discovery aggregates after a successful fetch with no mirror issues."""
    evaluation.issue_discovery_score = 0.0
    evaluation.issue_token_score = 0.0
    evaluation.issue_credibility = 0.0
    evaluation.is_issue_eligible = False
    evaluation.total_solved_issues = 0
    evaluation.total_valid_solved_issues = 0
    evaluation.total_closed_issues = 0
    evaluation.total_open_issues = 0
    evaluation.issue_discovery_issues = []
    for repo_eval in evaluation.repo_evaluations.values():
        repo_eval.is_issue_eligible = False
        repo_eval.issue_credibility = 0.0
        repo_eval.issue_discovery_score = 0.0
        repo_eval.issue_token_score = 0.0
        repo_eval.total_solved_issues = 0
        repo_eval.total_valid_solved_issues = 0
        repo_eval.total_closed_issues = 0
        repo_eval.total_open_issues = 0


def _apply_open_issue_counts(evaluation: MinerEvaluation, open_counts: Dict[str, int]) -> None:
    """Record per-repo open-issue counts (and the round-level total) for a miner
    with no in-window issues to score."""
    for repo_name, count in open_counts.items():
        repo_eval = evaluation.repo_evaluations.get(repo_name)
        if repo_eval is None:
            repo_eval = RepoEvaluation(repository_full_name=repo_name)
            evaluation.repo_evaluations[repo_name] = repo_eval
        repo_eval.total_open_issues = count
    evaluation.total_open_issues = sum(open_counts.values())


def _copy_issue_discovery_fields(target: MinerEvaluation, source: MinerEvaluation) -> None:
    target.issue_discovery_score = source.issue_discovery_score
    target.issue_token_score = source.issue_token_score
    target.issue_credibility = source.issue_credibility
    target.is_issue_eligible = source.is_issue_eligible
    target.total_solved_issues = source.total_solved_issues
    target.total_valid_solved_issues = source.total_valid_solved_issues
    target.total_closed_issues = source.total_closed_issues
    target.total_open_issues = source.total_open_issues
    target.issue_discovery_issues = list(source.issue_discovery_issues)
    for repo_name, source_repo in source.repo_evaluations.items():
        target_repo = target.repo_evaluations.get(repo_name)
        if target_repo is None:
            target_repo = RepoEvaluation(repository_full_name=source_repo.repository_full_name)
            target.repo_evaluations[repo_name] = target_repo
        target_repo.copy_issue_discovery_from(source_repo)


def _restore_issue_discovery_from_cache(
    evaluation: MinerEvaluation,
    evaluation_cache: Optional[MinerEvaluationCache],
) -> bool:
    """Restore cached issue-discovery aggregates for a transient DAS fetch failure."""
    if evaluation_cache is None:
        return False

    cached = evaluation_cache.get(evaluation.uid, evaluation.hotkey, evaluation.github_id or '')
    if cached is None:
        bt.logging.warning(f'├─ UID {evaluation.uid}: no cached issue-discovery evaluation available')
        return False

    _copy_issue_discovery_fields(evaluation, cached)
    bt.logging.info(
        f'├─ UID {evaluation.uid}: restored cached issue discovery '
        f'(score={cached.issue_discovery_score:.2f}, solved={cached.total_solved_issues}, '
        f'valid={cached.total_valid_solved_issues})'
    )
    return True


def _build_canonical_pr_owners(
    pending: List[Tuple[MinerEvaluation, List[MirrorIssue], Dict[str, int]]],
) -> Dict[Tuple[str, int], Tuple[datetime, int, int]]:
    """Cross-miner one-issue-per-PR resolution.

    Returns ``(repo, pr_number) -> (created_at, issue_number, uid)`` for the
    earliest-created qualifying issue across all miners. Same-account issues
    (discoverer == solver) are excluded — they never claim the slot.
    ``_score_miner_issues`` matches issue markers against this map to
    gate scoring vs. credibility-only.
    """
    canonical: Dict[Tuple[str, int], Tuple[datetime, int, int]] = {}
    for evaluation, issues, _ in pending:
        for issue in issues:
            if _classify_issue(issue) != 'solved':
                continue
            sp = issue.solving_pr
            assert sp is not None  # _classify_issue guarantees a solving_pr
            if issue.author_github_id == sp.author_github_id:
                continue
            key = (issue.repo_full_name, sp.pr_number)
            marker = (issue.created_at or _FAR_FUTURE, issue.issue_number, evaluation.uid)
            existing = canonical.get(key)
            if existing is None or marker < existing:
                canonical[key] = marker
    return canonical


def _count_open_issues(issues: List[MirrorIssue], enabled_names: Set[str]) -> Dict[str, int]:
    """Count current OPEN issues per repository (enabled repos only)."""
    counts: Dict[str, int] = {}
    for issue in issues:
        if issue.repo_full_name in enabled_names and issue.state == 'OPEN':
            counts[issue.repo_full_name] = counts.get(issue.repo_full_name, 0) + 1
    return counts


def _build_solving_pr_cache(
    miner_evaluations: Dict[int, MinerEvaluation],
) -> Dict[Tuple[str, int], CachedSolvingPR]:
    """Pre-populate the cross-miner cache from already-scored mirror PRs.

    Any PR that was scored during OSS (in any miner's merged_prs) is
    keyed by (repo, pr_number) → CachedSolvingPR. Issue-discovery lookups hit
    this cache instead of re-fetching for miners' own PRs or other miners' PRs.
    """
    cache: Dict[Tuple[str, int], CachedSolvingPR] = {}
    for evaluation in miner_evaluations.values():
        for scored in evaluation.merged_prs:
            if scored.token_score < MIN_TOKEN_SCORE_FOR_BASE_SCORE:
                continue
            key = (scored.pr.repo_full_name, scored.pr.pr_number)
            if key in cache:
                continue  # first miner wins — values are the same PR's fields
            cache[key] = CachedSolvingPR(
                base_score=scored.base_score,
                token_score=scored.token_score,
            )
    return cache


async def _score_miner_issues(
    evaluation: MinerEvaluation,
    issues: List[MirrorIssue],
    mirror_repos: Dict[str, RepositoryConfig],
    solving_pr_cache: Dict[Tuple[str, int], CachedSolvingPR],
    cache_stats: _CacheStats,
    client: MirrorClient,
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    open_counts: Dict[str, int],
    canonical_pr_owners: Dict[Tuple[str, int], Tuple[datetime, int, int]],
) -> bool:
    """Classify + score one miner's mirror issues, per repository.

    Each repository gates issue discovery independently from only its own
    issues. ``open_counts`` maps repo -> the miner's current OPEN issue count
    there, independent of the issue-scoring lookback window.

    ``canonical_pr_owners`` enforces the cross-miner one-issue-per-PR rule:
    only the marker-matching issue scores, siblings count for credibility.

    Returns ``True`` when all required solving-PR score data was available, so
    the caller can safely refresh the cache with this complete issue snapshot.
    """
    repo_acc: Dict[str, _RepoIssueAcc] = {}

    issues_sorted = sorted(
        issues,
        key=lambda i: (
            i.repo_full_name,
            i.solved_by_pr or 0,
            i.created_at or _FAR_FUTURE,
        ),
    )

    for issue in issues_sorted:
        repo_config = mirror_repos.get(issue.repo_full_name)
        if repo_config is None:
            continue
        cfg = resolve_eligibility(repo_config.eligibility)
        acc = repo_acc.setdefault(issue.repo_full_name, _RepoIssueAcc())

        classification = _classify_issue(issue)
        if classification == 'not-solved-closed':
            acc.closed += 1
            continue
        if classification == 'ignore':
            continue

        # classification == 'solved'
        assert issue.solving_pr is not None  # _classify_issue guarantees
        solving_pr = issue.solving_pr

        acc.solved += 1

        # Resolve real base_score + token_score for the solving PR (cache or fetch)
        cached = await _resolve_solving_pr_score(
            issue,
            solving_pr,
            solving_pr_cache,
            cache_stats,
            client,
            programming_languages,
            token_config,
            repo_config,
        )
        if cached is None:
            # Fetch failed — issue still counts for solved/credibility but not scored.
            # Can't apply the valid-solved gate without a real token_score, so be
            # conservative and don't increment valid_solved.
            acc.fetch_failed = True
            bt.logging.debug(
                f'  issue #{issue.issue_number} ({issue.repo_full_name}): solver score unavailable '
                f'(fetch failed) — credibility only'
            )
            continue

        # Valid-solved gate: solving PR must meet the repo's token threshold.
        if cached.token_score >= cfg.min_token_score_for_valid_issue:
            acc.valid_solved += 1

        # Same-account: discoverer == solver gets credibility only, no score
        if issue.author_github_id == solving_pr.author_github_id:
            bt.logging.debug(
                f'  issue #{issue.issue_number} ({issue.repo_full_name}): same-account '
                f'(discoverer == solver {issue.author_github_id}) — credibility only'
            )
            continue

        pr_key = (issue.repo_full_name, solving_pr.pr_number)
        own_marker = (issue.created_at or _FAR_FUTURE, issue.issue_number, evaluation.uid)
        if canonical_pr_owners.get(pr_key) != own_marker:
            bt.logging.debug(
                f'  issue #{issue.issue_number} ({issue.repo_full_name}): one-issue-per-PR '
                f'(PR #{solving_pr.pr_number} canonical owner is a different issue) — credibility only'
            )
            continue

        # Quality gate: below-threshold solving PRs add credibility only, no
        # discovery score.
        if cached.token_score < cfg.min_token_score_for_valid_issue:
            bt.logging.debug(
                f'  issue #{issue.issue_number} ({issue.repo_full_name}): solving PR '
                f'#{solving_pr.pr_number} token_score {cached.token_score:.2f} < '
                f'{cfg.min_token_score_for_valid_issue} — credibility only'
            )
            continue

        adapted = _mirror_issue_for_scoring(issue, solving_pr, repo_config, base_score=cached.base_score)
        if adapted is None:
            continue

        acc.scored_issues.append(adapted)
        # issue_token_score accumulates per-solving-PR token scores for the
        # open-issue spam multiplier threshold calc.
        acc.issue_token_score += cached.token_score

    _finalize_repo_issue_scores(evaluation, repo_acc, open_counts, mirror_repos)
    return not any(acc.fetch_failed for acc in repo_acc.values())


def _finalize_repo_issue_scores(
    evaluation: MinerEvaluation,
    repo_acc: Dict[str, _RepoIssueAcc],
    open_counts: Dict[str, int],
    mirror_repos: Dict[str, RepositoryConfig],
) -> None:
    """Gate + score issue discovery per repository, then roll up the totals."""
    evaluation.issue_discovery_issues = []

    for repo_name in sorted(set(repo_acc) | set(open_counts)):
        repo_config = mirror_repos.get(repo_name)
        if repo_config is None:
            continue
        cfg = resolve_eligibility(repo_config.eligibility)
        acc = repo_acc.get(repo_name) or _RepoIssueAcc()
        open_count = open_counts.get(repo_name, 0)

        repo_eval = evaluation.repo_evaluations.get(repo_name)
        if repo_eval is None:
            repo_eval = RepoEvaluation(repository_full_name=repo_name)
            evaluation.repo_evaluations[repo_name] = repo_eval

        repo_eval.total_solved_issues = acc.solved
        repo_eval.total_valid_solved_issues = acc.valid_solved
        repo_eval.total_closed_issues = acc.closed
        repo_eval.total_open_issues = open_count
        repo_eval.issue_token_score = round(acc.issue_token_score, 2)

        is_eligible, credibility, reason = check_issue_eligibility(cfg, acc.solved, acc.valid_solved, acc.closed)
        repo_eval.is_issue_eligible = is_eligible
        repo_eval.issue_credibility = credibility

        if not is_eligible:
            repo_eval.issue_discovery_score = 0.0
            if acc.solved or acc.closed:
                bt.logging.info(
                    f'├─ {repo_name}: issue-ineligible ({reason}) | {acc.solved} solved '
                    f'({acc.valid_solved} valid) | {acc.closed} closed | {open_count} open'
                )
            continue

        spam_mult = calculate_open_issue_spam_multiplier(cfg, open_count, acc.issue_token_score)
        repo_score = 0.0
        for issue in acc.scored_issues:
            issue.discovery_credibility_multiplier = round(credibility, 2)
            issue.discovery_open_issue_spam_multiplier = spam_mult
            issue.discovery_earned_score = round(
                issue.discovery_base_score
                * issue.discovery_time_decay_multiplier
                * issue.discovery_review_quality_multiplier
                * issue.discovery_credibility_multiplier
                * issue.discovery_open_issue_spam_multiplier,
                2,
            )
            repo_score += issue.discovery_earned_score

        repo_eval.issue_discovery_score = round(repo_score, 2)
        evaluation.issue_discovery_issues.extend(acc.scored_issues)
        bt.logging.info(
            f'├─ {repo_name}: {acc.solved} solved ({acc.valid_solved} valid) | {acc.closed} closed | '
            f'{open_count} open | {len(acc.scored_issues)} scored | credibility={credibility:.2f} | '
            f'spam_mult={spam_mult:.1f} | discovery_score={repo_eval.issue_discovery_score:.2f}'
        )

    _roll_up_issue_totals(evaluation)


def _roll_up_issue_totals(evaluation: MinerEvaluation) -> None:
    """Roll per-repo issue-discovery results up into the round-level scalars."""
    repo_evals = list(evaluation.repo_evaluations.values())
    evaluation.total_solved_issues = sum(re.total_solved_issues for re in repo_evals)
    evaluation.total_valid_solved_issues = sum(re.total_valid_solved_issues for re in repo_evals)
    evaluation.total_closed_issues = sum(re.total_closed_issues for re in repo_evals)
    evaluation.total_open_issues = sum(re.total_open_issues for re in repo_evals)
    evaluation.issue_token_score = round(sum(re.issue_token_score for re in repo_evals), 2)
    evaluation.issue_discovery_score = round(sum(re.issue_discovery_score for re in repo_evals), 2)
    evaluation.is_issue_eligible = any(re.is_issue_eligible for re in repo_evals)
    evaluation.issue_credibility = max((re.issue_credibility for re in repo_evals), default=0.0)


async def _resolve_solving_pr_score(
    issue: MirrorIssue,
    solving_pr: MirrorSolvingPR,
    cache: Dict[Tuple[str, int], CachedSolvingPR],
    cache_stats: _CacheStats,
    client: MirrorClient,
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    repo_config: Optional[RepositoryConfig],
) -> Optional[CachedSolvingPR]:
    """Return base_score + token_score for the solving PR.

    Cache hit: returns immediately (case 1 = miner's own PR, case 2 = another
    miner's PR). Cache miss: fetches ``/pulls/:o/:r/:n/files`` and tokenizes;
    writes the result back into the cache. Returns None on fetch failure.

    Applies ``fixed_base_score`` repo override on cache miss (matching the
    override in ``score_mirror_pr``) so that ``discovery_base_score`` is the
    same regardless of solver identity.
    """
    key = (issue.repo_full_name, solving_pr.pr_number)
    if key in cache:
        cache_stats.hits += 1
        return cache[key]

    cache_stats.misses += 1
    try:
        files_response = await asyncio.to_thread(client.get_pr_files, issue.repo_full_name, solving_pr.pr_number)
    except MirrorRequestError as e:
        cache_stats.fetch_failures += 1
        bt.logging.warning(
            f'Mirror file fetch failed for solving PR #{solving_pr.pr_number} '
            f'({issue.repo_full_name}): {e} — issue #{issue.issue_number} not scored'
        )
        return None

    if not files_response.scoring_data_stored:
        cache_stats.fetch_failures += 1
        bt.logging.warning(
            f'Mirror scoring data unavailable for solving PR #{solving_pr.pr_number} '
            f'({issue.repo_full_name}): scoring_data_stored=False — '
            f'issue #{issue.issue_number} not scored'
        )
        return None

    file_changes, file_contents = mirror_files_to_legacy(
        issue.repo_full_name, solving_pr.pr_number, files_response.files
    )
    result = calculate_base_score_for_pr_files(file_changes, file_contents, programming_languages, token_config)
    base_score = (
        repo_config.fixed_base_score
        if repo_config is not None and repo_config.fixed_base_score is not None
        else result.base_score
    )
    cached = CachedSolvingPR(base_score=base_score, token_score=result.token_score)
    cache[key] = cached
    return cached


def _classify_issue(issue: MirrorIssue) -> str:
    """Return 'solved', 'not-solved-closed', or 'ignore' per anti-gaming gates.

    'ignore' = issue is open / transferred / has no scorable meaning at all.
    'not-solved-closed' = counts against credibility (closed but not solved).
    'solved' = counts toward solved metrics.

    Per-issue debug logs explain each classification so operators can debug
    "why didn't UID X get credit for issue Y?" without guessing.
    """
    if issue.is_transferred:
        bt.logging.debug(f'  issue #{issue.issue_number} ({issue.repo_full_name}): ignore (transferred)')
        return 'ignore'

    if issue.state != 'CLOSED':
        bt.logging.debug(
            f'  issue #{issue.issue_number} ({issue.repo_full_name}): ignore (state {issue.state}, not CLOSED)'
        )
        return 'ignore'

    if issue.state_reason != 'COMPLETED':
        bt.logging.debug(
            f'  issue #{issue.issue_number} ({issue.repo_full_name}): closed-not-solved '
            f'(state_reason={issue.state_reason}, need COMPLETED)'
        )
        return 'not-solved-closed'

    if not issue.solved_by_pr or not issue.solving_pr:
        bt.logging.debug(
            f'  issue #{issue.issue_number} ({issue.repo_full_name}): closed-not-solved (no solving PR linked)'
        )
        return 'not-solved-closed'

    sp = issue.solving_pr
    if sp.state != 'MERGED':
        bt.logging.debug(
            f'  issue #{issue.issue_number} ({issue.repo_full_name}): closed-not-solved '
            f'(solving PR #{sp.pr_number} state={sp.state}, not MERGED)'
        )
        return 'not-solved-closed'

    if sp.edited_after_merge:
        bt.logging.debug(
            f'  issue #{issue.issue_number} ({issue.repo_full_name}): closed-not-solved '
            f'(solving PR #{sp.pr_number} edited after merge — anti-spec-rewrite gate)'
        )
        return 'not-solved-closed'

    if issue.last_edited_at is not None and sp.merged_at is not None and issue.last_edited_at > sp.merged_at:
        bt.logging.debug(
            f'  issue #{issue.issue_number} ({issue.repo_full_name}): closed-not-solved '
            f'(issue body/title edited after solving PR #{sp.pr_number} merge — anti-spec-rewrite gate)'
        )
        return 'not-solved-closed'

    if not issue.author_github_id:
        bt.logging.debug(f'  issue #{issue.issue_number} ({issue.repo_full_name}): ignore (missing author_github_id)')
        return 'ignore'

    return 'solved'


def _mirror_issue_for_scoring(
    issue: MirrorIssue,
    solving_pr: MirrorSolvingPR,
    repo_config: RepositoryConfig,
    base_score: float,
) -> Optional[Issue]:
    """Build a legacy ``Issue`` with discovery_* fields populated.

    ``base_score`` is the real token-scored base for the solving PR, resolved
    by the caller via the cross-miner cache or on-demand fetch.

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

    adapted.discovery_base_score = base_score
    adapted.discovery_time_decay_multiplier = round(calculate_time_decay(solving_pr.merged_at), 2)
    adapted.discovery_review_quality_multiplier = round(
        calculate_issue_review_quality_multiplier(solving_pr.review_summary.maintainer_changes_requested_count),
        2,
    )

    return adapted
