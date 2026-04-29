# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for `issues list --json --id` not-found handling."""

import json
from unittest.mock import patch

FAKE_ISSUES = [
    {
        'id': 1,
        'repository_full_name': 'owner/repo',
        'issue_number': 10,
        'bounty_amount': 50_000_000_000,
        'target_bounty': 100_000_000_000,
        'status': 'Active',
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

    payload = json.loads(result.output)
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


# Regression tests for #854: --id input validation must match the rest of the
# CLI (mutations.py / vote.py / submissions.py — all of which call
# validate_issue_id) and reject 0, negatives, and out-of-u32 values *before*
# any contract read.
def test_issues_list_id_zero_rejected_at_parse(cli_root, runner):
    """--id 0 must fail with the standard validate_issue_id message and not touch the chain."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
        ) as resolve,
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract') as read_chain,
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--id', '0'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'must be between 1 and 999999' in result.output
    assert '(got 0)' in result.output
    # Validation must happen before any contract / network resolution.
    resolve.assert_not_called()
    read_chain.assert_not_called()


def test_issues_list_id_negative_rejected_at_parse(cli_root, runner):
    """--id -1 must fail at validation, not be passed through to on-chain lookup."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
        ) as resolve,
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract') as read_chain,
    ):
        # click parses '--id -1' as the option `-1`, so use --id=-1 syntax
        # (the form the user has to use for negative ints).
        result = runner.invoke(cli_root, ['issues', 'list', '--id=-1'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'must be between 1 and 999999' in result.output
    assert '(got -1)' in result.output
    resolve.assert_not_called()
    read_chain.assert_not_called()


def test_issues_list_id_out_of_range_rejected_at_parse(cli_root, runner):
    """An ID well above the u32-friendly cap must be rejected at parse time."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
        ) as resolve,
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract') as read_chain,
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--id', '99999999999999'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'must be between 1 and 999999' in result.output
    resolve.assert_not_called()
    read_chain.assert_not_called()


def test_issues_list_id_zero_json_returns_bad_parameter(cli_root, runner):
    """JSON callers must get a structured `bad_parameter` error, not a network attempt."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
        ) as resolve,
        patch('gittensor.cli.issue_commands.view.read_issues_from_contract') as read_chain,
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json', '--id', '0'], catch_exceptions=False)

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert payload['error']['type'] == 'bad_parameter'
    assert 'must be between 1 and 999999' in payload['error']['message']
    resolve.assert_not_called()
    read_chain.assert_not_called()


def test_issues_list_valid_id_still_reaches_chain(cli_root, runner):
    """Sanity: a valid --id still resolves the contract and reads the chain (no regression on #335)."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ) as resolve,
        patch(
            'gittensor.cli.issue_commands.view.read_issues_from_contract',
            return_value=FAKE_ISSUES,
        ) as read_chain,
    ):
        result = runner.invoke(cli_root, ['issues', 'list', '--json', '--id', '1'], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['success'] is True
    assert payload['issue']['id'] == 1
    resolve.assert_called_once()
    read_chain.assert_called_once()
