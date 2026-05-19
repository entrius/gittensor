from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from gittensor.validator.issue_competitions.contract_client import ContractIssue, IssueStatus


@dataclass(frozen=True)
class BountyVoteDecision:
    issue_id: int
    repository_full_name: str
    issue_number: int
    action: str
    reason: str
    is_closed: Optional[bool] = None
    solver_github_id: Optional[str] = None
    pr_number: Optional[int] = None
    solver_lookup_failed: bool = False
    solver_hotkey: Optional[str] = None
    solver_coldkey: Optional[str] = None
    cancel_reason: Optional[str] = None


def explain_bounty_vote(
    issue: ContractIssue,
    github_state: Optional[Dict[str, Any]],
    registered_miners: Dict[str, str],
    coldkey_lookup: Callable[[str], Optional[str]],
) -> BountyVoteDecision:
    """Explain the vote action the validator would take for one bounty issue."""
    base = {
        'issue_id': issue.id,
        'repository_full_name': issue.repository_full_name,
        'issue_number': issue.issue_number,
    }

    issue_status = getattr(issue, 'status', IssueStatus.ACTIVE)
    if issue_status is not IssueStatus.ACTIVE:
        status_name = issue_status.name if isinstance(issue_status, IssueStatus) else str(issue_status)
        return BountyVoteDecision(
            **base,
            action='skip',
            reason=f'Issue status is {status_name}, not ACTIVE',
        )

    if github_state is None:
        return BountyVoteDecision(
            **base,
            action='skip',
            reason='Could not check GitHub issue state',
        )

    is_closed = bool(github_state.get('is_closed'))
    if not is_closed:
        return BountyVoteDecision(
            **base,
            action='skip',
            reason='Issue is still open on GitHub',
            is_closed=False,
        )

    solver_github_id_raw = github_state.get('solver_github_id')
    solver_github_id = str(solver_github_id_raw) if solver_github_id_raw else None
    pr_number = github_state.get('pr_number')
    solver_lookup_failed = bool(github_state.get('solver_lookup_failed'))

    if solver_lookup_failed:
        return BountyVoteDecision(
            **base,
            action='skip',
            reason='Solver lookup failed',
            is_closed=True,
            solver_github_id=solver_github_id,
            pr_number=pr_number,
            solver_lookup_failed=True,
        )

    if not solver_github_id:
        cancel_reason = 'Issue closed without identifiable solver'
        return BountyVoteDecision(
            **base,
            action='vote_cancel',
            reason=cancel_reason,
            is_closed=True,
            pr_number=pr_number,
            cancel_reason=cancel_reason,
        )

    miner_hotkey = registered_miners.get(solver_github_id)
    if not miner_hotkey:
        cancel_reason = f'Issue closed externally (not by a registered miner, solver: {solver_github_id})'
        return BountyVoteDecision(
            **base,
            action='vote_cancel',
            reason=cancel_reason,
            is_closed=True,
            solver_github_id=solver_github_id,
            pr_number=pr_number,
            cancel_reason=cancel_reason,
        )

    miner_coldkey = coldkey_lookup(miner_hotkey)
    if not miner_coldkey:
        return BountyVoteDecision(
            **base,
            action='skip',
            reason=f'Could not resolve coldkey for registered solver hotkey {miner_hotkey}',
            is_closed=True,
            solver_github_id=solver_github_id,
            pr_number=pr_number,
            solver_hotkey=miner_hotkey,
        )

    return BountyVoteDecision(
        **base,
        action='vote_solution',
        reason='Registered solver identity and coldkey resolved',
        is_closed=True,
        solver_github_id=solver_github_id,
        pr_number=pr_number,
        solver_hotkey=miner_hotkey,
        solver_coldkey=miner_coldkey,
    )
