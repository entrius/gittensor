# Copyright © 2025 Entrius
import os
import sys
import time
from typing import Optional

import bittensor as bt
import requests

from gittensor.constants import BASE_GITHUB_API_URL

# Token cache to avoid re-validating on every request
_cached_token: Optional[str] = None
_cached_validity: Optional[bool] = None
_last_check_time: float = 0.0
TOKEN_CACHE_MAX_AGE_SECONDS = 300  # Re-validate every 5 minutes


def init() -> bool:
    """Initialize and check if GitHub token exists in environment

    Returns:
        bool: Always returns True if token exists, otherwise exits

    Raises:
        SystemExit: If GITTENSOR_MINER_PAT environment variable is not set
    """
    token = os.getenv('GITTENSOR_MINER_PAT')
    if not token:
        bt.logging.error('GitHub Token NOT FOUND. Please set GITTENSOR_MINER_PAT environment variable.')
        bt.logging.error('Refer to README.md and the miner setup for more information.')
        sys.exit(1)

    bt.logging.success('Found GITTENSOR_MINER_PAT in environment')
    return True


def load_token(quiet: bool = False) -> Optional[str]:
    """
    Load GitHub token from environment variable with caching.

    Caches the token validity for 5 minutes to avoid excessive GitHub API calls.
    Only re-validates if the token changes or cache expires.

    Returns:
        Optional[str]: The GitHub access token string if valid, None otherwise
    """
    global _cached_token, _cached_validity, _last_check_time

    if not quiet:
        bt.logging.info('Loading GitHub token from environment.')

    access_token = os.getenv('GITTENSOR_MINER_PAT')

    if not access_token:
        if not quiet:
            bt.logging.error('No GitHub token found in GITTENSOR_MINER_PAT environment variable!')
        return None

    # Check if token changed or cache expired
    current_time = time.time()
    cache_valid = (
        _cached_token == access_token and
        _cached_validity is not None and
        (current_time - _last_check_time) < TOKEN_CACHE_MAX_AGE_SECONDS
    )

    if cache_valid:
        if not quiet:
            bt.logging.debug('Using cached GitHub token validity.')
        return access_token if _cached_validity else None

    # Validate and cache result
    is_valid = is_token_valid(access_token)
    _cached_token = access_token
    _cached_validity = is_valid
    _last_check_time = current_time

    if is_valid:
        if not quiet:
            bt.logging.info('GitHub token loaded successfully and is valid.')
        return access_token

    if not quiet:
        bt.logging.error('GitHub token is invalid or expired.')
    return None


def is_token_valid(token: str) -> bool:
    """
    Test if a GitHub token is valid by making a simple API call.

    Args:
        token (str): GitHub personal access token to validate

    Returns:
        bool: True if valid token, False otherwise
    """
    headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}

    for attempt in range(3):
        try:
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=15)
            if response.status_code == 200:
                return True
            elif response.status_code == 401:
                # Unauthorized - token is invalid
                return False
            elif response.status_code == 429:
                # Rate limited - wait and retry with backoff
                bt.logging.warning(f'GitHub API rate limited, waiting...')
                if attempt < 2:
                    time.sleep(2 ** attempt + 5)  # Exponential backoff + 5s
                    continue
                return False
            else:
                # Other errors - log and retry
                bt.logging.warning(f'GitHub API returned status {response.status_code}')
        except Exception as e:
            bt.logging.warning(f'Error validating GitHub token (attempt {attempt + 1}/3): {e}')
            if attempt < 2:
                time.sleep(2 ** attempt)

    return False
