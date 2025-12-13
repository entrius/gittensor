# Copyright © 2025 Entrius
import os
import sys
import time

import bittensor as bt
import requests

from gittensor.constants import BASE_GITHUB_API_URL


def init():
    """Initialize and check if GitHub token exists in environment"""
    token = os.getenv('GITTENSOR_MINER_PAT')
    if not token:
        bt.logging.error("GitHub Token NOT FOUND. Please set GITTENSOR_MINER_PAT environment variable.")
        bt.logging.error("Refer to README.md and the miner setup for more information.")
        sys.exit(1)

    bt.logging.success("Found GITTENSOR_MINER_PAT in environment")
    return True


def load_token():
    """
    Load GitHub token from environment variable
    Returns the GitHub access token string.
    """
    bt.logging.info("Loading GitHub token from environment.")

    access_token = os.getenv('GITTENSOR_MINER_PAT')

    if not access_token:
        bt.logging.error("No GitHub token found in GITTENSOR_MINER_PAT environment variable!")
        return None

    # Test if token is still valid
    if is_token_valid(access_token):
        bt.logging.info("GitHub token loaded successfully and is valid.")
        return access_token

    bt.logging.error("GitHub token is invalid or expired.")
    return None


def is_token_valid(token) -> bool:
    """
    Test if a GitHub token is valid by making a simple API call.
    Returns:
        True if valid token, False otherwise
    """
    headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}

    for attempt in range(3):
        try:
            response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=15)
            return response.status_code == 200
        except Exception as e:
            bt.logging.warning(f"Error validating GitHub token (attempt {attempt + 1}/3): {e}")
            if attempt < 2:  # Don't sleep on last attempt
                time.sleep(3)

    return False
