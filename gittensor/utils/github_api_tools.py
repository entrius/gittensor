# Entrius 2025
import base64
import time
from datetime import datetime, timedelta, timezone
from typing import DefaultDict, List, Optional, Tuple

import bittensor as bt
import requests

from gittensor.classes import FileChange
from gittensor.constants import BASE_GITHUB_API_URL
from gittensor.utils.utils import mask_secret
from gittensor.validator.utils.config import MERGED_PR_LOOKBACK_DAYS


def make_headers(token: str):
    '''
    helper function for formatting headers for requests
    '''
    return {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}


def get_github_username(token: str) -> Optional[str]:
    '''
    Get username using token
    Args:
        token (str): Github pat
    Returns:
        username: Str or None if PAT is invalid or something went wrong
    '''
    headers = make_headers(token)
    try:
        response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get('login', None)
    except Exception as e:
        bt.logging.warning(f"Could not fetch GitHub username: {e}")
    return None


def get_github_id(token: str) -> Optional[str]:
    '''
    Get id using token
    Args:
        token (str): Github pat
    Returns:
        user_id: Str or None if PAT is invalid or something went wrong
    '''
    headers = make_headers(token)

    # Retry logic for timeout issues
    for attempt in range(3):
        try:
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=30)
            if response.status_code == 200:
                user_id = response.json().get('id', None)
                if user_id:
                    return str(user_id)  # Ensure it's returned as string
        except Exception as e:
            bt.logging.warning(f"Could not fetch GitHub id (attempt {attempt + 1}/3): {e}")
            if attempt < 2:  # Don't sleep on last attempt
                time.sleep(2)
    return None


def get_github_account_age_days(token: str) -> Optional[int]:
    '''
    Get GitHub account age in days
    Args:
        token (str): Github pat
    Returns:
        age_days: Int, number of days since account creation, or None if PAT is invalid or something went wrong
    '''
    headers = make_headers(token)

    # Retry logic for timeout issues
    for attempt in range(3):
        try:
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=30)
            if response.status_code == 200:
                user_data = response.json()
                created_at = user_data.get('created_at')
                if created_at:
                    created_dt = datetime.fromisoformat(created_at.rstrip("Z")).replace(tzinfo=timezone.utc)
                    now_dt = datetime.now(timezone.utc)
                    age_days = (now_dt - created_dt).days
                    return age_days
        except Exception as e:
            bt.logging.warning(f"Could not fetch GitHub account age (attempt {attempt + 1}/3): {e}")
            if attempt < 2:  # Don't sleep on last attempt
                time.sleep(2)
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
        bt.logging.error(
            f"Error getting file changes for PR #{mask_secret(str(pr_number))} in {mask_secret(repository)}: {e}"
        )
        return []


