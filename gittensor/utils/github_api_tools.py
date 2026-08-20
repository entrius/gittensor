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


# GraphQL fragment used by issue submissions / PR discovery.
@dataclass
class FileContentPair:
    """Holds both old (base) and new (head) content for a file."""

    old_content: Optional[str]  # None for new files
    new_content: Optional[str]  # None for deleted files
