# The MIT License (MIT)
# Copyright © 2025 Entrius

"""pending-harvest must reuse the subtensor connection, not open a second one."""

import json
from unittest.mock import MagicMock, patch


def test_pending_harvest_reuses_subtensor_substrate(cli_root, runner):
    fake_subtensor = MagicMock()
    fake_client = MagicMock()
    fake_client.get_treasury_stake.return_value = 5_000_000_000

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
        patch('gittensor.cli.issue_commands.view._read_issues_from_child_storage', return_value=[]) as read_issues,
        patch('async_substrate_interface.SubstrateInterface') as substrate_iface,
    ):
        result = runner.invoke(cli_root, ['issues', 'pending-harvest', '--json'], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload['success'] is True
    # The fix: no second connection is opened, and the issue read reuses subtensor.substrate.
    substrate_iface.assert_not_called()
    read_issues.assert_called_once()
    assert read_issues.call_args.args[0] is fake_subtensor.substrate
