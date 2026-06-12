# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for `issues list --json --id` not-found handling."""

import json
from unittest.mock import patch

import pytest

FAKE_ISSUES = [
    {
        'id': 1,
        'repository_full_name': 'owner/repo',
        'issue_number': 10,
        'bounty_amount': 50_000_000_000,
        'target_bounty': 100_000_000_000,
        'status': 'Active',
    },
    {
        'id': 2,
        'repository_full_name': 'owner/repo',
        'issue_number': 11,
        'bounty_amount': 0,
        'target_bounty': 100_000_000_000,
        'status': 'Completed',
    },
]


def test_issues_list_json_missing_issue_returns_structured_error(cli_root, runner):
    """Requesting a nonexistent issue ID must return a structured JSON error with non-zero exit."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json', '--id', '999'], catch_exceptions=False)

    assert result.exit_code != 0

    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'not_found'
    assert '999' in payload['error']['message']


def test_issues_list_human_missing_issue_exits_non_zero(cli_root, runner):
    """Human mode must exit non-zero for missing --id, matching JSON semantics."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--id', '999'], catch_exceptions=False)

    assert result.exit_code != 0
    assert '999' in result.output
    assert 'not found' in result.output.lower()


@pytest.mark.parametrize('bad_id', ['0', '-1', '1000000', '99999999999999'])
def test_issues_list_rejects_invalid_id_human(cli_root, runner, bad_id):
    """Out-of-range --id must be rejected at parse time without any contract read."""
    with patch('gittensor.cli.issue_commands.view.read_issues_from_contract') as mock_read:
        result = runner.invoke(cli_root, ['issues', 'list', '--id', bad_id], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'not in the range' in result.output
    mock_read.assert_not_called()


def test_issues_list_json_status_filter_is_case_insensitive(cli_root, runner):
    """`--status` must filter JSON output by lifecycle state regardless of input casing."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=FAKE_ISSUES),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json', '--status', 'ACTIVE'], catch_exceptions=False)

    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload['success'] is True
    assert payload['issue_count'] == 1
    assert [i['id'] for i in payload['issues']] == [1]


def test_issues_list_rejects_invalid_status(cli_root, runner):
    """An unknown --status value must be rejected at parse time without any contract read."""
    with patch('gittensor.cli.issue_commands.view.read_issues_from_contract') as mock_read:
        result = runner.invoke(cli_root, ['issues', 'list', '--status', 'bogus'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'bogus' in result.output
    mock_read.assert_not_called()


def test_issues_list_json_contract_read_failure_returns_structured_error(cli_root, runner):
    """A contract read failure must surface as `success: false` JSON with non-zero exit,
    not as a `success: true, issue_count: 0` payload that looks like a clean empty contract."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch(
            'gittensor.cli.issue_commands.view.read_issues_from_contract',
            side_effect=ConnectionRefusedError('[Errno 111] Connection refused'),
        ),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code != 0

    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert 'Connection refused' in payload['error']['message']
    assert 'issue_count' not in payload


def test_issues_list_human_contract_read_failure_exits_non_zero(cli_root, runner):
    """Human mode must exit non-zero and not print `No issues found.` when the contract read fails."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch(
            'gittensor.cli.issue_commands.view.read_issues_from_contract',
            side_effect=ConnectionRefusedError('[Errno 111] Connection refused'),
        ),
    ):
        result = runner.invoke(cli_root, ['issues', 'list'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'No issues found' not in result.output
    assert 'Connection refused' in result.output


def test_issues_list_json_empty_contract_still_succeeds(cli_root, runner):
    """A reachable contract with zero issues must keep emitting `success: true, issue_count: 0`."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract', return_value=[]),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload['success'] is True
    assert payload['issue_count'] == 0
    assert payload['issues'] == []


def test_issues_list_json_deeper_storage_read_failure_returns_structured_error(cli_root, runner):
    """A failure inside `_read_issues_from_child_storage` (e.g. the contract-info RPC raising)
    must surface as `success: false`, not be silently coerced to an empty issue list."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('async_substrate_interface.SubstrateInterface', return_value=object()),
        patch(
            'gittensor.cli.issue_commands.helpers.get_contract_child_storage_key',
            side_effect=RuntimeError('contract-info query failed'),
        ),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code != 0

    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert 'contract-info query failed' in payload['error']['message']
    assert 'issue_count' not in payload


def test_issues_list_json_missing_dependency_emits_install_hint(cli_root, runner):
    """A missing-dependency ImportError must surface a structured error with the install hint,
    distinct from a generic read failure."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch(
            'gittensor.cli.issue_commands.view.read_issues_from_contract',
            side_effect=ImportError("No module named 'substrateinterface'"),
        ),
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json'], catch_exceptions=False)

    assert result.exit_code != 0

    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'missing_dependency'
    assert 'uv sync' in payload['error']['message']
    assert 'substrateinterface' in payload['error']['message']
