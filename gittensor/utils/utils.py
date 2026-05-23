"""
GitTensor Utilities
"""

import os


def backoff_seconds(attempt: int, base: int = 5, cap: int = 30) -> int:
    return min(base * (2**attempt), cap)


def get_contract_address() -> str:
    """Get contract address. Override via CONTRACT_ADDRESS env var for dev/testing.

    Returns:
        Contract address string (env var override or constants.py default)
    """
    from gittensor.constants import CONTRACT_ADDRESS

    return os.environ.get('CONTRACT_ADDRESS') or CONTRACT_ADDRESS
