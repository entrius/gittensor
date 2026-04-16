# Entrius 2025
import fnmatch
import time
from typing import Any, Dict, List, Optional

import bittensor as bt
import requests

from gittensor.classes import FileChange
from gittensor.constants import BASE_GITHUB_API_URL, MAINTAINER_ASSOCIATIONS


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
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=30)
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
                backoff_delay = min(5 * (2**attempt), 30)
                bt.logging.warning(
                    f'Compare API for {repository} failed with status {response.status_code} '
                    f'(attempt {attempt + 1}/{max_attempts}), retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)

        except requests.exceptions.RequestException as e:
            if attempt < max_attempts - 1:
                backoff_delay = min(5 * (2**attempt), 30)
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
