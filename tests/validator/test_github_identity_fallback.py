from typing import Optional
from unittest.mock import patch

from gittensor.utils.github_api_tools import GitHubIdentityResult, GitHubIdentityStatus
from gittensor.validator.oss_contributions.inspections import validate_response_and_initialize_miner_evaluation


def _identity_result(status: GitHubIdentityStatus, github_id: Optional[str] = None) -> GitHubIdentityResult:
    return GitHubIdentityResult(github_id=github_id, status=status)


def test_transient_identity_lookup_uses_stored_id_for_cache_fallback():
    with patch(
        'gittensor.validator.utils.github_validation.get_github_identity',
        return_value=_identity_result(GitHubIdentityStatus.TRANSIENT_FAILURE),
    ):
        evaluation = validate_response_and_initialize_miner_evaluation(
            uid=7,
            hotkey='hk',
            pat='ghp_stored',
            stored_github_id='12345',
        )

    assert evaluation.failed_reason is None
    assert evaluation.github_id == '12345'
    assert evaluation.github_pr_fetch_failed is True
    assert evaluation.should_use_cache_fallback is True


def test_auth_identity_failure_does_not_use_stored_id_for_cache_fallback():
    with patch(
        'gittensor.validator.utils.github_validation.get_github_identity',
        return_value=_identity_result(GitHubIdentityStatus.INVALID_AUTH),
    ):
        evaluation = validate_response_and_initialize_miner_evaluation(
            uid=7,
            hotkey='hk',
            pat='ghp_revoked',
            stored_github_id='12345',
        )

    assert evaluation.failed_reason == "No Github id found for miner 7's PAT"
    assert evaluation.github_id == '0'
    assert evaluation.github_pr_fetch_failed is False
