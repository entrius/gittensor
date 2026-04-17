# Entrius 2025
import base64
import fnmatch
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from gittensor.utils.utils import parse_repo_name

if TYPE_CHECKING:
    from gittensor.classes import FileChange as FileChangeType

import bittensor as bt
import requests

from gittensor.classes import (
    FileChange,
    MinerEvaluation,
    PRState,
)
from gittensor.constants import (
    BASE_GITHUB_API_URL,
    GITHUB_HTTP_TIMEOUT_SECONDS,
    MAINTAINER_ASSOCIATIONS,
    MAX_FILE_SIZE_BYTES,
    MAX_FILES_PER_GRAPHQL_BATCH,
    PR_LOOKBACK_DAYS,
)
from gittensor.utils.models import PRInfo
from gittensor.validator.utils.datetime_utils import parse_github_iso_to_utc
from gittensor.validator.utils.load_weights import RepositoryConfig

# core github graphql query
QUERY = """
    query($userId: ID!, $limit: Int!, $cursor: String) {
      node(id: $userId) {
        ... on User {
          issues(states: [OPEN]) { totalCount }
          pullRequests(first: $limit, states: [MERGED, OPEN, CLOSED], orderBy: {field: CREATED_AT, direction: DESC}, after: $cursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              title
              number
              additions
              deletions
              mergedAt
              createdAt
              closedAt
              lastEditedAt
              bodyText
              state
              commits {
                totalCount
              }
              repository {
                name
                owner {
                  login
                }
                defaultBranchRef {
                  name
                }
              }
              headRepository {
                name
                owner {
                  login
                }
              }
              baseRefName
              baseRefOid
              headRefName
              headRefOid
              author {
                login
              }
              authorAssociation
              mergedBy {
                login
              }
              closingIssuesReferences(first: 3) {
                nodes {
                  number
                  title
                  state
                  createdAt
                  closedAt
                  updatedAt
                  author {
                    login
                    ... on User { databaseId }
                  }
                  authorAssociation
                  userContentEdits(first: 1) {
                    nodes { editedAt }
                  }
                  timelineItems(itemTypes: [RENAMED_TITLE_EVENT], last: 1) {
                    nodes {
                      ... on RenamedTitleEvent { createdAt }
                    }
                  }
                }
              }
              reviews(first: 3, states: APPROVED) {
                nodes {
                  author {
                    login
                  }
                }
              }
              timelineItems(itemTypes: [LABELED_EVENT], last: 1) {
                nodes {
                  ... on LabeledEvent {
                    label { name }
                    createdAt
                  }
                }
              }
            }
          }
        }
      }
    }
    """


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
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def get_github_user(token: str) -> Optional[Dict[str, Any]]:
    """Fetch GitHub user data for a PAT with retry.

    Args:
        token (str): Github pat
    Returns:
        Optional[Dict[str, Any]]: Parsed JSON user object on success, or None on failure.
    """
    if not token:
        return None

    headers = make_headers(token)

    # Retry logic for timeout issues
    for attempt in range(6):
        try:
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=GITHUB_HTTP_TIMEOUT_SECONDS)
            if response.status_code == 200:
                try:
                    user_data: Dict[str, Any] = response.json()
                except Exception as e:  # pragma: no cover
                    bt.logging.warning(f'Failed to parse GitHub /user JSON response: {e}')
                    return None

                return user_data

            bt.logging.warning(
                f'GitHub /user request failed with status {response.status_code} (attempt {attempt + 1}/6)'
            )
            if attempt < 5:
                time.sleep(2)

        except Exception as e:
            bt.logging.warning(f'Could not fetch GitHub user (attempt {attempt + 1}/6): {e}')
            if attempt < 5:  # Don't sleep on last attempt
                time.sleep(2)

    return None


def get_github_id(token: str) -> Optional[str]:
    """Get GitHub numeric user id (as string) using a PAT.

    Args:
        token (str): GitHub personal access token.

    Returns:
        Optional[str]: Numeric user id as a string, or None if it cannot be determined.
    """
    user_data = get_github_user(token)
    if not user_data:
        return None

    user_id = user_data.get('id')
    if user_id is None:
        return None

    return str(user_id)


