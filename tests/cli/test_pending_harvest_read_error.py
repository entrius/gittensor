# The MIT License (MIT)
# Copyright © 2025 Entrius

"""pending-harvest --json must emit success:false / error.type=read_failed when
the treasury stake read fails, not success:true with treasury_stake_raw:0."""

import json
from unittest.mock import MagicMock, patch

from gittensor.validator.issue_competitions.errors import TreasuryReadError


def test_pending_harvest_json_reports_failure_on_treasury_read_error(cli_root, runner):
    """TreasuryReadError → success:false, error.type=read_failed, exit non-zero."""
    fake_subtensor = MagicMock()
    fake_client = MagicMock()
    fake_client.get_treasury_stake.side_effect = TreasuryReadError(
        'Treasury stake read failed: ConnectionResetError'
    )

    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('bittensor.Subtensor', return_value=fake_subtensor),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
        patch('gittensor.cli.issue_commands.view._read_issues_from_child_storage', return_value=[]),
    ):
        result = runner.invoke(cli_root, ['issues', 'pending-harvest', '--json'])

    assert result.exit_code != 0, 'exit code must be non-zero on read failure'
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'


def test_pending_harvest_json_succeeds_on_genuine_zero_stake(cli_root, runner):
    """get_treasury_stake() returning 0 normally → success:true, pending_harvest_raw:0."""
    fake_subtensor = MagicMock()
    fake_client = MagicMock()
    fake_client.get_treasury_stake.return_value = 0

    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('bittensor.Subtensor', return_value=fake_subtensor),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient',
            return_value=fake_client,
        ),
        patch('gittensor.cli.issue_commands.view._read_issues_from_child_storage', return_value=[]),
    ):
        result = runner.invoke(cli_root, ['issues', 'pending-harvest', '--json'], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload['success'] is True
    assert payload['treasury_stake_raw'] == 0
    assert payload['pending_harvest_raw'] == 0
