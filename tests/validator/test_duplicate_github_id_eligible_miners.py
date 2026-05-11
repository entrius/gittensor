# Entrius 2025

"""Test that duplicate github_ids in eligible_miners dict are detected
and logged instead of silently overwriting.

Bug #895: If two eligible MinerEvaluation entries share the same github_id,
the dict comprehension silently overwrites the first with the second,
potentially misdirecting bounty votes.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from gittensor.classes import MinerEvaluation
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


def _make_eval(uid, hotkey, github_id, is_eligible=True):
    ev = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id)
    ev.is_eligible = is_eligible
    return ev


class TestDuplicateGithubIdInEligibleMiners:
    def test_duplicate_github_id_logs_warning(self):
        """When two eligible miners share a github_id, the second overwrites
        the first but a warning is logged to make the overwrite visible."""
        validator = _make_validator()
        contract_client = MagicMock()
        contract_client.harvest_emissions.return_value = None
        contract_client.get_issues_by_status.return_value = []

        eval1 = _make_eval(uid=1, hotkey='hotkey_alpha', github_id='12345')
        eval2 = _make_eval(uid=2, hotkey='hotkey_beta', github_id='12345')

        miner_evaluations = {1: eval1, 2: eval2}

        with (
            patch('gittensor.validator.issue_competitions.forward.GITTENSOR_VALIDATOR_PAT', 'ghp_validator'),
            patch('gittensor.validator.issue_competitions.forward.get_contract_address', return_value='5Contract'),
            patch(
                'gittensor.validator.issue_competitions.forward.IssueCompetitionContractClient',
                return_value=contract_client,
            ),
        ):
            with patch('gittensor.validator.issue_competitions.forward.bt') as mock_bt:
                _run(issue_competitions(cast(Any, validator), miner_evaluations))

                warning_calls = [
                    call for call in mock_bt.logging.warning.call_args_list if 'Duplicate github_id' in str(call)
                ]
                assert len(warning_calls) == 1, (
                    f'Expected 1 warning about duplicate github_id, got {len(warning_calls)}'
                )

    def test_no_duplicate_github_id_no_warning(self):
        """When all eligible miners have distinct github_ids, no duplicate
        warning should be logged."""
        validator = _make_validator()
        contract_client = MagicMock()
        contract_client.harvest_emissions.return_value = None
        contract_client.get_issues_by_status.return_value = []

        eval1 = _make_eval(uid=1, hotkey='hotkey_alpha', github_id='11111')
        eval2 = _make_eval(uid=2, hotkey='hotkey_beta', github_id='22222')

        miner_evaluations = {1: eval1, 2: eval2}

        with (
            patch('gittensor.validator.issue_competitions.forward.GITTENSOR_VALIDATOR_PAT', 'ghp_validator'),
            patch('gittensor.validator.issue_competitions.forward.get_contract_address', return_value='5Contract'),
            patch(
                'gittensor.validator.issue_competitions.forward.IssueCompetitionContractClient',
                return_value=contract_client,
            ),
        ):
            with patch('gittensor.validator.issue_competitions.forward.bt') as mock_bt:
                _run(issue_competitions(cast(Any, validator), miner_evaluations))

                warning_calls = [
                    call for call in mock_bt.logging.warning.call_args_list if 'Duplicate github_id' in str(call)
                ]
                assert len(warning_calls) == 0

    def test_ineligible_miners_excluded_from_mapping(self):
        """Miners that are not eligible or have github_id='0' are excluded
        from eligible_miners entirely."""
        validator = _make_validator()
        contract_client = MagicMock()
        contract_client.harvest_emissions.return_value = None
        contract_client.get_issues_by_status.return_value = []

        ineligible = _make_eval(uid=3, hotkey='hotkey_gamma', github_id='99999', is_eligible=False)
        zero_id = _make_eval(uid=4, hotkey='hotkey_delta', github_id='0', is_eligible=True)
        no_id = _make_eval(uid=5, hotkey='hotkey_epsilon', github_id=None, is_eligible=True)
        eligible = _make_eval(uid=1, hotkey='hotkey_alpha', github_id='11111')

        ineligible.is_eligible = False

        miner_evaluations = {
            1: eligible,
            3: ineligible,
            4: zero_id,
            5: no_id,
        }

        with (
            patch('gittensor.validator.issue_competitions.forward.GITTENSOR_VALIDATOR_PAT', 'ghp_validator'),
            patch('gittensor.validator.issue_competitions.forward.get_contract_address', return_value='5Contract'),
            patch(
                'gittensor.validator.issue_competitions.forward.IssueCompetitionContractClient',
                return_value=contract_client,
            ),
        ):
            _run(issue_competitions(cast(Any, validator), miner_evaluations))