def get_merge_base_sha(repository: str, base_sha: str, head_sha: str, token: str) -> Optional[str]:
    """
    Get the merge-base commit SHA between two refs using GitHub's compare API.

    The merge-base is the common ancestor commit — the correct "before" state
    for computing a PR's own changes via tree-diff scoring.

    Args:
        repository: Repository in format 'owner/repo'
        base_sha: Base branch ref OID
        head_sha: Head branch ref OID
        token: GitHub PAT

    Returns:
        Merge-base commit SHA, or None if the request fails
    """
    headers = make_headers(token)
    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            response = requests.get(
                f'{BASE_GITHUB_API_URL}/repos/{repository}/compare/{base_sha}...{head_sha}',
                headers=headers,
                timeout=15,
            )

            if response.status_code == 200:
                data = response.json()
                merge_base = (data.get('merge_base_commit') or {}).get('sha')
                if merge_base:
                    return merge_base
                bt.logging.warning(f'Compare API returned 200 but no merge_base_commit for {repository}')
                return None

            if attempt < max_attempts - 1:
                backoff_delay = min(5 * (2 ** (attempt)), 30)
                bt.logging.warning(
                    f'Compare API for {repository} failed with status {response.status_code} '
                    f'(attempt {attempt + 1}/{max_attempts}), retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

        except requests.exceptions.RequestException as e:
            if attempt < max_attempts - 1:
                backoff_delay = min(5 * (2 ** (attempt)), 30)
                bt.logging.warning(
                    f'Compare API error for {repository} (attempt {attempt + 1}/{max_attempts}): {e}, '
                    f'retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

    bt.logging.warning(f'Compare API for {repository} failed after {max_attempts} attempts. Will use base_ref_oid.')
    return None


def get_pull_request_file_changes(repository: str, pr_number: int, token: str) -> Optional[List[FileChange]]:
    """
    Get the diff for a specific PR by repository name and PR number.

    Uses retry logic with exponential backoff for transient failures.
    Paginates with per_page=100 (GitHub max) to fetch ALL changed files,
    not just the default 30. On 5xx errors the page size is halved
    (floor 10) to work around large-payload failures.

    Args:
        repository (str): Repository in format 'owner/repo'
        pr_number (int): PR number
        token (str): Github pat
    Returns:
        List[FileChanges]: List object with file changes or None if error
    """
    max_attempts = 3
    per_page = 100
    headers = make_headers(token)

    all_file_diffs: list = []
    page = 1
    attempt = 0
    last_error = None

    while attempt < max_attempts:
        try:
            response = requests.get(
                f'{BASE_GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/files',
                headers=headers,
                params={'per_page': per_page, 'page': page},
                timeout=15,
            )

            if response.status_code == 200:
                file_diffs = response.json()
                all_file_diffs.extend(file_diffs)

                if len(file_diffs) < per_page:
                    return [
                        FileChange.from_github_response(pr_number, repository, file_diff)
                        for file_diff in all_file_diffs
                    ]

                page += 1
                continue

            # Request failed — prepare retry
            last_error = f'status {response.status_code}'

            # Reduce page size on server-side errors (payload may be too large)
            if response.status_code in (502, 503, 504):
                per_page = max(per_page // 2, 10)

            all_file_diffs = []
            page = 1
            attempt += 1

            if attempt < max_attempts:
                backoff_delay = min(5 * (2 ** (attempt - 1)), 30)
                bt.logging.warning(
                    f'File changes request for PR #{pr_number} in {repository} failed with {last_error} '
                    f'(attempt {attempt}/{max_attempts}), per_page={per_page}, retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

        except requests.exceptions.RequestException as e:
            last_error = str(e)
            all_file_diffs = []
            page = 1
            attempt += 1

            if attempt < max_attempts:
                backoff_delay = min(5 * (2 ** (attempt - 1)), 30)
                bt.logging.warning(
                    f'File changes request error for PR #{pr_number} in {repository} '
                    f'(attempt {attempt}/{max_attempts}): {e}, retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

    bt.logging.error(
        f'File changes request for PR #{pr_number} in {repository} failed after {max_attempts} attempts: {last_error}'
    )
    return []


def get_pull_request_maintainer_changes_requested_count(repository: str, pr_number: int, token: str) -> int:
    """
    Count CHANGES_REQUESTED reviews from maintainers for a PR.

    Paginates with per_page=100 (GitHub max) to fetch ALL reviews.
    Uses retry logic with exponential backoff for transient failures.
    On error, returns 0 (fail-safe: no penalty applied).

    Args:
        repository (str): Repository in format 'owner/repo'
        pr_number (int): PR number
        token (str): Github pat
    Returns:
        int: Number of CHANGES_REQUESTED reviews from maintainers
    """
    max_attempts = 3
    per_page = 100
    headers = make_headers(token)

    all_reviews: list = []
    page = 1
    attempt = 0
    last_error = None

    while attempt < max_attempts:
        try:
            response = requests.get(
                f'{BASE_GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/reviews',
                headers=headers,
                params={'per_page': per_page, 'page': page},
                timeout=15,
            )
            if response.status_code == 200:
                reviews = response.json()
                all_reviews.extend(reviews)

                if len(reviews) < per_page:
                    return sum(
                        1
                        for review in all_reviews
                        if review.get('state') == 'CHANGES_REQUESTED'
                        and review.get('author_association') in MAINTAINER_ASSOCIATIONS
                    )

                page += 1
                continue

            # Request failed — prepare retry
            last_error = f'status {response.status_code}'
            all_reviews = []
            page = 1
            attempt += 1

            if attempt < max_attempts:
                backoff_delay = min(5 * (2 ** (attempt - 1)), 30)
                bt.logging.warning(
                    f'Reviews request for PR #{pr_number} in {repository} failed with status {response.status_code} '
                    f'(attempt {attempt}/{max_attempts}), retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

        except requests.exceptions.RequestException as e:
            last_error = str(e)
            all_reviews = []
            page = 1
            attempt += 1

            if attempt < max_attempts:
                backoff_delay = min(5 * (2 ** (attempt - 1)), 30)
                bt.logging.warning(
                    f'Reviews request error for PR #{pr_number} in {repository} '
                    f'(attempt {attempt}/{max_attempts}): {e}, retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

    bt.logging.error(
        f'Reviews request for PR #{pr_number} in {repository} failed after {max_attempts} attempts: {last_error}. '
        f'Defaulting to 0 (no penalty).'
    )
    return 0


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

    result = execute_graphql_query(
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
    headers = make_graphql_headers(token)

    for attempt in range(max_attempts):
        try:
            response = requests.post(
                f'{BASE_GITHUB_API_URL}/graphql',
                headers=headers,
                json={'query': query, 'variables': variables},
                timeout=timeout,
            )

            if response.status_code == 200:
                return response.json()

            # Retry on failure
            if attempt < (max_attempts - 1):
                backoff_delay = min(5 * (2**attempt), 30)  # max of 30 second wait between retries
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
                backoff_delay = min(5 * (2**attempt), 30)
                bt.logging.warning(
                    f'GraphQL request exception (attempt {attempt + 1}/{max_attempts}), '
                    f'retrying in {backoff_delay}s: {e}'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(f'GraphQL request failed after {max_attempts} attempts: {e}')

    return None


@dataclass
class GraphQLPageResult:
    """Result of a paginated GraphQL query."""

    response: Optional[requests.Response]
    page_size: int  # actual page size used (may have been reduced on retry)


def get_github_graphql_query(
    token: str,
    global_user_id: str,
    merged_pr_count: int,
    max_prs: int,
    cursor: Optional[str],
    page_size: Optional[int] = None,
) -> GraphQLPageResult:
    """
    Get all merged PRs for a user across all repositories using GraphQL API with pagination.

    Handles RESOURCE_LIMITS_EXCEEDED (HTTP 200 with errors in body) by halving
    page size and retrying, same pattern as 502/503/504 handling.

    Args:
        token: GitHub PAT
        global_user_id: Converted numeric user ID to GraphQL global node ID
        merged_pr_count: Count of all validated and merged PRs
        max_prs: Maximum number of PRs to fetch across all pages
        cursor: Pagination cursor (where query left off last), None for first page
        page_size: Override page size (used when retrying with smaller pages)

    Returns:
        GraphQLPageResult with response and the page size actually used
    """

    max_attempts = 8
    headers = make_graphql_headers(token)
    limit = page_size if page_size is not None else min(100, max_prs - merged_pr_count)

    for attempt in range(max_attempts):
        variables = {
            'userId': global_user_id,
            'limit': limit,
            'cursor': cursor,
        }
        try:
            response = requests.post(
                f'{BASE_GITHUB_API_URL}/graphql',
                headers=headers,
                json={'query': QUERY, 'variables': variables},
                timeout=30,
            )

            if response.status_code == 200:
                # Check for RESOURCE_LIMITS_EXCEEDED in response body (GitHub returns 200 with errors)
                try:
                    data = response.json()
                except Exception:
                    return GraphQLPageResult(response=response, page_size=limit)

                if _has_resource_limit_errors(data):
                    if attempt < (max_attempts - 1):
                        old_limit = limit
                        limit = max(limit // 2, 10)
                        backoff_delay = min(2 * (2**attempt), 15)
                        bt.logging.warning(
                            f'GraphQL RESOURCE_LIMITS_EXCEEDED (attempt {attempt + 1}/{max_attempts}), '
                            f'page size {old_limit} -> {limit}, retrying in {backoff_delay}s...'
                        )
                        time.sleep(backoff_delay)
                        continue
                    else:
                        bt.logging.error(
                            f'GraphQL RESOURCE_LIMITS_EXCEEDED at page size {limit} after {max_attempts} attempts'
                        )
                        return GraphQLPageResult(response=None, page_size=limit)

                return GraphQLPageResult(response=response, page_size=limit)

            # HTTP error - log and retry
            if attempt < (max_attempts - 1):
                backoff_delay = min(5 * (2**attempt), 30)
                if response.status_code in (502, 503, 504):
                    limit = max(limit // 2, 10)
                    bt.logging.warning(
                        f'GraphQL request for PRs failed with status {response.status_code} '
                        f'(attempt {attempt + 1}/{max_attempts}), page size set to {limit}, retrying in {backoff_delay}s...'
                    )
                else:
                    bt.logging.warning(
                        f'GraphQL request for PRs failed with status {response.status_code} '
                        f'(attempt {attempt + 1}/{max_attempts}), retrying in {backoff_delay}s...'
                    )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(
                    f'GraphQL request for PRs failed with status {response.status_code} after {max_attempts} attempts: {response.text}'
                )

        except requests.exceptions.RequestException as e:
            if attempt < (max_attempts - 1):
                backoff_delay = min(5 * (2**attempt), 30)
                bt.logging.warning(
                    f'GraphQL request connection error (attempt {attempt + 1}/{max_attempts}): {e}, retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(f'GraphQL request failed after {max_attempts} attempts: {e}')
                return GraphQLPageResult(response=None, page_size=limit)

    return GraphQLPageResult(response=None, page_size=limit)


def _has_resource_limit_errors(data: Dict) -> bool:
    """Check if a GraphQL response contains RESOURCE_LIMITS_EXCEEDED errors."""
    errors = data.get('errors')
    if not errors or not isinstance(errors, list):
        return False
    return any(isinstance(e, dict) and e.get('type') == 'RESOURCE_LIMITS_EXCEEDED' for e in errors)


def try_add_open_or_closed_pr(
    miner_eval: MinerEvaluation,
    pr_raw: Dict,
    pr_state: str,
    lookback_date_filter: datetime,
) -> None:
    """
    Attempts to add an OPEN or CLOSED PR to miner_eval if eligible.

    Args:
        miner_eval: The MinerEvaluation to add the PR to
        pr_raw: Raw PR data from GraphQL
        pr_state: GitHub PR state (OPEN, CLOSED, MERGED)
        lookback_date_filter: Date filter for lookback period
    """
    # Ignore all maintainer contributions
    if not os.environ.get('DEV_MODE') and pr_raw.get('authorAssociation') in MAINTAINER_ASSOCIATIONS:
        return

    if pr_state == PRState.OPEN.value:
        miner_eval.add_open_pull_request(pr_raw)

    if pr_state == PRState.CLOSED.value:
        closed_at = pr_raw.get('closedAt')
        if not closed_at:
            bt.logging.warning(f'PR #{pr_raw["number"]} is CLOSED but missing closedAt timestamp.')
            return

        created_at = pr_raw.get('createdAt')
        if not created_at:
            bt.logging.warning(f'PR #{pr_raw["number"]} is CLOSED but missing createdAt timestamp.')
            return

        closed_dt = parse_github_iso_to_utc(closed_at)
        created_dt = parse_github_iso_to_utc(created_at)

        # Ignore stale PRs that were created before the scoring lookback window.
        # This allows users to close old PRs without receiving a fresh credibility penalty.
        if created_dt < lookback_date_filter:
            return

        if closed_dt >= lookback_date_filter:
            miner_eval.add_closed_pull_request(pr_raw)


def should_skip_merged_pr(
    pr_raw: Dict,
    repository_full_name: str,
    repo_config: RepositoryConfig,
    lookback_date_filter: datetime,
) -> tuple[bool, Optional[str]]:
    """
    Validate a merged PR against all eligibility criteria.

    Args:
        pr_raw (Dict): Raw PR data from GraphQL
        repository_full_name (str): Full repository name (owner/repo)
        repo_config (RepositoryConfig): Repository configuration
        lookback_date_filter (datetime): Date filter for lookback period

    Returns:
        tuple[bool, Optional[str]]: (should_skip, skip_reason) - True if PR should be skipped with reason
    """

    if not pr_raw['mergedAt']:
        return (True, f'PR #{pr_raw["number"]} is MERGED, but missing a mergedAt timestamp. Skipping...')

    merged_dt = parse_github_iso_to_utc(pr_raw['mergedAt'])

    # Filter by lookback window
    if merged_dt < lookback_date_filter:
        return (
            True,
            f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - merged before {PR_LOOKBACK_DAYS}-day lookback window',
        )

    # Skip if PR author is a maintainer
    author_association = pr_raw.get('authorAssociation')
    if not os.environ.get('DEV_MODE') and author_association in MAINTAINER_ASSOCIATIONS:
        return (
            True,
            f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - author is {author_association} (has direct merge capabilities)',
        )

    # Skip if PR was merged by the same person who created it (self-merge) AND there's no approvals from a differing party
    if pr_raw['mergedBy'] and pr_raw['author']['login'] == pr_raw['mergedBy']['login']:
        # Check if there are any approvals from users other than the author
        reviews = pr_raw.get('reviews', {}).get('nodes', [])
        has_external_approval = any(
            review.get('author') and review['author']['login'] != pr_raw['author']['login'] for review in reviews
        )

        if not has_external_approval:
            return (True, f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - self-merged, no approval')

    # Skip if PR was not merged to an acceptable branch (default or additional)
    default_branch = (
        pr_raw['repository']['defaultBranchRef']['name'] if pr_raw['repository']['defaultBranchRef'] else 'main'
    )
    base_ref = pr_raw['baseRefName']
    head_ref = pr_raw.get('headRefName', '')  # Source branch (where PR is coming FROM)
    additional_branches = repo_config.additional_acceptable_branches or []
    acceptable_branches = [default_branch] + additional_branches

    # Skip if the source branch (headRef) is also an acceptable branch
    # This prevents PRs like "staging -> main" or "develop -> staging" where both are acceptable branches
    # This check ONLY applies to internal PRs (same repository), as fork branch names are arbitrary.
    # Supports wildcard patterns (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
    head_repo = pr_raw.get('headRepository')
    if head_repo and parse_repo_name(head_repo) == repository_full_name:
        if branch_matches_pattern(head_ref, acceptable_branches):
            return (
                True,
                f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - '
                f"source branch '{head_ref}' is an acceptable branch (merging between acceptable branches not allowed)",
            )

    # Check if merged to an acceptable branch (default or additional)
    # Supports wildcard patterns (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
    if not branch_matches_pattern(base_ref, acceptable_branches):
        return (
            True,
            f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - '
            f"merged to '{base_ref}' (not default branch '{default_branch}' or additional acceptable branches)",
        )

    # All checks passed
    return (False, None)


def load_miners_prs(
    miner_eval: MinerEvaluation, master_repositories: Dict[str, RepositoryConfig], max_prs: int = 1000
) -> None:
    """
    Fetches user PRs via GraphQL API and categorize them by state.
    Populates the provided miner_eval instance with fetched PR data.

    Args:
        miner_eval: The MinerEvaluation object containing github details + more
        master_repositories: Repository metadata (name -> RepositoryConfig)
        max_prs: Maximum merged PRs to fetch
    """
    bt.logging.info('*****Fetching PRs*****')
    miner_eval.github_pr_fetch_failed = False

    if not miner_eval.github_pat:
        bt.logging.warning(f'UID {miner_eval.uid} has no github_pat, skipping PR fetch')
        return

    lookback_date_filter = datetime.now(timezone.utc) - timedelta(days=PR_LOOKBACK_DAYS)
    global_user_id = base64.b64encode(f'04:User{miner_eval.github_id}'.encode()).decode()

    cursor = None
    current_page_size: Optional[int] = None  # None = let get_github_graphql_query choose default

    try:
        while len(miner_eval.merged_pull_requests) < max_prs:
            result = get_github_graphql_query(
                miner_eval.github_pat,
                global_user_id,
                len(miner_eval.merged_pull_requests),
                max_prs,
                cursor,
                page_size=current_page_size,
            )

            # Carry reduced page size forward for subsequent pages
            current_page_size = result.page_size

            if not result.response:
                bt.logging.warning('No response from github, breaking fetch loop...')
                miner_eval.github_pr_fetch_failed = True
                break

            try:
                data: Dict = result.response.json()
            except Exception as e:
                bt.logging.error(f'Failed to parse GraphQL JSON response: {e}')
                miner_eval.github_pr_fetch_failed = True
                break

            # Resource limit errors are already handled in get_github_graphql_query; break on others
            if 'errors' in data:
                non_resource_errors = [e for e in data['errors'] if e.get('type') != 'RESOURCE_LIMITS_EXCEEDED']
                if non_resource_errors:
                    bt.logging.error(f'GraphQL errors: {non_resource_errors}')
                    miner_eval.github_pr_fetch_failed = True
                    break

            user_data: Dict = data.get('data', {}).get('node')
            if not user_data:
                bt.logging.warning('User not found or no pull requests')
                miner_eval.github_pr_fetch_failed = True
                break

            # Extract open issue count from first page (User-level field, not paginated)
            if cursor is None:
                miner_eval.total_open_issues = user_data.get('issues', {}).get('totalCount', 0)

            pr_data: Dict = user_data.get('pullRequests', {})
            prs: List = pr_data.get('nodes', [])
            page_info: Dict = pr_data.get('pageInfo', {})

            for pr_raw in prs:
                try:
                    repository_full_name = parse_repo_name(pr_raw['repository'])
                    pr_state = pr_raw['state']

                    if repository_full_name not in master_repositories:
                        bt.logging.info(f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - ineligible repo')
                        continue

                    repo_config = master_repositories[repository_full_name]

                    # Check if repo is inactive
                    if repo_config.inactive_at is not None:
                        inactive_dt = parse_github_iso_to_utc(repo_config.inactive_at)
                        pr_creation_time = parse_github_iso_to_utc(pr_raw['createdAt'])
                        # Skip PR if it was created after the repo became inactive
                        if pr_creation_time >= inactive_dt:
                            bt.logging.info(
                                f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - PR was created after repo became inactive (created: {pr_creation_time.isoformat()}, inactive: {inactive_dt.isoformat()})'
                            )
                            continue

                    if pr_state in (PRState.OPEN.value, PRState.CLOSED.value):
                        try_add_open_or_closed_pr(miner_eval, pr_raw, pr_state, lookback_date_filter)
                        continue

                    should_skip, skip_reason = should_skip_merged_pr(
                        pr_raw, repository_full_name, repo_config, lookback_date_filter
                    )

                    if should_skip:
                        bt.logging.debug(skip_reason or '')
                        continue

                    miner_eval.add_merged_pull_request(pr_raw)

                except Exception as e:
                    pr_number = pr_raw.get('number', '?')
                    bt.logging.warning(f'Error processing PR #{pr_number}, skipping: {e}')

            if not page_info.get('hasNextPage') or len(prs) == 0:
                break

            cursor = page_info.get('endCursor')

    except Exception as e:
        bt.logging.error(f'Unexpected error fetching PRs via GraphQL: {e}')
        miner_eval.github_pr_fetch_failed = True

    bt.logging.info(
        f'Fetched {len(miner_eval.merged_pull_requests)} merged PRs, {len(miner_eval.open_pull_requests)} open PRs, '
        f'{len(miner_eval.closed_pull_requests)} closed'
    )


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


@dataclass
class FileContentPair:
    """Holds both old (base) and new (head) content for a file."""

    old_content: Optional[str]  # None for new files
    new_content: Optional[str]  # None for deleted files


def _fetch_file_contents_with_base_batch(
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    head_sha: str,
    batch_changes: List['FileChangeType'],
    token: str,
) -> Dict[str, FileContentPair]:
    """Fetch base and head file contents for a single batch of file changes.

    Args:
        repo_owner: Repository owner
        repo_name: Repository name
        base_sha: The base branch SHA (before PR changes)
        head_sha: The head/merge commit SHA (after PR changes)
        batch_changes: File changes for this batch
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to FileContentPair (old_content, new_content)
    """
    file_fields = []
    for i, fc in enumerate(batch_changes):
        # Renames need the old path for the base version
        base_path = fc.previous_filename if fc.previous_filename else fc.filename
        head_path = fc.filename

        # New files have no base version to fetch
        if fc.status != 'added':
            base_expr = f'{base_sha}:{base_path}'
            file_fields.append(
                f'base{i}: object(expression: "{base_expr}") {{ ... on Blob {{ text byteSize isBinary }} }}'
            )

        # Deleted files have no head version to fetch
        if fc.status != 'removed':
            head_expr = f'{head_sha}:{head_path}'
            file_fields.append(
                f'head{i}: object(expression: "{head_expr}") {{ ... on Blob {{ text byteSize isBinary }} }}'
            )

    if not file_fields:
        return {}

    query = f"""
        query($owner: String!, $name: String!) {{
            repository(owner: $owner, name: $name) {{
                {' '.join(file_fields)}
            }}
        }}
    """

    variables = {'owner': repo_owner, 'name': repo_name}

    data = execute_graphql_query(query, variables, token)
    if data is None:
        bt.logging.warning(f'Failed to fetch file contents for {repo_owner}/{repo_name}')
        return {fc.filename: FileContentPair(None, None) for fc in batch_changes}

    if 'errors' in data:
        bt.logging.warning(f'GraphQL errors fetching files: {data["errors"]}')

    repo_data = data.get('data', {}).get('repository', {})
    results: Dict[str, FileContentPair] = {}

    for i, fc in enumerate(batch_changes):
        old_content = None
        new_content = None

        # Pull the old content unless this file was just added
        if fc.status != 'added':
            base_data = repo_data.get(f'base{i}')
            if base_data and not base_data.get('isBinary') and base_data.get('byteSize', 0) <= MAX_FILE_SIZE_BYTES:
                old_content = base_data.get('text')

        # Pull the new content unless this file was removed
        if fc.status != 'removed':
            head_data = repo_data.get(f'head{i}')
            if head_data and not head_data.get('isBinary') and head_data.get('byteSize', 0) <= MAX_FILE_SIZE_BYTES:
                new_content = head_data.get('text')

        results[fc.filename] = FileContentPair(old_content=old_content, new_content=new_content)

    return results


def fetch_file_contents_with_base(
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    head_sha: str,
    file_changes: List['FileChangeType'],
    token: str,
) -> Dict[str, FileContentPair]:
    """Fetch old and new versions of files in batches so large PRs don't hit complexity limits.

    Args:
        repo_owner: Repository owner
        repo_name: Repository name
        base_sha: The base branch SHA (before PR changes)
        head_sha: The head/merge commit SHA (after PR changes)
        file_changes: List of FileChange objects (needed for status and previous_filename)
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to FileContentPair (old_content, new_content)
    """
    if not file_changes:
        return {}

    results: Dict[str, FileContentPair] = {}

    for batch_start in range(0, len(file_changes), MAX_FILES_PER_GRAPHQL_BATCH):
        batch = file_changes[batch_start : batch_start + MAX_FILES_PER_GRAPHQL_BATCH]
        batch_results = _fetch_file_contents_with_base_batch(repo_owner, repo_name, base_sha, head_sha, batch, token)
        results.update(batch_results)

    return results
