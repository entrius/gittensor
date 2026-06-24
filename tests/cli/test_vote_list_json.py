# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for `vote list --json` validator whitelist reads."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _subtensor_with_contract():
    substrate = MagicMock()
    substrate.query.return_value = {'contract': True}
    return SimpleNamespace(substrate=substrate)


def test_vote_list_json_raw_read_none_returns_read_failed(cli_root, runner):
    """A failed raw validator read must not look like a successful empty whitelist."""
    with (
        patch(
            'gittensor.cli.issue_commands.vote._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('bittensor.Subtensor', return_value=_subtensor_with_contract()),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient._raw_contract_read',
            return_value=None,
        ),
    ):
        result = runner.invoke(cli_root, ['vote', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code != 0

    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert 'validator whitelist' in payload['error']['message']
    assert 'validators' not in payload


def test_vote_list_json_raw_read_exception_returns_read_failed(cli_root, runner):
    """RPC exceptions from the validator whitelist read must surface as read failures."""
    with (
        patch(
            'gittensor.cli.issue_commands.vote._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('bittensor.Subtensor', return_value=_subtensor_with_contract()),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient._raw_contract_read',
            side_effect=ConnectionRefusedError('[Errno 111] Connection refused'),
        ),
    ):
        result = runner.invoke(cli_root, ['vote', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code != 0

    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert 'Connection refused' in payload['error']['message']
    assert 'validators' not in payload


def test_vote_list_json_malformed_validator_payload_returns_read_failed(cli_root, runner):
    """Malformed validator Vec payloads must not be reported as a valid empty whitelist."""
    with (
        patch(
            'gittensor.cli.issue_commands.vote._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('bittensor.Subtensor', return_value=_subtensor_with_contract()),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient._raw_contract_read',
            return_value=b'\x04',
        ),
    ):
        result = runner.invoke(cli_root, ['vote', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code != 0

    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert 'length mismatch' in payload['error']['message']
    assert 'validators' not in payload


def test_vote_list_json_empty_validator_vec_still_succeeds(cli_root, runner):
    """A reachable contract with an encoded empty Vec<AccountId> remains a successful empty whitelist."""
    with (
        patch(
            'gittensor.cli.issue_commands.vote._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('bittensor.Subtensor', return_value=_subtensor_with_contract()),
        patch(
            'gittensor.validator.issue_competitions.contract_client.IssueCompetitionContractClient._raw_contract_read',
            return_value=b'\x00',
        ),
    ):
        result = runner.invoke(cli_root, ['vote', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload == {
        'success': True,
        'validators': [],
        'count': 0,
        'consensus_threshold': 0,
    }
