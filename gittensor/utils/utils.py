"""
GitTensor Utilities
"""

import os
from typing import Dict, Optional


def parse_repo_name(repo_data: Dict) -> Optional[str]:
    """Normalizes and converts repository name from dict.

    Returns None if owner is missing (e.g. deleted fork-owner account).
    """
    owner = (repo_data.get('owner') or {}).get('login')
    name = repo_data.get('name')
    return f'{owner}/{name}'.lower() if owner and name else None


def get_contract_address() -> str:
    """Get contract address. Override via CONTRACT_ADDRESS env var for dev/testing.

    Returns:
        Contract address string (env var override or constants.py default)
    """
    from gittensor.constants import CONTRACT_ADDRESS

    return os.environ.get('CONTRACT_ADDRESS') or CONTRACT_ADDRESS
