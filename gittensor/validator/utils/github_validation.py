# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared GitHub credential validation used by multiple validator subsystems."""

from typing import Optional, Tuple

from gittensor.utils.github_api_tools import get_github_id


def validate_github_credentials(
    uid: int, pat: Optional[str]
) -> Tuple[Optional[str], Optional[str], bool]:
    """Validate PAT and return (github_id, error_reason, is_transient) tuple.

    is_transient is True when the failure is due to a transient GitHub API
    error (timeout, 5xx, parse failure) rather than an invalid PAT, so that
    callers can choose to fall back to cached evaluation data.
    """
    if not pat:
        return None, f'No Github PAT provided by miner {uid}', False

    github_id, is_transient = get_github_id(pat)
    if not github_id:
        return None, f"No Github id found for miner {uid}'s PAT", is_transient

    return github_id, None, False
