# Entrius 2025
import base64
import fnmatch
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import bittensor as bt
import requests

from gittensor.classes import FileChange, PRCountResult
from gittensor.constants import (
    BASE_GITHUB_API_URL,
    MERGE_SUCCESS_RATIO_APPLICATION_DATE,
)
from gittensor.validator.utils.config import MERGED_PR_LOOKBACK_DAYS

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
              baseRefName
              headRefName
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


def normalize_repo_name(repo_name: str) -> str:
    """Normalize repository name to lowercase for case-insensitive comparison.
    
    Args:
        repo_name (str): Repository name in format 'owner/repo'
    
    Returns:
        str: Lowercase repository name
    """
    return repo_name.lower()


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
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
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
            response = requests.get(f"{BASE_GITHUB_API_URL}/user", headers=headers, timeout=30)
            if response.status_code == 200:
                try:
                    user_data: Dict[str, Any] = response.json()
                except Exception as e:  # pragma: no cover
                    bt.logging.warning(f"Failed to parse GitHub /user JSON response: {e}")
                    return None

                _GITHUB_USER_CACHE[token] = user_data
                return user_data

            bt.logging.warning(
                f"GitHub /user request failed with status {response.status_code} (attempt {attempt + 1}/6)"
            )
            if attempt < 5:
                time.sleep(2)

        except Exception as e:
            bt.logging.warning(
                f"Could not fetch GitHub user (attempt {attempt + 1}/6): {e}"
            )
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
    return user_data.get("login")


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

    user_id = user_data.get("id")
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

    created_at = user_data.get("created_at")
    if not created_at:
        return None

    try:
        created_dt = datetime.fromisoformat(created_at.rstrip("Z")).replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        return (now_dt - created_dt).days
    except Exception as e:
        bt.logging.warning(f"Could not parse GitHub account creation date: {e}")
        return None


def get_pull_request_file_changes(repository: str, pr_number: int, token: str) -> Optional[List[FileChange]]:
    '''
    Get the diff for a specific PR by repository name and PR number
    Args:
        repository (str): Repository in format 'owner/repo'
        pr_number (int): PR number
        token (str): Github pat
    Returns:
        List[FileChanges]: List object with file changes or None if error
    '''
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
        bt.logging.error(f"Error getting file changes for PR #{pr_number} in {repository}: {e}")
        return []