def get_user_merged_prs_graphql(
    user_id: str, token: str, master_repositories: dict[str, dict], max_prs: int = 1000
) -> Tuple[List[DefaultDict], int]:
    """
    Get all merged PRs for a user across all repositories using GraphQL API with pagination.

    Args:
        user_id (str): GitHub user ID (numeric)
        token (str): GitHub PAT
        master_repositories (dict[str, dict]): The dict of repositories (name -> {weight, inactiveAt})
        max_prs (int): Maximum number of PRs to fetch across all pages

    Returns:
        List[PullRequest]: List of PullRequest objects
        int: Count of total open PRs for a miner
    """

    if not user_id or user_id == "None":
        bt.logging.error("Invalid user_id provided to get_user_merged_prs_graphql")
        return ([], 0)

    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    # Calculate date filter
    date_filter = datetime.now(timezone.utc) - timedelta(days=MERGED_PR_LOOKBACK_DAYS)

    # Convert numeric user ID to GraphQL global node ID
    global_user_id = base64.b64encode(f"04:User{user_id}".encode()).decode()

    query = """
    query($userId: ID!, $limit: Int!, $cursor: String) {
      node(id: $userId) {
        ... on User {
          pullRequests(first: $limit, states: [MERGED, OPEN], orderBy: {field: UPDATED_AT, direction: DESC}, after: $cursor) {
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
              author {
                login
              }
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
                }
              }
            }
          }
        }
      }
    }
    """

    all_valid_prs = []
    open_pr_count = 0
    cursor = None
    page_size = min(100, max_prs)  # GitHub GraphQL max is 100 per page

    # Build list of active repositories (those without an inactiveAt timestamp)
    active_repositories = [
        repo_full_name for repo_full_name, metadata in master_repositories.items() if metadata["inactiveAt"] is None
    ]

    try:
        while len(all_valid_prs) < max_prs:
            variables = {
                "userId": global_user_id,
                "limit": min(page_size, max_prs - len(all_valid_prs)),
                "cursor": cursor,
            }

            response = requests.post(
                f'{BASE_GITHUB_API_URL}/graphql',
                headers=headers,
                json={"query": query, "variables": variables},
                timeout=15,
            )

            if response.status_code != 200:
                bt.logging.error(f"GraphQL request failed with status {response.status_code}: {response.text}")
                break

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
                # Check if it's an open PR and count it
                if pr_raw['state'] == 'OPEN':
                    # Check if in tracked repositories
                    if repository_full_name in active_repositories:
                        open_pr_count += 1
                    continue  # Skip further processing for open PRs

                # Skip if not a merged pr
                if not pr_raw['mergedAt']:
                    continue

                # Filter by master_repositories
                if repository_full_name not in master_repositories.keys():
                    bt.logging.debug(
                        f"Skipping PR #{mask_secret(pr_raw['number'])} in {mask_secret(repository_full_name)} - not in master_repositories"
                    )
                    continue

                # Parse merge date and filter by time window
                merged_dt = datetime.fromisoformat(pr_raw['mergedAt'].rstrip("Z")).replace(tzinfo=timezone.utc)
                if merged_dt < date_filter:
                    # stop once we hit a pr before lookback window
                    bt.logging.debug(f"Reached PRs older than {MERGED_PR_LOOKBACK_DAYS} days, stopping pagination")
                    return (all_valid_prs, open_pr_count)

                # Skip if PR was merged by the same person who created it (self-merge)
                if pr_raw['mergedBy'] and pr_raw['author']['login'] == pr_raw['mergedBy']['login']:
                    bt.logging.debug(
                        f"Skipping PR #{mask_secret(pr_raw['number'])} in {mask_secret(repository_full_name)} - self-merged PR"
                    )
                    continue

                # Skip if PR was not merged to the default branch
                default_branch = (
                    pr_raw['repository']['defaultBranchRef']['name']
                    if pr_raw['repository']['defaultBranchRef']
                    else 'main'
                )
                base_ref = pr_raw['baseRefName']
                if base_ref != default_branch:
                    bt.logging.debug(
                        f"Skipping PR #{mask_secret(pr_raw['number'])} in {mask_secret(repository_full_name)} - not merged to the default (prod) branch"
                    )
                    continue

                repo_metadata = master_repositories[repository_full_name]
                inactive_at = repo_metadata.get("inactiveAt", None)
                # if repo is inactive
                if inactive_at is not None:
                    inactive_dt = datetime.fromisoformat(inactive_at.rstrip("Z")).replace(tzinfo=timezone.utc)
                    # Skip PR if it was merged at or after the repo became inactive
                    if merged_dt >= inactive_dt:
                        bt.logging.debug(
                            f"Skipping PR #{mask_secret(pr_raw['number'])} in {mask_secret(repository_full_name)} - PR was merged at/after repo became inactive (merged: {merged_dt.isoformat()}, inactive: {inactive_dt.isoformat()})"
                        )
                        continue

                # consider PR valid if all checks passed
                all_valid_prs.append(pr_raw)

            # Check if we should continue pagination
            if not page_info.get('hasNextPage') or len(prs) == 0:
                break

            cursor = page_info.get('endCursor')

        bt.logging.info(f"Found {len(all_valid_prs)} valid merged PRs and {open_pr_count} open PRs for user")
        return (all_valid_prs, open_pr_count)

    except Exception as e:
        bt.logging.error(f"Error fetching PRs via GraphQL for user: {e}")
        return ([], 0)
