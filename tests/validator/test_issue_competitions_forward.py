#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for issue bounty forward voting policy around solver lookup failures."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

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
