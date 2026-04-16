"""
GitTensor Utilities
"""

import os
from typing import Dict


def parse_repo_name(repo_data: Dict):
    """Normalizes and converts repository name from dict"""
    return f'{repo_data["owner"]["login"]}/{repo_data["name"]}'.lower()


def get_contract_address() -> str:
    """Get contract address. Override via CONTRACT_ADDRESS env var for dev/testing.

    Returns:
        Contract address string (env var override or constants.py default)
    """
    from gittensor.constants import CONTRACT_ADDRESS

    return os.environ.get('CONTRACT_ADDRESS') or CONTRACT_ADDRESS
