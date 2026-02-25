# Entrius 2025
import base64
import fnmatch
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

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
    DEFAULT_RATE_LIMIT_WAIT_SECONDS,
    MAINTAINER_ASSOCIATIONS,
    MAX_FILE_SIZE_BYTES,
    MAX_RATE_LIMIT_WAIT_SECONDS,
    TIER_BASED_INCENTIVE_MECHANISM_START_DATE,
)
from gittensor.utils.utils import parse_repo_name
from gittensor.validator.utils.config import PR_LOOKBACK_DAYS
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
              commits(first: 100) {
                totalCount
                nodes {
                  commit {
                    message
                  }
                }
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
              closingIssuesReferences(first: 50) {
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
              reviews(first: 50, states: APPROVED) {
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


def _is_rate_limit_response(status_code: int) -> bool:
    """Return True if status code indicates GitHub rate limiting (403 or 429)."""
    return status_code in (403, 429)


def _parse_retry_after(response: requests.Response) -> int:
    """
    Parse Retry-After from GitHub rate limit response.
    Retry-After can be an integer (seconds) or an HTTP-date.
    Returns wait time in seconds, capped by MAX_RATE_LIMIT_WAIT_SECONDS.
    """
    value = response.headers.get('Retry-After')
    if not value:
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS

    value = value.strip()
    if value.isdigit():
        return min(int(value), MAX_RATE_LIMIT_WAIT_SECONDS)

    try:
        # HTTP-date format
        parsed = parsedate_to_datetime(value)
        now = datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = (parsed - now).total_seconds()
        return max(0, min(int(delta), MAX_RATE_LIMIT_WAIT_SECONDS))
    except Exception:
        return DEFAULT_RATE_LIMIT_WAIT_SECONDS


def _graphql_rate_limit_from_errors(data: Dict[str, Any]) -> Optional[int]:
    """
    Check GraphQL 200 response for rate limit errors (RATE_LIMITED or secondary limit).
    Returns suggested wait time in seconds, or None if not rate limited.
    """
    errors = data.get('errors') if isinstance(data, dict) else None
    if not isinstance(errors, list):
        return None
    for err in errors:
        if not isinstance(err, dict):
            continue
        msg = (err.get('message') or '').lower()
        err_type = (err.get('type') or '').upper()
        if err_type == 'RATE_LIMITED' or 'rate limit' in msg or 'secondary rate limit' in msg:
            return DEFAULT_RATE_LIMIT_WAIT_SECONDS
    return None


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

    # Retry logic for timeout and rate limit
    for attempt in range(6):
        try:
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=30)
            if response.status_code == 200:
                try:
                    user_data = response.json()
                except Exception as e:  # pragma: no cover
                    bt.logging.warning(f'Failed to parse GitHub /user JSON response: {e}')
                    return None
                if not isinstance(user_data, dict):
                    bt.logging.warning('GitHub /user response is not a JSON object')
                    return None
                _GITHUB_USER_CACHE[token] = user_data
                return user_data

            if _is_rate_limit_response(response.status_code):
                wait_sec = _parse_retry_after(response)
                bt.logging.warning(
                    f'GitHub rate limit (status {response.status_code}), waiting {wait_sec}s (attempt {attempt + 1}/6)'
                )
                time.sleep(wait_sec)
                continue

            # 401 Unauthorized: invalid or revoked token; retrying will not help
            if response.status_code == 401:
                bt.logging.warning('GitHub /user returned 401 Unauthorized (invalid or revoked token)')
                return None

            bt.logging.warning(
                f'GitHub /user request failed with status {response.status_code} (attempt {attempt + 1}/6)'
            )
            if attempt < 5:
                backoff_delay = 5 * (2**attempt)
                time.sleep(backoff_delay)

        except Exception as e:
            bt.logging.warning(f'Could not fetch GitHub user (attempt {attempt + 1}/6): {e}')
            if attempt < 5:
                backoff_delay = 5 * (2**attempt)
                time.sleep(backoff_delay)

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
    Validates response shape and each file entry; skips malformed entries and respects rate limits.
    """
    headers = make_headers(token)
    max_attempts = 6

    for attempt in range(max_attempts):
        try:
            response = requests.get(
                f'{BASE_GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/files',
                headers=headers,
                timeout=15,
            )
            if response.status_code == 200:
                try:
                    raw = response.json()
                except Exception as e:
                    bt.logging.warning(f'PR files response not valid JSON for PR #{pr_number}: {e}')
                    return []
                if not isinstance(raw, list):
                    bt.logging.warning(
                        f'PR files response for #{pr_number} is not a list (got {type(raw).__name__}), returning empty'
                    )
                    return []
                file_changes: List[FileChange] = []
                for i, file_diff in enumerate(raw):
                    fc = FileChange.safe_from_github_response(pr_number, repository, file_diff)
                    if fc is None:
                        bt.logging.debug(
                            f'PR #{pr_number} file entry {i} skipped (missing/invalid keys or types)'
                        )
                        continue
                    file_changes.append(fc)
                return file_changes

            if _is_rate_limit_response(response.status_code):
                wait_sec = _parse_retry_after(response)
                bt.logging.warning(
                    f'Rate limit when fetching PR files (status {response.status_code}), '
                    f'waiting {wait_sec}s (attempt {attempt + 1}/{max_attempts})'
                )
                time.sleep(wait_sec)
                continue

            if attempt < (max_attempts - 1):
                time.sleep(2)
            else:
                bt.logging.error(
                    f'PR files request failed with status {response.status_code} after {max_attempts} attempts'
                )

        except Exception as e:
            bt.logging.error(f'Error getting file changes for PR #{pr_number} in {repository}: {e}')
            if attempt < (max_attempts - 1):
                time.sleep(2)

    return []


def execute_graphql_query(
    query: str,
    variables: Dict[str, Any],
    token: str,
    max_attempts: int = 6,
    timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Execute a GraphQL query with retry logic and exponential backoff.

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
                try:
                    data = response.json()
                except Exception as e:
                    bt.logging.warning(f'GraphQL response not valid JSON: {e}')
                    if attempt < (max_attempts - 1):
                        time.sleep(5 * (2**attempt))
                    continue
                if not isinstance(data, dict):
                    bt.logging.warning('GraphQL response is not a JSON object')
                    if attempt < (max_attempts - 1):
                        time.sleep(5 * (2**attempt))
                    continue
                wait_sec = _graphql_rate_limit_from_errors(data)
                if wait_sec is not None:
                    bt.logging.warning(
                        f'GraphQL rate limit in response body, waiting {wait_sec}s (attempt {attempt + 1}/{max_attempts})'
                    )
                    time.sleep(wait_sec)
                    continue
                return data
            if _is_rate_limit_response(response.status_code):
                wait_sec = _parse_retry_after(response)
                bt.logging.warning(
                    f'GraphQL rate limit (status {response.status_code}), waiting {wait_sec}s (attempt {attempt + 1}/{max_attempts})'
                )
                time.sleep(wait_sec)
                continue

            # Retry on failure
            if attempt < (max_attempts - 1):
                backoff_delay = 5 * (2**attempt)  # 5s, 10s, 20s, 40s, 80s
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
                backoff_delay = 5 * (2**attempt)
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

    attempts = 6
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    variables = {
        'userId': global_user_id,
        'limit': min(100, max_prs - merged_pr_count),
        'cursor': cursor,
    }

    for attempt in range(attempts):
        try:
            response = requests.post(
                f'{BASE_GITHUB_API_URL}/graphql',
                headers=headers,
                json={'query': QUERY, 'variables': variables},
                timeout=30,
            )

            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:
                    return response
                if isinstance(data, dict):
                    wait_sec = _graphql_rate_limit_from_errors(data)
                    if wait_sec is not None:
                        bt.logging.warning(
                            f'GraphQL rate limit in response (attempt {attempt + 1}/{attempts}), waiting {wait_sec}s...'
                        )
                        time.sleep(wait_sec)
                        continue
                return response
            if _is_rate_limit_response(response.status_code):
                wait_sec = _parse_retry_after(response)
                bt.logging.warning(
                    f'GraphQL rate limit (status {response.status_code}), waiting {wait_sec}s (attempt {attempt + 1}/{attempts})...'
                )
                time.sleep(wait_sec)
                continue
            # error - log and retry
            if attempt < (attempts - 1):
                # Exponential backoff: 5s, 10s, 20s, 40s, 80s
                backoff_delay = 5 * (2**attempt)
                bt.logging.warning(
                    f'GraphQL request failed with status {response.status_code} (attempt {attempt + 1}/{attempts}), retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(
                    f'GraphQL request failed with status {response.status_code} after {attempts} attempts: {response.text}'
                )

        except requests.exceptions.RequestException as e:
            if attempt < (attempts - 1):
                # Exponential backoff: 5s, 10s, 20s, 40s, 80s
                backoff_delay = 5 * (2**attempt)
                bt.logging.warning(
                    f'GraphQL request connection error (attempt {attempt + 1}/{attempts}): {e}, retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(f'GraphQL request failed after {attempts} attempts: {e}')
                return None

    return None


def try_add_open_or_closed_pr(
    miner_eval: MinerEvaluation,
    pr_raw: Dict,
    repository_full_name: str,
    pr_state: str,
    lookback_date_filter: datetime,
    active_repositories: List[str],
) -> None:
    """
    Attempts to add an OPEN or CLOSED PR to miner_eval if eligible.

    Args:
        miner_eval: The MinerEvaluation to add the PR to
        pr_raw: Raw PR data from GraphQL
        repository_full_name: Full repository name (owner/repo), lowercase
        pr_state: GitHub PR state (OPEN, CLOSED, MERGED)
        lookback_date_filter: Date filter for lookback period
        active_repositories: List of active repository names (lowercase)
    """
    if repository_full_name not in active_repositories:
        return

    # Ignore all maintainer contributions
    if pr_raw.get('authorAssociation') in MAINTAINER_ASSOCIATIONS:
        return

    if pr_state == PRState.OPEN.value:
        miner_eval.add_open_pull_request(pr_raw)

    if pr_state == PRState.CLOSED.value:
        closed_at = pr_raw.get('closedAt')
        if not closed_at or not isinstance(closed_at, str):
            bt.logging.warning(f'PR #{pr_raw.get("number", "?")} is CLOSED but missing closedAt timestamp.')
            return
        try:
            closed_dt = datetime.fromisoformat(closed_at.rstrip('Z')).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            bt.logging.warning(f'PR #{pr_raw.get("number", "?")} has invalid closedAt format, skipping.')
            return
        if closed_dt >= lookback_date_filter:
            miner_eval.add_closed_pull_request(pr_raw)


def should_skip_merged_pr(
    pr_raw: Dict,
    repository_full_name: str,
    master_repositories: Dict[str, RepositoryConfig],
    lookback_date_filter: datetime,
) -> tuple[bool, Optional[str]]:
    """
    Validate a merged PR against all eligibility criteria.

    Args:
        pr_raw (Dict): Raw PR data from GraphQL
        repository_full_name (str): Full repository name (owner/repo)
        master_repositories (Dict[str, RepositoryConfig]): Repository metadata (keys are normalized to lowercase)
        lookback_date_filter (datetime): Date filter for lookback period

    Returns:
        tuple[bool, Optional[str]]: (should_skip, skip_reason) - True if PR should be skipped with reason
    """

    if not pr_raw['mergedAt']:
        return (True, f'PR #{pr_raw["number"]} is MERGED, but missing a mergedAt timestamp. Skipping...')

    merged_dt = datetime.fromisoformat(pr_raw['mergedAt'].rstrip('Z')).replace(tzinfo=timezone.utc)

    # Filter by master_repositories - keys are already normalized to lowercase
    if repository_full_name not in master_repositories:
        return (True, f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - ineligible repo')

    repo_config = master_repositories[repository_full_name]

    # Filter by lookback window
    if merged_dt < lookback_date_filter:
        return (
            True,
            f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - merged within {PR_LOOKBACK_DAYS} day lookback window',
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

    # Check if repo is inactive
    if repo_config.inactive_at is not None:
        inactive_dt = datetime.fromisoformat(repo_config.inactive_at.rstrip('Z')).replace(tzinfo=timezone.utc)
        # Skip PR if it was merged at or after the repo became inactive
        if merged_dt >= inactive_dt:
            return (
                True,
                f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - PR was merged at/after repo became inactive (merged: {merged_dt.isoformat()}, inactive: {inactive_dt.isoformat()})',
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

    # Build list of active repositories (those without an inactive_at timestamp)
    # Keys are already normalized to lowercase
    active_repositories = [
        repo_full_name for repo_full_name, config in master_repositories.items() if config.inactive_at is None
    ]

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

            pr_data = user_data.get('pullRequests') or {}
            prs = pr_data.get('nodes') or []
            if not isinstance(prs, list):
                bt.logging.warning('GraphQL pullRequests.nodes is not a list, breaking fetch')
                break
            page_info = pr_data.get('pageInfo') or {}

            for pr_raw in prs:
                if not isinstance(pr_raw, dict):
                    bt.logging.debug('Skipping non-dict PR node in GraphQL response')
                    continue
                repo_node = pr_raw.get('repository')
                if not isinstance(repo_node, dict) or not isinstance(repo_node.get('owner'), dict) or not repo_node.get('name'):
                    bt.logging.debug(f'Skipping PR node with missing/invalid repository (number={pr_raw.get("number")})')
                    continue
                try:
                    repository_full_name = parse_repo_name(repo_node)
                except (KeyError, TypeError) as e:
                    bt.logging.debug(f'Skipping PR node: parse_repo_name failed: {e}')
                    continue
                pr_state = pr_raw.get('state')
                if not pr_state or not isinstance(pr_state, str):
                    bt.logging.debug(f'Skipping PR node with missing state (repository={repository_full_name})')
                    continue
                created_at_raw = pr_raw.get('createdAt')
                if not created_at_raw or not isinstance(created_at_raw, str):
                    bt.logging.debug(f'Skipping PR #{pr_raw.get("number")} with missing createdAt')
                    continue
                try:
                    pr_creation_time = datetime.fromisoformat(created_at_raw.rstrip('Z')).replace(tzinfo=timezone.utc)
                except (ValueError, TypeError) as e:
                    bt.logging.debug(f'Skipping PR #{pr_raw.get("number")}: invalid createdAt: {e}')
                    continue

                # Stop querying once we hit PRs older than the tier incentive start date

                if pr_creation_time < TIER_BASED_INCENTIVE_MECHANISM_START_DATE:
                    bt.logging.info(
                        f'Reached PR #{pr_raw["number"]} in {repository_full_name} created at {pr_creation_time}, '
                        f'before tier incentive start date ({TIER_BASED_INCENTIVE_MECHANISM_START_DATE}). '
                        f'Stopping PR fetch.'
                    )
                    return

                if pr_state in (PRState.OPEN.value, PRState.CLOSED.value):
                    try_add_open_or_closed_pr(
                        miner_eval, pr_raw, repository_full_name, pr_state, lookback_date_filter, active_repositories
                    )
                    continue

                should_skip, skip_reason = should_skip_merged_pr(
                    pr_raw, repository_full_name, master_repositories, lookback_date_filter
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
