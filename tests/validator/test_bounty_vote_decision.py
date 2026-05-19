from gittensor.validator.issue_competitions.contract_client import ContractIssue, IssueStatus
from gittensor.validator.issue_competitions.vote_decision import explain_bounty_vote


def _issue(status: IssueStatus = IssueStatus.ACTIVE) -> ContractIssue:
    return ContractIssue(
        id=7,
        github_url_hash=b'0' * 32,
        repository_full_name='entrius/gittensor',
        issue_number=12,
        bounty_amount=1_000_000_000,
        target_bounty=1_000_000_000,
        status=status,
        registered_at_block=1,
        is_fully_funded=True,
    )


def test_explains_solution_vote_when_solver_is_registered():
    decision = explain_bounty_vote(
        issue=_issue(),
        github_state={
            'is_closed': True,
            'solver_github_id': '999',
            'pr_number': 42,
            'solver_lookup_failed': False,
        },
        registered_miners={'999': 'hk999'},
        coldkey_lookup=lambda hotkey: 'ck999' if hotkey == 'hk999' else None,
    )

    assert decision.action == 'vote_solution'
    assert decision.solver_hotkey == 'hk999'
    assert decision.solver_coldkey == 'ck999'
    assert decision.pr_number == 42


def test_explains_cancel_when_closed_without_registered_solver():
    decision = explain_bounty_vote(
        issue=_issue(),
        github_state={
            'is_closed': True,
            'solver_github_id': '999',
            'pr_number': 42,
            'solver_lookup_failed': False,
        },
        registered_miners={},
        coldkey_lookup=lambda _hotkey: None,
    )

    assert decision.action == 'vote_cancel'
    assert decision.cancel_reason == 'Issue closed externally (not by a registered miner, solver: 999)'


def test_explains_skip_when_solver_lookup_failed():
    decision = explain_bounty_vote(
        issue=_issue(),
        github_state={'is_closed': True, 'solver_lookup_failed': True},
        registered_miners={},
        coldkey_lookup=lambda _hotkey: None,
    )

    assert decision.action == 'skip'
    assert decision.reason == 'Solver lookup failed'
    assert decision.solver_lookup_failed is True
