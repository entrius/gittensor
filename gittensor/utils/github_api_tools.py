# Entrius 2025
import fnmatch
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import bittensor as bt
import requests

from gittensor.constants import (
    BASE_GITHUB_API_URL,
    GITHUB_HTTP_TIMEOUT_SECONDS,
)
from gittensor.utils.models import PRInfo
from gittensor.utils.utils import backoff_seconds


class GitHubIdentityStatus(Enum):
    VALID = 'VALID'
    INVALID_AUTH = 'INVALID_AUTH'
    TRANSIENT_FAILURE = 'TRANSIENT_FAILURE'


@dataclass(frozen=True)
class GitHubIdentityResult:
    github_id: Optional[str]
    status: GitHubIdentityStatus


def branch_matches_pattern(branch_name: str, patterns: List[str]) -> bool:
    """Check if a branch name matches any pattern in the list.

    Args:
        branch_name (str): Branch name to check.
        patterns (List[str]): Wildcard patterns to match (for example, "*-dev").

    Returns:
        bool: True if the branch name matches any of the patterns, otherwise False.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(branch_name, pattern):
            return True
    return False


def make_headers(token: str) -> Dict[str, str]:
    """Build standard GitHub HTTP headers for a PAT.

    Args:
        token (str): Github pat
    Returns:
        Dict[str, str]: Mapping of HTTP header names to values.
    """
    return {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
    }


def make_graphql_headers(token: str) -> Dict[str, str]:
    """Build GitHub GraphQL headers for a PAT."""
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def make_anonymous_headers() -> Dict[str, str]:
    """Build GitHub HTTP headers for unauthenticated calls."""
    return {'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'gittensor-cli'}


def get_session(token: str) -> requests.Session:
    """Return a fresh requests.Session preconfigured with the appropriate headers."""
    session = requests.Session()
    session.headers.update(make_headers(token) if token else make_anonymous_headers())
    return session


def _is_rate_limited_response(response: requests.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get('x-ratelimit-remaining') == '0':
        return True
    try:
        message = str(response.json().get('message', '')).lower()
    except Exception:
        message = getattr(response, 'text', '').lower()
    return 'rate limit' in message


def get_github_identity(token: str) -> GitHubIdentityResult:
    """Get GitHub numeric user id and whether lookup failure is cacheable.

    Args:
        token (str): GitHub personal access token.

    Returns:
        GitHubIdentityResult: Numeric user id on success, invalid auth for
            permanent auth failures, or transient failure when GitHub/user JSON
            could not be reached after retries.
    """
    if not token:
        return GitHubIdentityResult(None, GitHubIdentityStatus.INVALID_AUTH)

    session = get_session(token)

    # Retry logic for timeout issues
    for attempt in range(6):
        try:
            response = session.get(f'{BASE_GITHUB_API_URL}/user', timeout=GITHUB_HTTP_TIMEOUT_SECONDS)
            if response.status_code == 200:
                try:
                    user_data: Dict[str, Any] = response.json()
                except Exception as e:
                    bt.logging.warning(f'Failed to parse GitHub /user JSON response: {e}')
                    if attempt < 5:
                        time.sleep(2)
                        continue
                    return GitHubIdentityResult(None, GitHubIdentityStatus.TRANSIENT_FAILURE)

                user_id = user_data.get('id')
                if user_id is not None:
                    return GitHubIdentityResult(str(user_id), GitHubIdentityStatus.VALID)

                bt.logging.warning(f'GitHub /user response missing id (attempt {attempt + 1}/6)')
                if attempt < 5:
                    time.sleep(2)
                    continue
                return GitHubIdentityResult(None, GitHubIdentityStatus.TRANSIENT_FAILURE)

            if response.status_code == 408 or _is_rate_limited_response(response):
                bt.logging.warning(
                    f'GitHub /user request failed with status {response.status_code} (attempt {attempt + 1}/6)'
                )
                if attempt < 5:
                    time.sleep(2)
                    continue
                return GitHubIdentityResult(None, GitHubIdentityStatus.TRANSIENT_FAILURE)

            if 400 <= response.status_code < 500:
                bt.logging.warning(f'GitHub /user auth failed with status {response.status_code}')
                return GitHubIdentityResult(None, GitHubIdentityStatus.INVALID_AUTH)

            bt.logging.warning(
                f'GitHub /user request failed with status {response.status_code} (attempt {attempt + 1}/6)'
            )
            if attempt < 5:
                time.sleep(2)

        except Exception as e:
            bt.logging.warning(f'Could not fetch GitHub user (attempt {attempt + 1}/6): {e}')
            if attempt < 5:  # Don't sleep on last attempt
                time.sleep(2)

    return GitHubIdentityResult(None, GitHubIdentityStatus.TRANSIENT_FAILURE)


# GraphQL fragment used by both issue submissions and solver detection.
_PR_TIMELINE_QUERY = """
query($owner: String!, $name: String!, $issueNumber: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $issueNumber) {
      timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 50) {
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
                  nodes {
                    number
                    repository { nameWithOwner }
                  }
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


def _closing_issue_numbers_for_repo(closing_ref: Optional[Dict[str, Any]], repo: str) -> List[int]:
    """Return only closing issue numbers whose GraphQL repository matches ``repo``."""
    target_repo = repo.lower()
    closing_numbers: List[int] = []
    for node in (closing_ref or {}).get('nodes') or []:
        if not node:
            continue
        issue_number = node.get('number')
        issue_repo = ((node.get('repository') or {}).get('nameWithOwner') or '').lower()
        if issue_number is not None and issue_repo == target_repo:
            closing_numbers.append(issue_number)
    return closing_numbers


def _search_issue_referencing_prs_graphql(
    repo: str, issue_number: int, token: str, open_only: bool = False
) -> Optional[List[PRInfo]]:
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
    target_repo = f'{owner}/{name}'.lower()

    result = execute_graphql_query(
        query=_PR_TIMELINE_QUERY,
        variables={'owner': owner, 'name': name, 'issueNumber': issue_number},
        token=token,
        max_attempts=3,
    )
    if result is None:
        bt.logging.warning(f'GraphQL cross-reference query failed for {repo}#{issue_number}')
        return None

    errors = result.get('errors')
    if errors:
        bt.logging.warning(f'GraphQL cross-reference query returned errors for {repo}#{issue_number}: {errors}')
        return None

    issue_data = result.get('data', {}).get('repository', {}).get('issue')
    if issue_data is None:
        bt.logging.warning(f'GraphQL cross-reference response missing issue data for {repo}#{issue_number}')
        return None

    timeline_nodes = issue_data.get('timelineItems', {}).get('nodes', [])

    out: List[PRInfo] = []
    for node in timeline_nodes:
        pr = node.get('source') or {}
        if not pr:
            continue

        base_repo = pr.get('baseRepository', {}).get('nameWithOwner', '')
        if base_repo.lower() != target_repo:
            continue

        pr_number = pr.get('number')
        if not pr_number:
            continue

        state = 'MERGED' if pr.get('merged') else (pr.get('state') or '').upper() or 'OPEN'
        if open_only and state != 'OPEN':
            continue

        author = pr.get('author') or {}
        reviews = pr.get('reviews') or {}
        closing_numbers = _closing_issue_numbers_for_repo(pr.get('closingIssuesReferences'), target_repo)

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


def find_prs_for_issue(
    repo: str,
    issue_number: int,
    open_only: bool = True,
    token: Optional[str] = None,
) -> List[PRInfo]:
    """Find PRs that reference an issue via GraphQL cross-reference data."""
    if token:
        try:
            prs = _search_issue_referencing_prs_graphql(repo, issue_number, token, open_only=open_only)
            return prs or []
        except Exception as exc:
            bt.logging.debug(f'GraphQL PR fetch failed for {repo}#{issue_number}: {exc}')

    return []


def execute_graphql_query(
    query: str,
    variables: Dict[str, Any],
    token: str,
    max_attempts: int = 8,
    timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Execute a GraphQL query with retry logic and backoff.

    Args:
        query: The GraphQL query string
        variables: Query variables
        token: GitHub PAT for authentication
        max_attempts: Maximum retry attempts (default 6)
        timeout: Request timeout in seconds (default 30)

    Returns:
        Parsed JSON response data, or None if all attempts failed
    """
    session = get_session(token)
    headers = make_graphql_headers(token)

    for attempt in range(max_attempts):
        try:
            response = session.post(
                f'{BASE_GITHUB_API_URL}/graphql',
                headers=headers,
                json={'query': query, 'variables': variables},
                timeout=timeout,
            )

            if response.status_code == 200:
                return response.json()

            # Retry on failure
            if attempt < (max_attempts - 1):
                backoff_delay = backoff_seconds(attempt)
                bt.logging.warning(
                    f'GraphQL request failed with status {response.status_code} '
                    f'(attempt {attempt + 1}/{max_attempts}), retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(
                    f'GraphQL request failed with status {response.status_code} '
                    f'after {max_attempts} attempts: {response.text}'
                )

        except requests.exceptions.RequestException as e:
            if attempt < (max_attempts - 1):
                backoff_delay = backoff_seconds(attempt)
                bt.logging.warning(
                    f'GraphQL request exception (attempt {attempt + 1}/{max_attempts}), '
                    f'retrying in {backoff_delay}s: {e}'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(f'GraphQL request failed after {max_attempts} attempts: {e}')

    return None


def find_solver_from_cross_references(
    repo: str, issue_number: int, token: str
) -> Optional[tuple[Optional[int], Optional[int]]]:
    """Resolve solver from cross-referenced PRs on the issue timeline.

    This uses ``_search_issue_referencing_prs_graphql`` and then narrows to PRs
    that are:
    - merged, and
    - explicitly closing ``issue_number``.

    If multiple candidates exist, the earliest ``merged_at`` is selected, since
    GitHub closes an issue on the first merged PR that triggers the close; later
    PRs declaring "Closes #X" in their body still appear in the timeline with
    ``closingIssuesReferences`` populated even though they did not actually
    close the issue. PR ``number`` is used as a deterministic tiebreaker.

    Returns:
        ``None`` when lookup fails and should be retried later. Otherwise a
        tuple ``(solver_github_id, pr_number)`` where either value may be
        ``None`` when no valid closing PR is found.
    """
    prs = _search_issue_referencing_prs_graphql(repo, issue_number, token, open_only=False)
    if prs is None:
        return None

    merged = [p for p in prs if p.get('state') == 'MERGED' and issue_number in p.get('closing_numbers', [])]
    bt.logging.debug(f'Found {len(merged)} verified closing PRs via GraphQL for {repo}#{issue_number}')
    if not merged:
        return None, None

    if len(merged) > 1:
        bt.logging.warning(f'Multiple closing PRs found for {repo}#{issue_number}, selecting earliest-merged.')
        for candidate in merged:
            bt.logging.debug(
                f'  PR#{candidate.get("number")}, solver_id={candidate.get("author_id")}, '
                f'merged_at={candidate.get("merged_at")}'
            )

    merged.sort(key=lambda p: (p.get('merged_at') or '', p.get('number') or 0))
    best = merged[0]
    bt.logging.debug(
        f'Solver via GraphQL cross-reference: PR#{best.get("number")}, '
        f'solver_id={best.get("author_id")}, merged_at={best.get("merged_at")}'
    )
    return best.get('author_id'), best.get('number')


def check_github_issue_closed(repo: str, issue_number: int, token: str) -> Optional[Dict[str, Any]]:
    """Check if a GitHub issue is closed and get the solving PR info.

    Args:
        repo: Repository full name (e.g., 'owner/repo')
        issue_number: GitHub issue number
        token: GitHub PAT for authentication

    Returns:
        Dict with 'is_closed', 'solver_github_id', 'pr_number', 'solver_lookup_failed' or None on error
    """
    session = get_session(token)

    try:
        response = session.get(
            f'{BASE_GITHUB_API_URL}/repos/{repo}/issues/{issue_number}',
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )

        if response.status_code != 200:
            bt.logging.warning(f'GitHub API error for {repo}#{issue_number}: {response.status_code}')
            return None

        data = response.json()

        if data.get('state') != 'closed':
            return {'is_closed': False}

        state_reason = data.get('state_reason')
        if not isinstance(state_reason, str) or state_reason.strip().lower() != 'completed':
            bt.logging.info(
                f'Issue closed on GitHub but not completed: {repo}#{issue_number} state_reason={state_reason}'
            )
            return {
                'is_closed': True,
                'solver_github_id': None,
                'pr_number': None,
                'solver_lookup_failed': False,
            }

        bt.logging.debug(f'Finding solver for {repo}#{issue_number}')
        solver_lookup = find_solver_from_cross_references(repo, issue_number, token)
        if solver_lookup is None:
            bt.logging.warning(f'Solver lookup failed for {repo}#{issue_number}')
            solver_lookup_failed = True
            solver_github_id = None
            pr_number = None
        else:
            solver_lookup_failed = False
            solver_github_id, pr_number = solver_lookup

        return {
            'is_closed': True,
            'solver_github_id': solver_github_id,
            'pr_number': pr_number,
            'solver_lookup_failed': solver_lookup_failed,
        }

    except Exception as e:
        bt.logging.error(f'Error checking GitHub issue {repo}#{issue_number}: {e}')
        return None


@dataclass
class FileContentPair:
    """Holds both old (base) and new (head) content for a file."""

    old_content: Optional[str]  # None for new files
    new_content: Optional[str]  # None for deleted files
