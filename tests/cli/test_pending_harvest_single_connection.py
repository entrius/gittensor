# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for `issues pending-harvest` contract reads."""

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


def test_pending_harvest_json_treasury_read_failure_returns_read_failed(cli_root, runner):
    fake_subtensor = MagicMock()
    fake_client = MagicMock()
    fake_client.get_treasury_stake.side_effect = ConnectionResetError('alpha storage reset')

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
        patch('gittensor.cli.issue_commands.view._read_issues_from_child_storage') as read_issues,
    ):
        result = runner.invoke(cli_root, ['issues', 'pending-harvest', '--json'], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert 'alpha storage reset' in payload['error']['message']
    read_issues.assert_not_called()


def test_pending_harvest_json_empty_treasury_still_succeeds(cli_root, runner):
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
        patch(
            'gittensor.cli.issue_commands.view._read_issues_from_child_storage',
            return_value=[{'bounty_amount': 1_000_000_000}],
        ),
    ):
        result = runner.invoke(cli_root, ['issues', 'pending-harvest', '--json'], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload['success'] is True
    assert payload['treasury_stake_raw'] == 0
    assert payload['allocated_bounties_raw'] == 1_000_000_000
    assert payload['pending_harvest_raw'] == 0


def test_pending_harvest_json_packed_storage_decode_failure_returns_read_failed(cli_root, runner):
    """When issue storage decode fails, pending-harvest must not overstate harvestable alpha."""
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
        patch(
            'gittensor.cli.issue_commands.view._read_issues_from_child_storage',
            side_effect=RuntimeError('Failed to decode packed contract storage (73 bytes returned from chain)'),
        ),
    ):
        result = runner.invoke(cli_root, ['issues', 'pending-harvest', '--json'], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert 'Failed to decode packed contract storage' in payload['error']['message']
    assert 'pending_harvest_raw' not in payload


def test_bounty_pool_json_packed_storage_decode_failure_returns_read_failed(cli_root, runner):
    """When packed storage decode fails, bounty-pool --json must not report a zero pool."""
    fake_substrate = object()
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('async_substrate_interface.SubstrateInterface', return_value=fake_substrate),
        patch(
            'gittensor.cli.issue_commands.helpers.get_contract_child_storage_key',
            return_value='0xchild',
        ),
        patch(
            'gittensor.cli.issue_commands.helpers.read_contract_packed_storage_bytes',
            return_value=b'\x00' * 73,
        ),
    ):
        result = runner.invoke(cli_root, ['issues', 'bounty-pool', '--json'], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert 'Failed to decode packed contract storage' in payload['error']['message']
    assert 'total_bounty_pool_raw' not in payload
