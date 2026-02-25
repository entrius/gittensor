# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared GitHub credential validation used by multiple validator subsystems."""

from typing import Optional, Tuple

from gittensor.constants import MIN_GITHUB_ACCOUNT_AGE
from gittensor.utils.github_api_tools import (
    get_github_account_age_days,
    get_github_id,
)


def validate_github_credentials(uid: int, pat: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Validate PAT and return (github_id, error_reason) tuple."""
    if not pat:
        return None, f'No Github PAT provided by miner {uid}'

    github_id = get_github_id(pat)
    if not github_id:
        return None, f"No Github id found for miner {uid}'s PAT"

    account_age = get_github_account_age_days(pat)
    if not account_age:
        return None, f'Could not determine Github account age for miner {uid}'
    if account_age < MIN_GITHUB_ACCOUNT_AGE:
        return None, f"Miner {uid}'s Github account too young ({account_age} < {MIN_GITHUB_ACCOUNT_AGE} days)"

    return github_id, None
