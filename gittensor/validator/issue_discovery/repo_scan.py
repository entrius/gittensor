# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Repo-centric closed issue scan for issue discovery.

Detects miner-authored closed issues that aren't linked to any miner's merged PR:
- Case 2: Solved by a non-miner PR → positive credibility (no score)
- Case 3: Closed without any PR → negative credibility

Uses the validator PAT for all API calls. Rate-limited by per-repo and global caps.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import bittensor as bt
import requests

from gittensor.classes import Issue, MinerEvaluation
from gittensor.constants import (
    BASE_GITHUB_API_URL,
    PR_LOOKBACK_DAYS,
    REPO_SCAN_CONCURRENCY,
    REPO_SCAN_GLOBAL_CAP,
    REPO_SCAN_PER_REPO_CAP,
)
from gittensor.utils.github_api_tools import find_solver_from_cross_references
from gittensor.validator.utils.datetime_utils import parse_github_iso_to_utc
from gittensor.validator.utils.load_weights import RepositoryConfig


async def scan_closed_issues(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    validator_pat: str,
) -> Dict[str, List[Issue]]:
    """Scan tracked repos for miner-authored closed issues not linked to miner PRs.

    Args:
        miner_evaluations: All miner evaluations (post-OSS scoring)
        master_repositories: Tracked repositories
        validator_pat: Validator's GitHub PAT for API calls

    Returns:
        Dict[github_id → List[Issue]] with issues classified for credibility counting.
        Issues with closed_at set = solved by non-miner PR (case 2, positive credibility).
        Issues without closed_at = closed without PR (case 3, negative credibility).
    """
    if not validator_pat:
        bt.logging.info('Issue discovery scan: no validator PAT, skipping')
        return {}

    # Build miner github_id set
    miner_github_ids: Set[str] = set()
    for evaluation in miner_evaluations.values():
        if evaluation.github_id and evaluation.github_id != '0':
            miner_github_ids.add(evaluation.github_id)

    if not miner_github_ids:
        return {}

    # Build set of already-known issues from PR data (skip these in scan)
    known_issues: Set[Tuple[str, int]] = set()  # (repo, issue_number)
    for evaluation in miner_evaluations.values():
        for pr in evaluation.merged_pull_requests + evaluation.open_pull_requests + evaluation.closed_pull_requests:
            if pr.issues:
                for issue in pr.issues:
                    known_issues.add((issue.repository_full_name, issue.number))

    bt.logging.info(
        f'Issue discovery scan: {len(miner_github_ids)} miners, '
        f'{len(known_issues)} known issues, {len(master_repositories)} repos to scan'
    )

    lookback_date = (datetime.now(timezone.utc) - timedelta(days=PR_LOOKBACK_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Sort repos by weight descending (high-value repos first)
    sorted_repos = sorted(master_repositories.items(), key=lambda x: x[1].weight, reverse=True)

    # Filter out inactive repos
    active_repos = [(name, config) for name, config in sorted_repos if config.inactive_at is None]

    result: Dict[str, List[Issue]] = {}
    global_lookup_count = 0

    for i, (repo_name, repo_config) in enumerate(active_repos, 1):
        if global_lookup_count >= REPO_SCAN_GLOBAL_CAP:
            bt.logging.info(f'Issue discovery scan: global cap ({REPO_SCAN_GLOBAL_CAP}) reached, stopping')
            break

        remaining_global = REPO_SCAN_GLOBAL_CAP - global_lookup_count
        lookups_done = await _scan_repo(
            repo_name,
            lookback_date,
            validator_pat,
            miner_github_ids,
            known_issues,
            result,
            min(REPO_SCAN_PER_REPO_CAP, remaining_global),
        )
        global_lookup_count += lookups_done

        if i % 25 == 0:
            bt.logging.info(
                f'Issue discovery scan: {i}/{len(active_repos)} repos scanned, {global_lookup_count} lookups'
            )

    total_issues = sum(len(issues) for issues in result.values())
    bt.logging.info(
        f'Issue discovery scan complete: {total_issues} issues found, {global_lookup_count} solver lookups used'
    )

    return result


def _extract_state_reason(issue_raw: dict) -> Optional[str]:
    """Normalize REST ``state_reason`` to uppercase or None for legacy data."""
    raw = issue_raw.get('state_reason')
    if not raw:
        return None
    return raw.upper()


def _build_scan_issue(
    issue_raw: dict,
    repo_name: str,
    pr_number: int = 0,
    closed_at: Optional[datetime] = None,
) -> Tuple[str, Issue]:
    """Build an Issue object from REST data and return (author_github_id, issue).

    Centralises the conversion from raw REST dict to Issue so both the solver-lookup
    path and the direct-classification path share the same construction logic.
    """
    user = issue_raw.get('user') or {}
    author_github_id = str(user.get('id', ''))

    issue = Issue(
        number=issue_raw['number'],
        pr_number=pr_number,
        repository_full_name=repo_name,
        title=issue_raw.get('title', ''),
        created_at=_parse_iso(issue_raw.get('created_at')),
        closed_at=closed_at,
        author_login=user.get('login'),
        author_github_id=author_github_id,
        state='CLOSED',
        state_reason=_extract_state_reason(issue_raw),
    )

    return author_github_id, issue


async def _scan_repo(
    repo_name: str,
    lookback_date: str,
    validator_pat: str,
    miner_github_ids: Set[str],
    known_issues: Set[Tuple[str, int]],
    result: Dict[str, List[Issue]],
    lookup_cap: int,
) -> int:
    """Scan a single repo's closed issues. Returns number of solver lookups performed."""

    closed_issues = _fetch_closed_issues(repo_name, lookback_date, validator_pat)
    if not closed_issues:
        return 0

    # GitHub REST ``since`` filters by updated_at, not closed_at.
    # Pre-parse the cutoff once so we can drop stale issues inside the loop.
    lookback_dt = datetime.fromisoformat(lookback_date.replace('Z', '+00:00'))

    # -------------------------------------------------------------------
    # Phase 1: filter to miner-authored issues not already known, then
    # partition by state_reason.  Non-COMPLETED issues always route to
    # closed_count downstream (regardless of solver), so performing an
    # expensive GraphQL solver lookup for them wastes the global budget.
    # -------------------------------------------------------------------
    completed_unmatched: List[dict] = []
    non_completed_unmatched: List[dict] = []
    stale_count = 0

    for issue_raw in closed_issues:
        user = issue_raw.get('user') or {}
        author_id = str(user.get('id', ''))
        issue_number = issue_raw.get('number')

        if not author_id or author_id not in miner_github_ids:
            continue
        if (repo_name, issue_number) in known_issues:
            continue
        # Skip pull requests (GitHub REST /issues endpoint includes PRs)
        if 'pull_request' in issue_raw:
            continue
        # Drop issues whose closed_at falls outside the lookback window.
        closed_at = _parse_iso(issue_raw.get('closed_at'))
        if closed_at is None or closed_at < lookback_dt:
            stale_count += 1
            continue

        state_reason = _extract_state_reason(issue_raw)
        if state_reason == 'COMPLETED':
            completed_unmatched.append(issue_raw)
        else:
            non_completed_unmatched.append(issue_raw)

    if stale_count:
        bt.logging.debug(f'{repo_name}: dropped {stale_count} issues closed before lookback window')

    total_unmatched = len(completed_unmatched) + len(non_completed_unmatched)
    if total_unmatched == 0:
        return 0

    # -------------------------------------------------------------------
    # Phase 2: classify non-COMPLETED issues directly (no solver needed).
    # These always end up as closed_count in _merge_scan_issues, so we
    # can skip the GraphQL call entirely and save the budget.
    # -------------------------------------------------------------------
    for issue_raw in non_completed_unmatched:
        author_github_id, issue = _build_scan_issue(issue_raw, repo_name, closed_at=None)
        result.setdefault(author_github_id, []).append(issue)

    if non_completed_unmatched:
        bt.logging.info(
            f'{repo_name}: classified {len(non_completed_unmatched)} non-COMPLETED issues directly '
            f'(saved {len(non_completed_unmatched)} solver lookups)'
        )

    if not completed_unmatched:
        bt.logging.debug(f'{repo_name}: {total_unmatched} unmatched issues, 0 COMPLETED — no lookups needed')
        return 0

    bt.logging.info(
        f'{repo_name}: {len(completed_unmatched)} COMPLETED issues need solver lookup '
        f'(out of {total_unmatched} total unmatched)'
    )

    # -------------------------------------------------------------------
    # Phase 3: resolve COMPLETED issues with solver lookups (capped).
    # Only these issues need the expensive GraphQL call to distinguish
    # "solved by non-miner PR" (positive cred) from "closed without PR"
    # (negative cred).
    # -------------------------------------------------------------------
    capped = completed_unmatched[:lookup_cap]
    semaphore = asyncio.Semaphore(REPO_SCAN_CONCURRENCY)

    async def _lookup(issue_raw: dict) -> Tuple[dict, Optional[int], Optional[int]]:
        async with semaphore:
            solver_id, pr_number = await asyncio.to_thread(
                find_solver_from_cross_references,
                repo_name,
                issue_raw['number'],
                validator_pat,
            )
            return issue_raw, solver_id, pr_number

    tasks = [_lookup(issue_raw) for issue_raw in capped]
    resolved = await asyncio.gather(*tasks, return_exceptions=True)

    for item in resolved:
        if isinstance(item, BaseException):
            bt.logging.warning(f'Solver lookup error in {repo_name}: {item}')
            continue

        assert isinstance(item, tuple)
        issue_raw, solver_id, pr_number = item

        if solver_id is not None:
            # Case 2: solved by non-miner PR → positive credibility
            closed_at_dt = _parse_iso(issue_raw.get('closed_at'))
        else:
            # Case 3: closed without PR → negative credibility
            closed_at_dt = None

        author_github_id, issue = _build_scan_issue(
            issue_raw, repo_name, pr_number=pr_number or 0, closed_at=closed_at_dt
        )
        result.setdefault(author_github_id, []).append(issue)

    return len(capped)


def _fetch_closed_issues(repo_name: str, since: str, token: str) -> List[dict]:
    """Fetch closed issues from a repo via REST API with pagination."""
    headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
    all_issues: List[dict] = []
    page = 1

    while True:
        try:
            response = requests.get(
                f'{BASE_GITHUB_API_URL}/repos/{repo_name}/issues',
                params={'state': 'closed', 'since': since, 'per_page': 100, 'page': page},
                headers=headers,
                timeout=30,
            )
            if response.status_code in (404, 422):
                bt.logging.debug(f'Issue scan {repo_name} page {page}: HTTP {response.status_code}')
                break
            if response.status_code != 200:
                bt.logging.warning(f'Issue scan {repo_name} page {page}: HTTP {response.status_code}')
                break

            issues = response.json()
            if not issues:
                break

            all_issues.extend(issues)
            page += 1

            # Safety: don't paginate forever
            if page > 100:
                bt.logging.warning(f'Issue scan {repo_name}: hit 100-page limit')
                break

        except requests.RequestException as e:
            bt.logging.warning(f'Issue scan {repo_name} page {page}: {e}')
            break

    return all_issues


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string to UTC datetime."""
    if not value:
        return None
    try:
        return parse_github_iso_to_utc(value)
    except (ValueError, AttributeError):
        return None
