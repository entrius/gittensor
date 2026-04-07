# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared GitHub credential validation used by multiple validator subsystems."""

from typing import Optional, Tuple

from gittensor.utils.github_api_tools import get_github_id


def validate_github_credentials(uid: int, pat: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Validate PAT and return (github_id, error_reason) tuple."""
    if not pat:
        return None, f'No Github PAT provided by miner {uid}'

    github_id = get_github_id(pat)
    if not github_id:
        return None, f"No Github id found for miner {uid}'s PAT"

    return github_id, None
