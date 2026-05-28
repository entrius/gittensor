# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared GitHub credential validation used by multiple validator subsystems."""

from dataclasses import dataclass
from typing import Optional, Tuple

from gittensor.utils.github_api_tools import (
    GitHubIdentityStatus,
    get_github_identity,
    normalize_github_pat,
)


@dataclass(frozen=True)
class GitHubCredentialValidation:
    github_id: Optional[str]
    error: Optional[str]
    transient_failure: bool = False


def validate_github_credentials(uid: int, pat: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Validate PAT and return (github_id, error_reason) tuple."""
    validation = validate_github_credentials_result(uid, pat)
    return validation.github_id, validation.error


def validate_github_credentials_result(
    uid: int,
    pat: Optional[str],
    stored_github_id: Optional[str] = None,
) -> GitHubCredentialValidation:
    """Validate PAT and expose transient /user lookup failures to scoring."""
    pat = normalize_github_pat(pat)
    if not pat:
        return GitHubCredentialValidation(None, f'No Github PAT provided by miner {uid}')

    identity = get_github_identity(pat)
    if identity.status is GitHubIdentityStatus.VALID:
        return GitHubCredentialValidation(identity.github_id, None)

    if identity.status is GitHubIdentityStatus.TRANSIENT_FAILURE:
        if stored_github_id and stored_github_id != '0':
            return GitHubCredentialValidation(stored_github_id, None, transient_failure=True)
        return GitHubCredentialValidation(
            None,
            f'Could not validate Github id for miner {uid}: GitHub /user lookup failed transiently',
            transient_failure=True,
        )

    return GitHubCredentialValidation(None, f"No Github id found for miner {uid}'s PAT")
