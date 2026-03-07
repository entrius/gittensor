# Copyright © 2025 Entrius
import os
import sys
import time
from typing import Optional, Tuple

import bittensor as bt
import requests

from gittensor.constants import BASE_GITHUB_API_URL

# Token validation retry configuration
MAX_RETRIES: int = 3
INITIAL_BACKOFF_SECONDS: float = 2.0
BACKOFF_MULTIPLIER: float = 2.0

# Rate limit thresholds
RATE_LIMIT_REMAINING_WARN: int = 100


def init() -> bool:
    """Initialize and check if GitHub token exists in environment.

    Returns:
        bool: Always returns True if token exists, otherwise exits.

    Raises:
        SystemExit: If GITTENSOR_MINER_PAT environment variable is not set.
    """
    token = os.getenv('GITTENSOR_MINER_PAT')
    if not token:
        bt.logging.error('GitHub Token NOT FOUND. Please set GITTENSOR_MINER_PAT environment variable.')
        bt.logging.error('Refer to README.md and the miner setup for more information.')
        sys.exit(1)

    bt.logging.success('Found GITTENSOR_MINER_PAT in environment')
    return True


def load_token() -> Optional[str]:
    """Load GitHub token from environment variable and validate it.

    Reads the token from the ``GITTENSOR_MINER_PAT`` environment variable,
    validates it against the GitHub API, and returns it if valid.

    Returns:
        Optional[str]: The GitHub access token string if valid, None otherwise.
    """
    bt.logging.info('Loading GitHub token from environment.')

    access_token = os.getenv('GITTENSOR_MINER_PAT')

    if not access_token:
        bt.logging.error('No GitHub token found in GITTENSOR_MINER_PAT environment variable!')
        return None

    # Test if token is still valid
    valid, message = validate_token(access_token)
    if valid:
        bt.logging.info(f'GitHub token loaded successfully and is valid. {message}')
        return access_token

    bt.logging.error(f'GitHub token is invalid or expired. {message}')
    return None


def validate_token(token: str) -> Tuple[bool, str]:
    """Validate a GitHub token and return status with diagnostic info.

    Makes an authenticated request to the GitHub ``/user`` endpoint to verify
    token validity. Implements exponential backoff on transient failures and
    provides diagnostic information about rate limits.

    Args:
        token: GitHub personal access token to validate.

    Returns:
        A tuple of ``(is_valid, message)`` where *message* contains
        diagnostic information such as the authenticated username or
        the reason for failure.
    """
    headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
    backoff = INITIAL_BACKOFF_SECONDS

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=15)

            if response.status_code == 200:
                username = response.json().get('login', 'unknown')
                _check_rate_limit(response)
                return True, f'Authenticated as {username}'

            if response.status_code == 401:
                return False, 'Token is invalid or revoked (HTTP 401)'

            if response.status_code == 403:
                remaining = response.headers.get('X-RateLimit-Remaining', '?')
                reset = response.headers.get('X-RateLimit-Reset')
                if remaining == '0' and reset:
                    reset_time = time.strftime('%H:%M:%S UTC', time.gmtime(int(reset)))
                    return False, f'Rate limited (HTTP 403). Resets at {reset_time}'
                return False, f'Forbidden (HTTP 403). Rate limit remaining: {remaining}'

            bt.logging.warning(
                f'Unexpected status {response.status_code} validating token (attempt {attempt + 1}/{MAX_RETRIES})'
            )

        except requests.exceptions.Timeout:
            bt.logging.warning(f'Timeout validating GitHub token (attempt {attempt + 1}/{MAX_RETRIES})')

        except requests.exceptions.ConnectionError as e:
            bt.logging.warning(f'Connection error validating GitHub token (attempt {attempt + 1}/{MAX_RETRIES}): {e}')

        except requests.exceptions.RequestException as e:
            bt.logging.warning(f'Request error validating GitHub token (attempt {attempt + 1}/{MAX_RETRIES}): {e}')

        if attempt < MAX_RETRIES - 1:
            bt.logging.info(f'Retrying in {backoff:.1f}s...')
            time.sleep(backoff)
            backoff *= BACKOFF_MULTIPLIER

    return False, f'Failed after {MAX_RETRIES} attempts'


def is_token_valid(token: str) -> bool:
    """Test if a GitHub token is valid by making a simple API call.

    This is a convenience wrapper around :func:`validate_token` that
    returns only the boolean result.

    Args:
        token: GitHub personal access token to validate.

    Returns:
        bool: True if valid token, False otherwise.
    """
    valid, _ = validate_token(token)
    return valid


def _check_rate_limit(response: requests.Response) -> None:
    """Log a warning if the GitHub API rate limit is running low.

    Args:
        response: A successful GitHub API response whose headers
            contain rate-limit information.
    """
    remaining = response.headers.get('X-RateLimit-Remaining')
    limit = response.headers.get('X-RateLimit-Limit')
    if remaining is not None:
        try:
            remaining_int = int(remaining)
            if remaining_int < RATE_LIMIT_REMAINING_WARN:
                bt.logging.warning(
                    f'GitHub API rate limit running low: {remaining}/{limit} requests remaining'
                )
        except ValueError:
            pass
