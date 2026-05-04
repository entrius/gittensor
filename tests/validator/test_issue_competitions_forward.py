#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for issue bounty forward voting policy around solver lookup failures."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gittensor.validator.issue_competitions.forward import issue_competitions


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_validator():
    return SimpleNamespace(
        subtensor=MagicMock(),
        wallet=MagicMock(),
        config=SimpleNamespace(netuid=1),
    )


def _make_issue():
    return SimpleNamespace(
        id=7,
        repository_full_name='owner/repo',
        issue_number=12,
        bounty_amount=1_000_000_000,
    )


def test_solver_lookup_failure_does_not_cancel():
    validator = _make_validator()
    contract_client = MagicMock()
    contract_client.harvest_emissions.return_value = None
    contract_client.get_issues_by_status.return_value = [_make_issue()]

    with (
        patch('gittensor.validator.issue_competitions.forward.GITTENSOR_VALIDATOR_PAT', 'ghp_validator'),
        patch('gittensor.validator.issue_competitions.forward.get_contract_address', return_value='5Contract'),
        patch(
            'gittensor.validator.issue_competitions.forward.check_github_issue_closed',
            return_value={
                'is_closed': True,
                'state_reason': 'completed',
                'solver_github_id': None,
                'pr_number': None,
                'solver_lookup_failed': True,
            },
        ),
        patch(
            'gittensor.validator.issue_competitions.forward.IssueCompetitionContractClient',
            return_value=contract_client,
        ),
    ):
        _run(issue_competitions(cast(Any, validator), {}))

    contract_client.vote_cancel_issue.assert_not_called()
    contract_client.vote_solution.assert_not_called()


def test_no_solver_without_lookup_failure_votes_cancel():
    validator = _make_validator()
    contract_client = MagicMock()
    contract_client.harvest_emissions.return_value = None
    contract_client.get_issues_by_status.return_value = [_make_issue()]
    contract_client.vote_cancel_issue.return_value = True

    with (
        patch('gittensor.validator.issue_competitions.forward.GITTENSOR_VALIDATOR_PAT', 'ghp_validator'),
        patch('gittensor.validator.issue_competitions.forward.get_contract_address', return_value='5Contract'),
        patch(
            'gittensor.validator.issue_competitions.forward.check_github_issue_closed',
            return_value={
                'is_closed': True,
                'state_reason': 'completed',
                'solver_github_id': None,
                'pr_number': None,
                'solver_lookup_failed': False,
            },
        ),
        patch(
            'gittensor.validator.issue_competitions.forward.IssueCompetitionContractClient',
            return_value=contract_client,
        ),
    ):
        _run(issue_competitions(cast(Any, validator), {}))

    contract_client.vote_cancel_issue.assert_called_once_with(
        issue_id=7,
        reason='Issue closed without identifiable solver',
        wallet=validator.wallet,
    )
    contract_client.vote_solution.assert_not_called()


@pytest.mark.parametrize(
    ('state_reason', 'expected_reason'),
    [
        ('not_planned', 'Issue closed as not_planned on GitHub'),
        ('duplicate', 'Issue closed as duplicate on GitHub'),
    ],
)
def test_non_completed_closure_cancels_with_state_reason(state_reason, expected_reason):
    """Issues closed as not_planned/duplicate must vote cancel and never vote solution.

    Regression for issue #979: the gate must hold above the forward layer so that a
    later refactor can't reintroduce solution voting on non-completed closures.
    """
    validator = _make_validator()
    contract_client = MagicMock()
    contract_client.harvest_emissions.return_value = None
    contract_client.get_issues_by_status.return_value = [_make_issue()]
    contract_client.vote_cancel_issue.return_value = True

    with (
        patch('gittensor.validator.issue_competitions.forward.GITTENSOR_VALIDATOR_PAT', 'ghp_validator'),
        patch('gittensor.validator.issue_competitions.forward.get_contract_address', return_value='5Contract'),
        patch(
            'gittensor.validator.issue_competitions.forward.check_github_issue_closed',
            return_value={
                'is_closed': True,
                'state_reason': state_reason,
                'solver_github_id': None,
                'pr_number': None,
                'solver_lookup_failed': False,
            },
        ),
        patch(
            'gittensor.validator.issue_competitions.forward.IssueCompetitionContractClient',
            return_value=contract_client,
        ),
    ):
        _run(issue_competitions(cast(Any, validator), {}))

    contract_client.vote_cancel_issue.assert_called_once_with(
        issue_id=7,
        reason=expected_reason,
        wallet=validator.wallet,
    )
    contract_client.vote_solution.assert_not_called()


def test_non_completed_closure_does_not_vote_solution_even_with_eligible_solver_id():
    """Defense-in-depth: even if check_github_issue_closed leaked a solver id alongside
    a non-completed state_reason (which it should not), the forward layer's no-solver
    branch fires first because gating happens at the lower layer. This test pins the
    contract that the forward layer trusts solver_github_id=None when the gate fired.
    """
    validator = _make_validator()
    contract_client = MagicMock()
    contract_client.harvest_emissions.return_value = None
    contract_client.get_issues_by_status.return_value = [_make_issue()]
    contract_client.vote_cancel_issue.return_value = True

    miner_evaluations = {
        99: SimpleNamespace(github_id='12345', hotkey='5HotkeyForMiner99', is_eligible=True),
    }

    with (
        patch('gittensor.validator.issue_competitions.forward.GITTENSOR_VALIDATOR_PAT', 'ghp_validator'),
        patch('gittensor.validator.issue_competitions.forward.get_contract_address', return_value='5Contract'),
        patch(
            'gittensor.validator.issue_competitions.forward.check_github_issue_closed',
            return_value={
                'is_closed': True,
                'state_reason': 'not_planned',
                'solver_github_id': None,
                'pr_number': None,
                'solver_lookup_failed': False,
            },
        ),
        patch(
            'gittensor.validator.issue_competitions.forward.IssueCompetitionContractClient',
            return_value=contract_client,
        ),
    ):
        _run(issue_competitions(cast(Any, validator), cast(Any, miner_evaluations)))

    contract_client.vote_solution.assert_not_called()
    contract_client.vote_cancel_issue.assert_called_once()
    cancel_kwargs = contract_client.vote_cancel_issue.call_args.kwargs
    assert cancel_kwargs['reason'] == 'Issue closed as not_planned on GitHub'
