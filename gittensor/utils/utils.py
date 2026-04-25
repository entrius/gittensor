"""
GitTensor Utilities
"""

import hashlib
from typing import Dict


def mask_secret(secret: str, length: int = 5) -> str:
    """Return a short SHA-256 hash of a secret for logging."""
    h = hashlib.sha256(str(secret).encode('utf-8')).hexdigest()
    return f'<masked:{h[:length]}>'


def parse_repo_name(repo_data: Dict):
    """Normalizes and converts repository name from dict"""
    owner = repo_data.get("owner")
    owner_login = owner.get("login") if owner else "deleted-user"
    return f'{owner_login}/{repo_data["name"]}'.lower()
