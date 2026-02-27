# Entrius 2025
import base64
import fnmatch
import re
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
    MAINTAINER_ASSOCIATIONS,
    MAX_FILE_SIZE_BYTES,
    PR_LOOKBACK_DAYS,
    TIER_BASED_INCENTIVE_MECHANISM_START_DATE,
)
from gittensor.utils.models import PRInfo
from gittensor.validator.utils.load_weights import RepositoryConfig

# core github graphql query
QUERY = """
    query($userId: ID!, $limit: Int!, $cursor: String) {
      node(id: $userId) {
        ... on User {
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
              closingIssuesReferences(first: 10) {
                nodes {
                  number
                  title
                  state
                  createdAt
                  closedAt
                  author {
                    login
                  }
                  authorAssociation
                }
              }
              reviews(first: 10, states: APPROVED) {
                nodes {
                  author {
                    login
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


# In-process cache for GitHub /user responses, keyed by PAT.
_GITHUB_USER_CACHE: Dict[str, Dict[str, Any]] = {}


def get_github_user(token: str) -> Optional[Dict[str, Any]]:
    """Fetch GitHub user data for a PAT with retry and in-process cache.

    Args:
        token (str): Github pat
    Returns:
        Optional[Dict[str, Any]]: Parsed JSON user object on success, or None on failure.
    """
    if not token:
        return None

    # Check cache first to avoid duplicate /user calls for the same PAT.
    cached = _GITHUB_USER_CACHE.get(token)
    if cached is not None:
        return cached

    headers = make_headers(token)

    # Retry logic for timeout issues
    for attempt in range(6):
        try:
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=30)
            if response.status_code == 200:
                try:
                    user_data: Dict[str, Any] = response.json()
                except Exception as e:  # pragma: no cover
                    bt.logging.warning(f'Failed to parse GitHub /user JSON response: {e}')
                    return None

                _GITHUB_USER_CACHE[token] = user_data
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


def get_github_username(token: str) -> Optional[str]:
    """Get GitHub username (login) using a PAT.

    Args:
        token (str): GitHub pat

    Returns:
        Optional[str]: Username (login) string, or None if the PAT is invalid or an error occurred.
    """
    user_data = get_github_user(token)
    if not user_data:
        return None
    return user_data.get('login')


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


def get_github_account_age_days(token: str) -> Optional[int]:
    """Get GitHub account age in days for a PAT.

    Args:
        token (str): GitHub personal access token.

    Returns:
        Optional[int]: Number of days since account creation, or None if it cannot be determined.
    """
    user_data = get_github_user(token)
    if not user_data:
        return None

    created_at = user_data.get('created_at')
    if not created_at:
        return None

    try:
        created_dt = datetime.fromisoformat(created_at.rstrip('Z')).replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        return (now_dt - created_dt).days
    except Exception as e:
        bt.logging.warning(f'Could not parse GitHub account creation date: {e}')
        return None


def get_pull_request_file_changes(repository: str, pr_number: int, token: str) -> Optional[List[FileChange]]:
    """
    Get the diff for a specific PR by repository name and PR number.

    Uses retry logic with exponential backoff for transient failures.

    Args:
        repository (str): Repository in format 'owner/repo'
        pr_number (int): PR number
        token (str): Github pat
    Returns:
        List[FileChanges]: List object with file changes or None if error
    """
    max_attempts = 3
    headers = make_headers(token)

    last_error = None
    for attempt in range(max_attempts):
        try:
            response = requests.get(
                f'{BASE_GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/files', headers=headers, timeout=15
            )
            if response.status_code == 200:
                file_diffs = response.json()
                return [FileChange.from_github_response(pr_number, repository, file_diff) for file_diff in file_diffs]

            last_error = f'status {response.status_code}'
            if attempt < max_attempts - 1:
                backoff_delay = min(5 * (2**attempt), 30)
                bt.logging.warning(
                    f'File changes request for PR #{pr_number} in {repository} failed with status {response.status_code} '
                    f'(attempt {attempt + 1}/{max_attempts}), retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

        except requests.exceptions.RequestException as e:
            last_error = str(e)
            if attempt < max_attempts - 1:
                backoff_delay = min(5 * (2**attempt), 30)
                bt.logging.warning(
                    f'File changes request error for PR #{pr_number} in {repository} '
                    f'(attempt {attempt + 1}/{max_attempts}): {e}, retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

    bt.logging.error(
        f'File changes request for PR #{pr_number} in {repository} failed after {max_attempts} attempts: {last_error}'
    )
    return []


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
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

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


def get_github_graphql_query(
    token: str, global_user_id: str, merged_pr_count: int, max_prs: int, cursor: Optional[str]
) -> Optional[requests.Response]:
    """
    Get all merged PRs for a user across all repositories using GraphQL API with pagination.

    Args:
        token (str): GitHub PAT
        global_user_id (str): Converted numeric user ID to GraphQL global node ID
        merged_pr_count (int): Count of all validated and merged PRs
        max_prs (int): Maximum number of PRs to fetch across all pages
        cursor (Optional[str]): Pagination cursor (where query left off last), None for first page

    Returns:
        Optional[requests.Response]: Response object from the GraphQL query or None if errors occurred
    """

    max_attempts = 8
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    limit = min(100, max_prs - merged_pr_count)

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
                return response

            # error - log and retry
            if attempt < (max_attempts - 1):
                backoff_delay = min(5 * (2**attempt), 30)  # cap at 30s
                # Reduce page size on server-side errors (query may be too expensive)
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
                backoff_delay = min(5 * (2**attempt), 30)  # cap at 30s
                bt.logging.warning(
                    f'GraphQL request connection error (attempt {attempt + 1}/{max_attempts}): {e}, retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(f'GraphQL request failed after {max_attempts} attempts: {e}')
                return None

    return None


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
    if pr_raw.get('authorAssociation') in MAINTAINER_ASSOCIATIONS:
        return

    if pr_state == PRState.OPEN.value:
        miner_eval.add_open_pull_request(pr_raw)

    if pr_state == PRState.CLOSED.value:
        closed_at = pr_raw.get('closedAt')
        if not closed_at:
            bt.logging.warning(f'PR #{pr_raw["number"]} is CLOSED but missing closedAt timestamp.')
            return

        closed_dt = datetime.fromisoformat(closed_at.rstrip('Z')).replace(tzinfo=timezone.utc)
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

    merged_dt = datetime.fromisoformat(pr_raw['mergedAt'].rstrip('Z')).replace(tzinfo=timezone.utc)

    # Filter by lookback window
    if merged_dt < lookback_date_filter:
        return (
            True,
            f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - merged before {PR_LOOKBACK_DAYS}-day lookback window',
        )

    # Skip if PR author is a maintainer
    author_association = pr_raw.get('authorAssociation')
    if author_association in MAINTAINER_ASSOCIATIONS:
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

    lookback_date_filter = datetime.now(timezone.utc) - timedelta(days=PR_LOOKBACK_DAYS)
    global_user_id = base64.b64encode(f'04:User{miner_eval.github_id}'.encode()).decode()

    cursor = None

    try:
        while len(miner_eval.merged_pull_requests) < max_prs:
            response = get_github_graphql_query(
                miner_eval.github_pat, global_user_id, len(miner_eval.merged_pull_requests), max_prs, cursor
            )
            if not response:
                bt.logging.warning('No response from github, breaking fetch loop...')
                break

            data: Dict = response.json()

            if 'errors' in data:
                bt.logging.error(f'GraphQL errors: {data["errors"]}')
                break

            user_data: Dict = data.get('data', {}).get('node')
            if not user_data:
                bt.logging.warning('User not found or no pull requests')
                break

            pr_data: Dict = user_data.get('pullRequests', {})
            prs: List = pr_data.get('nodes', [])
            page_info: Dict = pr_data.get('pageInfo', {})

            for pr_raw in prs:
                repository_full_name = parse_repo_name(pr_raw['repository'])
                pr_state = pr_raw['state']

                # Stop querying once we hit PRs older than the tier incentive start date
                pr_creation_time = datetime.fromisoformat(pr_raw['createdAt'].rstrip('Z')).replace(tzinfo=timezone.utc)

                if pr_creation_time < TIER_BASED_INCENTIVE_MECHANISM_START_DATE:
                    bt.logging.info(
                        f'Reached PR #{pr_raw["number"]} in {repository_full_name} created at {pr_creation_time}, '
                        f'before tier incentive start date ({TIER_BASED_INCENTIVE_MECHANISM_START_DATE}). '
                        f'Stopping PR fetch.'
                    )
                    return

                if repository_full_name not in master_repositories:
                    bt.logging.info(f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - ineligible repo')
                    continue

                repo_config = master_repositories[repository_full_name]

                # Check if repo is inactive
                if repo_config.inactive_at is not None:
                    inactive_dt = datetime.fromisoformat(repo_config.inactive_at.rstrip('Z')).replace(
                        tzinfo=timezone.utc
                    )
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
                    bt.logging.debug(skip_reason)
                    continue

                miner_eval.add_merged_pull_request(pr_raw)

            if not page_info.get('hasNextPage') or len(prs) == 0:
                break

            cursor = page_info.get('endCursor')

        bt.logging.info(
            f'Fetched {len(miner_eval.merged_pull_requests)} merged PRs, {len(miner_eval.open_pull_requests)} open PRs, '
            f'{len(miner_eval.closed_pull_requests)} closed'
        )

    except Exception as e:
        bt.logging.error(f'Error fetching PRs via GraphQL: {e}')


def extract_pr_number_from_url(pr_url: str) -> Optional[int]:
    """Extract PR number from a GitHub PR URL.

    Args:
        pr_url: Full GitHub PR URL (e.g., https://github.com/owner/repo/pull/123)

    Returns:
        PR number as integer, or None if invalid URL
    """
    if not pr_url:
        return None
    match = re.search(r'/pull/(\d+)', pr_url)
    return int(match.group(1)) if match else None


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


def fetch_file_contents_batch(
    repo_owner: str,
    repo_name: str,
    head_sha: str,
    file_paths: List[str],
    token: str,
) -> Dict[str, Optional[str]]:
    """
    Fetch multiple file contents from a repository in a single GraphQL request.

    Uses retry logic with exponential backoff for reliability.

    Args:
        repo_owner: Repository owner
        repo_name: Repository name
        head_sha: The commit SHA to fetch files at
        file_paths: List of file paths to fetch
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to their contents (None if file is binary, deleted, or too large)
    """
    if not file_paths:
        return {}

    # Build GraphQL query with aliased file fields
    file_fields = []
    for i, path in enumerate(file_paths):
        expression = f'{head_sha}:{path}'
        file_fields.append(
            f'file{i}: object(expression: "{expression}") {{ ... on Blob {{ text byteSize isBinary }} }}'
        )

    query = f"""
        query($owner: String!, $name: String!) {{
            repository(owner: $owner, name: $name) {{
                {' '.join(file_fields)}
            }}
        }}
    """

    variables = {'owner': repo_owner, 'name': repo_name}

    # Execute with retry logic
    data = execute_graphql_query(query, variables, token)
    if data is None:
        bt.logging.warning(f'Failed to fetch file contents for {repo_owner}/{repo_name}')
        return {path: None for path in file_paths}

    if 'errors' in data:
        bt.logging.warning(f'GraphQL errors fetching files: {data["errors"]}')

    repo_data = data.get('data', {}).get('repository', {})
    results = {}

    for i, path in enumerate(file_paths):
        file_data = repo_data.get(f'file{i}')

        if file_data is None:
            results[path] = None
        elif file_data.get('isBinary'):
            results[path] = None
        elif file_data.get('byteSize', 0) > MAX_FILE_SIZE_BYTES:
            results[path] = None
        else:
            results[path] = file_data.get('text')

    return results


@dataclass
class FileContentPair:
    """Holds both old (base) and new (head) content for a file."""

    old_content: Optional[str]  # None for new files
    new_content: Optional[str]  # None for deleted files


def fetch_file_contents_with_base(
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    head_sha: str,
    file_changes: List['FileChangeType'],
    token: str,
) -> Dict[str, FileContentPair]:
    """
    Fetch both base and head (old and new) versions of files in a single GraphQL request.

    Args:
        repo_owner: Repository owner
        repo_name: Repository name
        base_sha: The base branch SHA (before PR changes)
        head_sha: The head/merge commit SHA (after PR changes)
        file_changes: List of FileChange objects (needed for status and previous_filename)
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to FileContentPair (old_content, new_content)
        - For new files: old_content is None
        - For deleted files: new_content is None
        - For renamed files: old_content fetched from previous_filename
    """
    if not file_changes:
        return {}

    # Build GraphQL query with both base and head versions
    file_fields = []
    for i, fc in enumerate(file_changes):
        # Determine the path to fetch for base version
        # For renames, use previous_filename; otherwise use current filename
        base_path = fc.previous_filename if fc.previous_filename else fc.filename
        head_path = fc.filename

        # Only fetch base version if file wasn't newly added
        if fc.status != 'added':
            base_expr = f'{base_sha}:{base_path}'
            file_fields.append(
                f'base{i}: object(expression: "{base_expr}") {{ ... on Blob {{ text byteSize isBinary }} }}'
            )

        # Only fetch head version if file wasn't deleted
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

    # Execute with retry logic
    data = execute_graphql_query(query, variables, token)
    if data is None:
        bt.logging.warning(f'Failed to fetch file contents for {repo_owner}/{repo_name}')
        return {fc.filename: FileContentPair(None, None) for fc in file_changes}

    if 'errors' in data:
        bt.logging.warning(f'GraphQL errors fetching files: {data["errors"]}')

    repo_data = data.get('data', {}).get('repository', {})
    results: Dict[str, FileContentPair] = {}

    for i, fc in enumerate(file_changes):
        old_content = None
        new_content = None

        # Extract base (old) content if applicable
        if fc.status != 'added':
            base_data = repo_data.get(f'base{i}')
            if base_data and not base_data.get('isBinary') and base_data.get('byteSize', 0) <= MAX_FILE_SIZE_BYTES:
                old_content = base_data.get('text')

        # Extract head (new) content if applicable
        if fc.status != 'removed':
            head_data = repo_data.get(f'head{i}')
            if head_data and not head_data.get('isBinary') and head_data.get('byteSize', 0) <= MAX_FILE_SIZE_BYTES:
                new_content = head_data.get('text')

        results[fc.filename] = FileContentPair(old_content=old_content, new_content=new_content)

    return results
