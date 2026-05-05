"""Issue discovery via the das-github-mirror service.

Replaces the timeline-scraping legacy path for mirror-enabled repos. Mirror
returns per-miner issues with an authoritative ``solved_by_pr`` and an inline
``solving_pr`` carrying everything needed to classify the discovery: author_id,
merge state, hours_since_merge, edited_after_merge, review_summary, shas.

Populates the existing ``MinerEvaluation`` issue-discovery fields directly
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

Base-score resolution uses a per-cycle cross-miner cache. Most solving PRs on
mirror-enabled repos will be miners' own PRs that OSS scoring already tokenized;
the cache pre-populates from every miner's ``mirror_merged_prs`` so those hits
require no HTTP. Non-miner-solved PRs (cache misses) trigger
``MirrorClient.get_pr_files`` + token scoring, with the result written back to
the cache so sibling discoveries benefit.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
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
from gittensor.validator.oss_contributions.mirror.adapters import mirror_files_to_legacy
from gittensor.validator.oss_contributions.mirror.scoring import (
    calculate_base_score_for_pr_files,
)
from gittensor.validator.utils.datetime_utils import calculate_time_decay, lookback_cutoff
from gittensor.validator.utils.load_weights import (
    LanguageConfig,
    RepositoryConfig,
    TokenConfig,
    resolve_repo_weight,
)


@dataclass
class CachedSolvingPR:
    """Per-cycle cached token-scoring output for a solving PR.

    Populated from miners' ``mirror_merged_prs`` before the per-miner loop
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


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


async def run_mirror_issue_discovery(
    miner_evaluations: Dict[int, MinerEvaluation],
    mirror_repos: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    client: Optional[MirrorClient] = None,
) -> None:
    """Score issue discovery for mirror-enabled repos. Mutates miner_evaluations.

    For each miner, fetches their authored issues via the mirror and classifies
    each. Issues in repos not present in ``mirror_repos`` are filtered out
    client-side (mirror returns all tracked repos; the config's mirror_enabled
    set may be narrower).

    Depends on OSS scoring (``score_mirror_miner_prs``) having already run for
    this cycle — the cross-miner solving-PR cache is built by walking every
    miner's populated ``mirror_merged_prs``.
    """
    bt.logging.info('')
    bt.logging.info('=' * 50)
    bt.logging.info(f'Issue discovery (mirror) | {len(mirror_repos)} mirror-enabled repo(s)')
    bt.logging.info('=' * 50)

    if not mirror_repos:
        bt.logging.info('No mirror-enabled repos — issue discovery skipped')
        return

    client = client or MirrorClient()
    lookback_date = lookback_cutoff(PR_LOOKBACK_DAYS)
    enabled_names: Set[str] = set(mirror_repos.keys())

    solving_pr_cache: Dict[Tuple[str, int], CachedSolvingPR] = _build_solving_pr_cache(miner_evaluations)
    cache_stats = _CacheStats()
    bt.logging.info(
        f'Cross-miner solving-PR cache: {len(solving_pr_cache)} entries from '
        f'{sum(len(ev.mirror_merged_prs) for ev in miner_evaluations.values())} scored mirror PRs'
    )

    skipped_no_gh = 0
    skipped_failed = 0
    fetch_errors = 0
    no_issues = 0

    # Phase 1: fetch each miner's issues.  The one-issue-per-PR rule is round-
    # global, so scoring is deferred until every miner's batch is in hand.
    pending: List[Tuple[MinerEvaluation, List[MirrorIssue], int]] = []
    for uid, evaluation in miner_evaluations.items():
        if not evaluation.github_id or evaluation.github_id == '0':
            skipped_no_gh += 1
            continue
        if evaluation.failed_reason is not None:
            skipped_failed += 1
            continue

        try:
            response = client.get_miner_issues(evaluation.github_id, since=lookback_date)
        except MirrorRequestError as e:
            bt.logging.warning(f'├─ UID {uid}: mirror issue fetch failed ({e}) — skipped this miner')
            fetch_errors += 1
            continue

        filtered = [i for i in response.issues if i.repo_full_name in enabled_names]
        if not filtered:
            no_issues += 1
            continue

        # Count this miner's currently-open issues across mirror-enabled repos
        # (within the lookback window). Used as the spam-multiplier signal and
        # also written to evaluation.total_open_issues so the DB row reflects
        # mirror-scoped state (the legacy GraphQL global open-issue count was
        # never the right signal for the gate).
        open_issue_count = sum(1 for i in filtered if i.state == 'OPEN')
        pending.append((evaluation, filtered, open_issue_count))

    canonical_pr_owners = _build_canonical_pr_owners(pending)
    for evaluation, filtered, open_issue_count in pending:
        _score_miner_mirror_issues(
            evaluation,
            filtered,
            mirror_repos,
            solving_pr_cache,
            cache_stats,
            client,
            programming_languages,
            token_config,
            open_issue_count=open_issue_count,
            canonical_pr_owners=canonical_pr_owners,
        )

    bt.logging.info('')
    bt.logging.info(
        f'Issue discovery complete | {len(pending)} processed | {no_issues} no mirror issues | '
        f'{fetch_errors} fetch errors | {skipped_no_gh} no github_id | {skipped_failed} prior OSS failure'
    )
    bt.logging.info(
        f'Solving-PR cache: {cache_stats.hits} hits | {cache_stats.misses} misses '
        f'({cache_stats.misses - cache_stats.fetch_failures} fetched OK, '
        f'{cache_stats.fetch_failures} fetch failures)'
    )


