# Entrius 2025

"""External state checks for predictions (on-chain + GitHub)."""

from typing import TYPE_CHECKING

import bittensor as bt

from gittensor.validator.issue_competitions.contract_client import (
    IssueCompetitionContractClient,
    IssueStatus,
)
from gittensor.validator.utils.config import GITTENSOR_VALIDATOR_PAT

if TYPE_CHECKING:
    from neurons.validator import Validator


def check_issue_active(validator: 'Validator', issue_id: int) -> str | None:
    """Verify issue is active on-chain. Returns error string or None."""
    try:
        from gittensor.validator.utils.issue_competitions import get_contract_address

        contract_addr = get_contract_address()
        if not contract_addr:
            return 'Issue bounties not configured on this validator'

        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=validator.subtensor,
        )
        issue = client.get_issue(issue_id)
        if issue is None:
            return f'Issue {issue_id} not found on-chain'
        if issue.status != IssueStatus.ACTIVE:
            return f'Issue {issue_id} is not active (status: {issue.status.name})'
    except Exception as e:
        bt.logging.warning(f'Failed to check issue state for {issue_id}: {e}')
        return f'Could not verify issue state: {e}'

    return None


def check_prs_open(repository: str, issue_id: int, predictions: dict[int, float]) -> str | None:
    """Verify all predicted PRs are still open on GitHub. Returns error string or None."""
    if not GITTENSOR_VALIDATOR_PAT:
        bt.logging.warning('No GITTENSOR_VALIDATOR_PAT, skipping PR open check')
        return None

    try:
        from gittensor.utils.github_api_tools import find_prs_for_issue

        open_prs = find_prs_for_issue(repository, issue_id, open_only=True, token=GITTENSOR_VALIDATOR_PAT)
        open_pr_numbers = {pr.get('number') if isinstance(pr, dict) else getattr(pr, 'number', None) for pr in open_prs}

        for pr_number in predictions:
            if pr_number not in open_pr_numbers:
                return f'PR #{pr_number} is not open on {repository}'

    except Exception as e:
        bt.logging.warning(f'Failed to check PR state for {repository}: {e}')
        return None

    return None
