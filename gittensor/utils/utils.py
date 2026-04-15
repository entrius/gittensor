"""
GitTensor Utilities
"""

from typing import Dict


def parse_repo_name(repo_data: Dict):
    """Normalizes and converts repository name from dict"""
    return f'{repo_data["owner"]["login"]}/{repo_data["name"]}'.lower()