def get_github_graphql_query(
    token: str, global_user_id: str, all_valid_prs: List[Dict], max_prs: int, cursor: Optional[str]
) -> Optional[requests.Response]:
    """
    Get all merged PRs for a user across all repositories using GraphQL API with pagination.

    Args:
        token (str): GitHub PAT
        global_user_id (str): Converted numeric user ID to GraphQL global node ID
        all_valid_prs (List[Dict]): List of raw currently validated PRs
        max_prs (int): Maximum number of PRs to fetch across all pages
        cursor (Optional[str]): Pagination cursor (where query left off last), None for first page

    Returns:
        Optional[requests.Response]: Response object from the GraphQL query or None if errors occurred
    """

    attempts = 6
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    variables = {
        "userId": global_user_id,
        "limit": min(100, max_prs - len(all_valid_prs)),
        "cursor": cursor,
    }

    for attempt in range(attempts):
        try:
            response = requests.post(
                f'{BASE_GITHUB_API_URL}/graphql',
                headers=headers,
                json={"query": QUERY, "variables": variables},
                timeout=30,
            )

            if response.status_code == 200:
                return response
            # error - log and retry
            elif attempt < (attempts - 1):
                # Exponential backoff: 5s, 10s, 20s, 40s, 80s
                backoff_delay = 5 * (2**attempt)
                bt.logging.warning(
                    f"GraphQL request failed with status {response.status_code} (attempt {attempt + 1}/{attempts}), retrying in {backoff_delay}s..."
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(
                    f"GraphQL request failed with status {response.status_code} after {attempts} attempts: {response.text}"
                )

        except requests.exceptions.RequestException as e:
            if attempt < (attempts - 1):
                # Exponential backoff: 5s, 10s, 20s, 40s, 80s
                backoff_delay = 5 * (2**attempt)
                bt.logging.warning(
                    f"GraphQL request connection error (attempt {attempt + 1}/{attempts}): {e}, retrying in {backoff_delay}s..."
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(f"GraphQL request failed after {attempts} attempts: {e}")
                return None

    return None


def _process_non_merged_pr(
    pr_raw: Dict, repository_full_name: str, pr_state: str, date_filter: datetime, active_repositories: List[str]
) -> tuple[int, int]:
    """
    Process open and closed (not merged) PRs and return counts.

    Args:
        pr_raw (Dict): Raw PR data from GraphQL
        repository_full_name (str): Full repository name (owner/repo)
        pr_state (str): PR state (OPEN, CLOSED, MERGED)
        date_filter (datetime): Date filter for lookback period
        active_repositories (List[str]): List of active repository names (already normalized to lowercase)

    Returns:
        tuple[int, int]: (open_pr_delta, closed_pr_delta) - increment counts for open/closed PRs
    """
    open_pr_delta = 0
    closed_pr_delta = 0

    # Normalize repository name for comparison (active_repositories keys are already lowercase)
    normalized_repo = normalize_repo_name(repository_full_name)

    # Check if it's an open PR. We are counting ALL open PRs to active repositories
    if pr_state == 'OPEN':
        if normalized_repo in active_repositories:
            open_pr_delta = 1
        return (open_pr_delta, closed_pr_delta)

    # Handle CLOSED (not merged) PRs - count if within lookback period
    if pr_state == 'CLOSED' and not pr_raw['mergedAt']:
        if pr_raw.get('closedAt'):
            closed_dt = datetime.fromisoformat(pr_raw['closedAt'].rstrip("Z")).replace(tzinfo=timezone.utc)
            if (
                normalized_repo in active_repositories
                and closed_dt >= date_filter
                and closed_dt > MERGE_SUCCESS_RATIO_APPLICATION_DATE
            ):
                closed_pr_delta = 1
        return (open_pr_delta, closed_pr_delta)

    return (open_pr_delta, closed_pr_delta)


def _should_skip_merged_pr(
    pr_raw: Dict,
    repository_full_name: str,
    master_repositories: dict[str, dict],
    date_filter: datetime,
    merged_dt: datetime,
) -> tuple[bool, Optional[str]]:
    """
    Validate a merged PR against all eligibility criteria.

    Args:
        pr_raw (Dict): Raw PR data from GraphQL
        repository_full_name (str): Full repository name (owner/repo)
        master_repositories (dict[str, dict]): Repository metadata (keys are normalized to lowercase)
        date_filter (datetime): Date filter for lookback period
        merged_dt (datetime): Parsed merge datetime

    Returns:
        tuple[bool, Optional[str]]: (should_skip, skip_reason) - True if PR should be skipped with reason
    """
    # Filter by master_repositories - keys are already normalized to lowercase
    normalized_repo = normalize_repo_name(repository_full_name)
    if normalized_repo not in master_repositories:
        return (True, f"Skipping PR #{pr_raw['number']} in {repository_full_name} - ineligible repo")
    
    repo_key = normalized_repo

    # Filter by lookback window
    if merged_dt < date_filter:
        return (
            True,
            f"Skipping PR #{pr_raw['number']} in {repository_full_name} - merged before {MERGED_PR_LOOKBACK_DAYS} day lookback window",
        )

    # Skip if PR author is a maintainer
    author_association = pr_raw.get('authorAssociation')
    if author_association in ['OWNER', 'MEMBER', 'COLLABORATOR']:
        return (
            True,
            f"Skipping PR #{pr_raw['number']} in {repository_full_name} - author is {author_association} (has direct merge capabilities)",
        )

    # Skip if PR was merged by the same person who created it (self-merge) AND there's no approvals from a differing party
    if pr_raw['mergedBy'] and pr_raw['author']['login'] == pr_raw['mergedBy']['login']:
        # Check if there are any approvals from users other than the author
        reviews = pr_raw.get('reviews', {}).get('nodes', [])
        has_external_approval = any(
            review.get('author') and review['author']['login'] != pr_raw['author']['login'] for review in reviews
        )

        if not has_external_approval:
            return (True, f"Skipping PR #{pr_raw['number']} in {repository_full_name} - self-merged, no approval")

    # Skip if PR was not merged to an acceptable branch (default or additional)
    default_branch = (
        pr_raw['repository']['defaultBranchRef']['name'] if pr_raw['repository']['defaultBranchRef'] else 'main'
    )
    base_ref = pr_raw['baseRefName']
    head_ref = pr_raw.get('headRefName', '')  # Source branch (where PR is coming FROM)
    repo_metadata = master_repositories[repo_key]
    additional_branches = repo_metadata.get('additional_acceptable_branches', [])

    # Build list of all acceptable branches (default + additional)
    acceptable_branches = [default_branch] + additional_branches

    # Skip if the source branch (headRef) is also an acceptable branch
    # This prevents PRs like "staging -> main" or "develop -> staging" where both are acceptable branches
    # Supports wildcard patterns (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
    if branch_matches_pattern(head_ref, acceptable_branches):
        return (
            True,
            f"Skipping PR #{pr_raw['number']} in {repository_full_name} - "
            f"source branch '{head_ref}' is an acceptable branch (merging between acceptable branches not allowed)",
        )

    # Check if merged to default branch
    if base_ref != default_branch:
        # If not default, check if repository has additional acceptable branches
        # Supports wildcard patterns (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
        if not branch_matches_pattern(base_ref, additional_branches):
            return (
                True,
                f"Skipping PR #{pr_raw['number']} in {repository_full_name} - "
                f"merged to '{base_ref}' (not default branch '{default_branch}' or additional acceptable branches)",
            )

    # Check if repo is inactive
    repo_metadata = master_repositories[repo_key]
    inactive_at = repo_metadata.get("inactiveAt")
    if inactive_at is not None:
        inactive_dt = datetime.fromisoformat(inactive_at.rstrip("Z")).replace(tzinfo=timezone.utc)
        # Skip PR if it was merged at or after the repo became inactive
        if merged_dt >= inactive_dt:
            return (
                True,
                f"Skipping PR #{pr_raw['number']} in {repository_full_name} - PR was merged at/after repo became inactive (merged: {merged_dt.isoformat()}, inactive: {inactive_dt.isoformat()})",
            )

    # All checks passed
    return (False, None)


def get_user_merged_prs_graphql(
    user_id: str, token: str, master_repositories: dict[str, dict], max_prs: int = 1000
) -> PRCountResult:
    """
    Get all merged PRs for a user across all repositories using GraphQL API with pagination.

    Args:
        user_id (str): GitHub user ID (numeric)
        token (str): GitHub PAT
        master_repositories (dict[str, dict]): The dict of repositories (name -> {weight, inactiveAt})
        max_prs (int): Maximum number of PRs to fetch across all pages

    Returns:
        PRCountResult containing:
            - valid_prs: List of valid merged PRs (passed all filters)
            - open_pr_count: Count of open PRs in active repos
            - merged_pr_count: Count of merged PRs after MERGE_SUCCESS_RATIO_APPLICATION_DATE
            - closed_pr_count: Count of closed (not merged) PRs within lookback period
    """

    bt.logging.info("*****Fetching merged PRs*****")

    if not user_id or user_id == "None":
        bt.logging.error("Invalid user_id provided to get_user_merged_prs_graphql")
        return PRCountResult(valid_prs=[], open_pr_count=0, merged_pr_count=0, closed_pr_count=0)

    # Calculate date filter
    date_filter = datetime.now(timezone.utc) - timedelta(days=MERGED_PR_LOOKBACK_DAYS)

    # Convert numeric user ID to GraphQL global node ID
    global_user_id = base64.b64encode(f"04:User{user_id}".encode()).decode()

    all_valid_prs = []
    open_pr_count = 0
    merged_pr_count = 0  # Merged PRs after MERGE_SUCCESS_RATIO_APPLICATION_DATE
    closed_pr_count = 0  # Closed (not merged) within lookback period
    cursor = None

    # Build list of active repositories (those without an inactiveAt timestamp)
    # Keys are already normalized to lowercase
    active_repositories = [
        repo_full_name for repo_full_name, metadata in master_repositories.items() if metadata.get("inactiveAt") is None
    ]

    try:
        while len(all_valid_prs) < max_prs:
            # graphql query
            response = get_github_graphql_query(token, global_user_id, all_valid_prs, max_prs, cursor)
            if not response:
                return PRCountResult(
                    valid_prs=all_valid_prs,
                    open_pr_count=open_pr_count,
                    merged_pr_count=merged_pr_count,
                    closed_pr_count=closed_pr_count,
                )
            data = response.json()

            if 'errors' in data:
                bt.logging.error(f"GraphQL errors: {data['errors']}")
                break

            # Extract user data from node query
            user_data = data.get('data', {}).get('node')

            if not user_data:
                bt.logging.warning("User not found or no pull requests")
                break

            pr_data = user_data.get('pullRequests', {})
            prs = pr_data.get('nodes', [])
            page_info = pr_data.get('pageInfo', {})

            # Process PRs from this page
            for pr_raw in prs:
                repository_full_name = f"{pr_raw['repository']['owner']['login']}/{pr_raw['repository']['name']}"
                pr_state = pr_raw['state']

                # Process non-merged PRs (OPEN or CLOSED without merge)
                open_delta, closed_delta = _process_non_merged_pr(
                    pr_raw, repository_full_name, pr_state, date_filter, active_repositories
                )
                open_pr_count += open_delta
                closed_pr_count += closed_delta

                # Skip if not a merged PR
                if not pr_raw['mergedAt']:
                    continue

                # Parse merge date
                merged_dt = datetime.fromisoformat(pr_raw['mergedAt'].rstrip("Z")).replace(tzinfo=timezone.utc)

                # Validate merged PR against all criteria
                should_skip, skip_reason = _should_skip_merged_pr(
                    pr_raw, repository_full_name, master_repositories, date_filter, merged_dt
                )

                if should_skip:
                    bt.logging.debug(skip_reason)
                    continue

                # PR passed all validation checks
                base_ref = pr_raw['baseRefName']
                bt.logging.info(f"Accepting PR #{pr_raw['number']} in {repository_full_name} - merged to '{base_ref}'")

                # Increment merged_pr_count if merged after MERGE_SUCCESS_RATIO_APPLICATION_DATE
                if merged_dt > MERGE_SUCCESS_RATIO_APPLICATION_DATE:
                    merged_pr_count += 1

                # Consider PR valid if all checks passed
                all_valid_prs.append(pr_raw)

            # Check if we should continue pagination
            if not page_info.get('hasNextPage') or len(prs) == 0:
                break

            cursor = page_info.get('endCursor')

        bt.logging.info(
            f"Found {len(all_valid_prs)} valid merged PRs, {open_pr_count} open PRs, "
            f"{merged_pr_count} merged PRs, {closed_pr_count} closed PRs."
        )
        return PRCountResult(
            valid_prs=all_valid_prs,
            open_pr_count=open_pr_count,
            merged_pr_count=merged_pr_count,
            closed_pr_count=closed_pr_count,
        )

    except Exception as e:
        bt.logging.error(f"Error fetching PRs via GraphQL for user: {e}")
        return PRCountResult(
            valid_prs=all_valid_prs,
            open_pr_count=open_pr_count,
            merged_pr_count=merged_pr_count,
            closed_pr_count=closed_pr_count,
        )
