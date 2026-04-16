# Entrius 2025
import time
from typing import Any, Dict, List, Optional

import bittensor as bt
import requests

from gittensor.constants import BASE_GITHUB_API_URL
from gittensor.utils.github_api.rest import make_headers

from . import graphql as gh_graphql
from gittensor.utils.models import PRInfo

# GraphQL fragment used by both issue submissions and solver detection.
_PR_TIMELINE_QUERY = """
query($owner: String!, $name: String!, $issueNumber: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $issueNumber) {
      timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], first: 50) {
        nodes {
          ... on CrossReferencedEvent {
            source {
              ... on PullRequest {
                number
                state
                title
                url
                merged
                mergedAt
                createdAt
                author { ... on User { databaseId login } }
                baseRepository { nameWithOwner }
                closingIssuesReferences(first: 20) {
                  nodes { number }
                }
                reviews(first: 1, states: APPROVED) { totalCount }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _resolve_pr_state(raw_state: str, merged: bool = False) -> str:
    """Normalize PR state to uppercase GraphQL-style values."""
    if merged:
        return 'MERGED'
    return (raw_state or '').upper() or 'OPEN'


def _search_issue_referencing_prs_graphql(
    repo: str, issue_number: int, token: str, open_only: bool = False
) -> List[PRInfo]:
    """Fetch PRs that reference an issue via GraphQL issue timeline cross-references."""
    if not token:
        return []
    if issue_number < 1 or '/' not in repo:
        return []
    owner, name = repo.split('/', 1)
    owner = owner.strip()
    name = name.strip()
    if not owner or not name:
        return []

    result = gh_graphql.execute_graphql_query(
        query=_PR_TIMELINE_QUERY,
        variables={'owner': owner, 'name': name, 'issueNumber': issue_number},
        token=token,
        max_attempts=3,
    )
    if not result:
        bt.logging.warning(f'GraphQL cross-reference query failed for {repo}#{issue_number}')
        return []

    timeline_nodes = (
        result.get('data', {}).get('repository', {}).get('issue', {}).get('timelineItems', {}).get('nodes', [])
    )

    out: List[PRInfo] = []
    for node in timeline_nodes:
        pr = node.get('source') or {}
        if not pr:
            continue

        base_repo = pr.get('baseRepository', {}).get('nameWithOwner', '')
        if base_repo.lower() != repo.lower():
            continue

        pr_number = pr.get('number')
        if not pr_number:
            continue

        state = _resolve_pr_state(pr.get('state', ''), merged=bool(pr.get('merged', False)))
        if open_only and state != 'OPEN':
            continue

        author = pr.get('author') or {}
        reviews = pr.get('reviews') or {}
        closing = pr.get('closingIssuesReferences', {}).get('nodes', [])
        closing_numbers = [n.get('number') for n in closing if n.get('number') is not None]

        pr_info: PRInfo = {
            'number': pr_number,
            'title': pr.get('title') or '',
            'author_login': author.get('login') or 'ghost',
            'author_id': author.get('databaseId'),
            'created_at': pr.get('createdAt') or '',
            'merged_at': pr.get('mergedAt') or None,
            'state': state,
            'url': pr.get('url') or '',
            'review_count': int(reviews.get('totalCount', 0) or 0),
            'closing_numbers': closing_numbers,
        }
        out.append(pr_info)

    return out


def _search_issue_referencing_prs_rest(
    repo: str, issue_number: int, token: Optional[str] = None, state: str = 'open'
) -> List[PRInfo]:
    """Search PRs via GitHub REST search API."""
    if issue_number < 1:
        return []

    if token:
        headers = make_headers(token)
    else:
        headers = {'Accept': 'application/vnd.github.v3+json'}
    headers.setdefault('User-Agent', 'gittensor-cli')

    state_clause = f' state:{state}' if state != 'all' else ''
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            resp = requests.get(
                f'{BASE_GITHUB_API_URL}/search/issues',
                params={'q': f'repo:{repo} type:pr{state_clause} {issue_number} in:title,body', 'per_page': '50'},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()

            out: List[PRInfo] = []
            for item in resp.json().get('items', []):
                number = item.get('number')
                if number is None:
                    continue
                user = item.get('user') or {}
                pr_info: PRInfo = {
                    'number': number,
                    'title': item.get('title') or '',
                    'author_login': user.get('login') or 'ghost',
                    'author_id': user.get('id'),
                    'created_at': item.get('created_at') or '',
                    'merged_at': None,
                    'state': _resolve_pr_state(item.get('state', 'open')),
                    'url': item.get('html_url') or '',
                    'review_count': 0,
                    'closing_numbers': [],
                }
                out.append(pr_info)
            return out

        except requests.exceptions.RequestException as e:
            if attempt < max_attempts - 1:
                backoff = 2 * (attempt + 1)
                bt.logging.warning(
                    f'REST search failed for {repo}#{issue_number} (attempt {attempt + 1}/{max_attempts}): {e}, '
                    f'retrying in {backoff}s...'
                )
                time.sleep(backoff)
            else:
                raise

    return []


def find_prs_for_issue(
    repo: str,
    issue_number: int,
    open_only: bool = True,
    token: Optional[str] = None,
) -> List[PRInfo]:
    """Cascading PR discovery: GraphQL -> authenticated REST -> unauthenticated REST."""
    rest_state = 'open' if open_only else 'all'

    if token:
        try:
            prs = _search_issue_referencing_prs_graphql(repo, issue_number, token, open_only=open_only)
            if prs:
                return prs
        except Exception as exc:
            bt.logging.debug(f'GraphQL PR fetch failed for {repo}#{issue_number}: {exc}')

    if token:
        try:
            prs = _search_issue_referencing_prs_rest(repo, issue_number, token=token, state=rest_state)
            if prs:
                return prs
        except Exception as exc:
            bt.logging.debug(f'Authenticated REST search failed for {repo}#{issue_number}: {exc}')

    try:
        prs = _search_issue_referencing_prs_rest(repo, issue_number, token=None, state=rest_state)
        if prs:
            return prs
    except Exception as exc:
        bt.logging.debug(f'Unauthenticated REST search failed for {repo}#{issue_number}: {exc}')

    return []


def find_solver_from_cross_references(repo: str, issue_number: int, token: str) -> tuple[Optional[int], Optional[int]]:
    """Resolve solver from cross-referenced PRs on the issue timeline.

    This uses ``_search_issue_referencing_prs_graphql`` and then narrows to PRs
    that are:
    - merged, and
    - explicitly closing ``issue_number``.

    If multiple candidates exist, the most recent ``merged_at`` is selected.

    Args:
        repo: Repository full name (``owner/repo``).
        issue_number: GitHub issue number.
        token: GitHub PAT used for GraphQL timeline access.

    Returns:
        Tuple ``(solver_github_id, pr_number)``. Either value may be ``None``
        when no valid closing PR is found.
    """
    prs = _search_issue_referencing_prs_graphql(repo, issue_number, token, open_only=False)
    merged = [p for p in prs if p.get('state') == 'MERGED' and issue_number in p.get('closing_numbers', [])]
    bt.logging.debug(f'Found {len(merged)} verified closing PRs via GraphQL for {repo}#{issue_number}')
    if not merged:
        return None, None

    if len(merged) > 1:
        bt.logging.warning(f'Multiple closing PRs found for {repo}#{issue_number}, selecting most recent.')
        for candidate in merged:
            bt.logging.debug(
                f'  PR#{candidate.get("number")}, solver_id={candidate.get("author_id")}, '
                f'merged_at={candidate.get("merged_at")}'
            )

    merged.sort(key=lambda p: p.get('merged_at') or '', reverse=True)
    best = merged[0]
    bt.logging.debug(
        f'Solver via GraphQL cross-reference: PR#{best.get("number")}, '
        f'solver_id={best.get("author_id")}, merged_at={best.get("merged_at")}'
    )
    return best.get('author_id'), best.get('number')


def find_solver_from_timeline(repo: str, issue_number: int, token: str) -> tuple:
    """Find the PR author who closed an issue.

    Uses GraphQL cross-reference analysis to find merged PRs that close the
    issue, with baseRepository validation and closingIssuesReferences check.

    Returns:
        (solver_github_id, pr_number) — either may be None if not found.
    """
    bt.logging.debug(f'Finding solver for {repo}#{issue_number}')
    return find_solver_from_cross_references(repo, issue_number, token)


def check_github_issue_closed(repo: str, issue_number: int, token: str) -> Optional[Dict[str, Any]]:
    """Check if a GitHub issue is closed and get the solving PR info.

    Args:
        repo: Repository full name (e.g., 'owner/repo')
        issue_number: GitHub issue number
        token: GitHub PAT for authentication

    Returns:
        Dict with 'is_closed', 'solver_github_id', 'pr_number' or None on error
    """
    headers = make_headers(token)

    try:
        response = requests.get(
            f'{BASE_GITHUB_API_URL}/repos/{repo}/issues/{issue_number}',
            headers=headers,
            timeout=15,
        )

        if response.status_code != 200:
            bt.logging.warning(f'GitHub API error for {repo}#{issue_number}: {response.status_code}')
            return None

        data = response.json()

        if data.get('state') != 'closed':
            return {'is_closed': False}

        solver_github_id, pr_number = find_solver_from_timeline(repo, issue_number, token)

        return {
            'is_closed': True,
            'solver_github_id': solver_github_id,
            'pr_number': pr_number,
        }

    except Exception as e:
        bt.logging.error(f'Error checking GitHub issue {repo}#{issue_number}: {e}')
        return None