def _build_canonical_pr_owners(
    pending: List[Tuple[MinerEvaluation, List[MirrorIssue], int]],
) -> Dict[Tuple[str, int], Tuple[datetime, int, int]]:
    """Cross-miner one-issue-per-PR resolution (legacy parity, pre-#796).

    Returns ``(repo, pr_number) -> (created_at, issue_number, uid)`` for the
    earliest-created qualifying issue across all miners. Same-account issues
    (discoverer == solver) are excluded — they never claim the slot, mirroring
    legacy ``pr_scored.add`` ordering. ``_score_miner_mirror_issues`` matches
    issue markers against this map to gate scoring vs. credibility-only.
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


def _build_solving_pr_cache(
    miner_evaluations: Dict[int, MinerEvaluation],
) -> Dict[Tuple[str, int], CachedSolvingPR]:
    """Pre-populate the cross-miner cache from already-scored mirror PRs.

    Any PR that was scored during OSS (in any miner's mirror_merged_prs) is
    keyed by (repo, pr_number) → CachedSolvingPR. Issue-discovery lookups hit
    this cache instead of re-fetching for miners' own PRs or other miners' PRs.
    """
    cache: Dict[Tuple[str, int], CachedSolvingPR] = {}
    for evaluation in miner_evaluations.values():
        for scored in evaluation.mirror_merged_prs:
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


def _score_miner_mirror_issues(
    evaluation: MinerEvaluation,
    issues: List[MirrorIssue],
    mirror_repos: Dict[str, RepositoryConfig],
    solving_pr_cache: Dict[Tuple[str, int], CachedSolvingPR],
    cache_stats: _CacheStats,
    client: MirrorClient,
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    open_issue_count: int,
    canonical_pr_owners: Dict[Tuple[str, int], Tuple[datetime, int, int]],
) -> None:
    """Classify + score one miner's mirror issues, populate MinerEvaluation fields.

    ``open_issue_count`` is the miner's currently-OPEN issue count across
    mirror-enabled repos within the lookback window — the source-of-truth
    for the open-issue spam multiplier on the mirror path.

    ``canonical_pr_owners`` enforces the cross-miner one-issue-per-PR rule:
    only the marker-matching issue scores, siblings count for credibility.
    """
    solved_count = 0
    valid_solved_count = 0
    closed_count = 0
    issue_token_score = 0.0
    scored_issues: List[Issue] = []

    issues_sorted = sorted(
        issues,
        key=lambda i: (
            i.repo_full_name,
            i.solved_by_pr or 0,
            i.created_at or _FAR_FUTURE,
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
        assert issue.solving_pr is not None  # _classify_issue guarantees
        solving_pr = issue.solving_pr

        solved_count += 1

        # Resolve real base_score + token_score for the solving PR (cache or fetch)
        cached = _resolve_solving_pr_score(
            issue,
            solving_pr,
            solving_pr_cache,
            cache_stats,
            client,
            programming_languages,
            token_config,
        )
        if cached is None:
            # Fetch failed — issue still counts for solved/credibility but not scored.
            # Can't apply the valid-solved gate without a real token_score, so be
            # conservative and don't increment valid_solved_count.
            bt.logging.debug(
                f'  issue #{issue.issue_number} ({issue.repo_full_name}): solver score unavailable '
                f'(fetch failed) — credibility only'
            )
            continue

        # Valid-solved gate (legacy parity): solving PR must meet the token threshold.
        if cached.token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE:
            valid_solved_count += 1

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

        repo_config = mirror_repos.get(issue.repo_full_name)
        if repo_config is None:
            continue

        # Quality gate — matches legacy issue-discovery behavior: below-threshold
        # solving PRs add credibility only, no discovery score.
        if cached.token_score < MIN_TOKEN_SCORE_FOR_BASE_SCORE:
            bt.logging.debug(
                f'  issue #{issue.issue_number} ({issue.repo_full_name}): solving PR '
                f'#{solving_pr.pr_number} token_score {cached.token_score:.2f} < '
                f'{MIN_TOKEN_SCORE_FOR_BASE_SCORE} — credibility only'
            )
            continue

        adapted = _mirror_issue_for_scoring(issue, solving_pr, repo_config, base_score=cached.base_score)
        if adapted is None:
            continue

        scored_issues.append(adapted)
        # Legacy parity: issue_token_score accumulates per-solving-PR token scores
        # for the open-issue spam multiplier threshold calc.
        issue_token_score += cached.token_score

    # All issue-discovery fields are now mirror-only writers (legacy issue
    # discovery has been removed), so simple assignment — no need to merge
    # with prior values.
    evaluation.total_solved_issues = solved_count
    evaluation.total_valid_solved_issues = valid_solved_count
    evaluation.total_closed_issues = closed_count
    evaluation.total_open_issues = open_issue_count
    evaluation.issue_token_score = round(issue_token_score, 2)

    is_eligible, credibility, reason = check_issue_eligibility(solved_count, valid_solved_count, closed_count)
    evaluation.is_issue_eligible = is_eligible
    evaluation.issue_credibility = credibility

    if not is_eligible:
        bt.logging.info(
            f'├─ UID {evaluation.uid}: ineligible ({reason}) | '
            f'{solved_count} solved ({valid_solved_count} valid) | {closed_count} closed | '
            f'{open_issue_count} open'
        )
        return

    spam_mult = calculate_open_issue_spam_multiplier(open_issue_count, issue_token_score)

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

    evaluation.issue_discovery_score = round(total_discovery_score, 2)

    bt.logging.info(
        f'├─ UID {evaluation.uid}: {solved_count} solved ({valid_solved_count} valid) | '
        f'{closed_count} closed | {open_issue_count} open | {len(scored_issues)} scored | '
        f'credibility={credibility:.2f} | spam_mult={spam_mult:.1f} | '
        f'mirror_score={total_discovery_score:.2f}'
    )


def _resolve_solving_pr_score(
    issue: MirrorIssue,
    solving_pr: MirrorSolvingPR,
    cache: Dict[Tuple[str, int], CachedSolvingPR],
    cache_stats: _CacheStats,
    client: MirrorClient,
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
) -> Optional[CachedSolvingPR]:
    """Return base_score + token_score for the solving PR.

    Cache hit: returns immediately (case 1 = miner's own PR, case 2 = another
    miner's PR). Cache miss: fetches ``/pulls/:o/:r/:n/files`` and tokenizes;
    writes the result back into the cache. Returns None on fetch failure.
    """
    key = (issue.repo_full_name, solving_pr.pr_number)
    if key in cache:
        cache_stats.hits += 1
        return cache[key]

    cache_stats.misses += 1
    try:
        files_response = client.get_pr_files(issue.repo_full_name, solving_pr.pr_number)
    except MirrorRequestError as e:
        cache_stats.fetch_failures += 1
        bt.logging.warning(
            f'Mirror file fetch failed for solving PR #{solving_pr.pr_number} '
            f'({issue.repo_full_name}): {e} — issue #{issue.issue_number} not scored'
        )
        return None

    file_changes, file_contents = mirror_files_to_legacy(
        issue.repo_full_name, solving_pr.pr_number, files_response.files
    )
    result = calculate_base_score_for_pr_files(file_changes, file_contents, programming_languages, token_config)
    cached = CachedSolvingPR(base_score=result.base_score, token_score=result.token_score)
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
    adapted.discovery_repo_weight_multiplier = resolve_repo_weight(repo_config)
    adapted.discovery_time_decay_multiplier = round(calculate_time_decay(solving_pr.merged_at), 2)
    adapted.discovery_review_quality_multiplier = round(
        calculate_issue_review_quality_multiplier(solving_pr.review_summary.maintainer_changes_requested_count),
        2,
    )

    return adapted
