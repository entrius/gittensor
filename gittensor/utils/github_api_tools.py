# Entrius 2025
import base64
import fnmatch
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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
    Get the diff for a specific PR by repository name and PR number
    Args:
        repository (str): Repository in format 'owner/repo'
        pr_number (int): PR number
        token (str): Github pat
    Returns:
        List[FileChanges]: List object with file changes or None if error
    """
    headers = make_headers(token)

    try:
        response = requests.get(
            f'{BASE_GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/files', headers=headers, timeout=15
        )
        if response.status_code == 200:
            file_diffs = response.json()
            return [FileChange.from_github_response(pr_number, repository, file_diff) for file_diff in file_diffs]

        return []

    except Exception as e:
        bt.logging.error(f'Error getting file changes for PR #{pr_number} in {repository}: {e}')
        return []


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
                return response
            # error - log and retry
            elif attempt < (attempts - 1):
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

    if pr_state == PRState.OPEN.value:
        if pr_raw.get('authorAssociation') not in MAINTAINER_ASSOCIATIONS:
            miner_eval.add_open_pull_request(pr_raw)
        return

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

            pr_data: Dict = user_data.get('pullRequests', {})
            prs: Dict = pr_data.get('nodes', [])
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


# 1MB max file size for tree-sitter parsing. Files exceeding this get a fixed score.
MAX_FILE_SIZE_BYTES = 1_000_000


def fetch_file_contents_batch(
    repo_owner: str,
    repo_name: str,
    head_sha: str,
    file_paths: List[str],
    token: str,
) -> Dict[str, Optional[str]]:
    """
    Fetch multiple file contents from a repository in a single GraphQL request.

    Args:
        repo_owner: Repository owner (e.g., 'anthropics')
        repo_name: Repository name (e.g., 'claude-code')
        head_sha: The commit SHA to fetch files at (from PR's headRefOid)
        file_paths: List of file paths to fetch (e.g., ['src/main.py', 'lib/utils.js'])
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to their contents (None if file is binary, deleted, or too large)
    """
    if not file_paths:
        return {}

    file_fields = []
    for i, path in enumerate(file_paths):
        expression = f'{head_sha}:{path}'
        file_fields.append(
            f'file{i}: object(expression: "{expression}") {{ ... on Blob {{ text byteSize isBinary }} }}'
        )

    query = f'''
        query($owner: String!, $name: String!) {{
            repository(owner: $owner, name: $name) {{
                {" ".join(file_fields)}
            }}
        }}
    '''

    variables = {'owner': repo_owner, 'name': repo_name}
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    try:
        response = requests.post(
            f'{BASE_GITHUB_API_URL}/graphql',
            headers=headers,
            json={'query': query, 'variables': variables},
            timeout=30,
        )

        if response.status_code != 200:
            bt.logging.warning(f'Failed to fetch file contents: {response.status_code}')
            return {path: None for path in file_paths}

        data = response.json()
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

    except requests.exceptions.RequestException as e:
        bt.logging.error(f'Error fetching file contents: {e}')
        return {path: None for path in file_paths}
