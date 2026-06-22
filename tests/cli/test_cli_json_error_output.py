# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests: --json mode must emit JSON even on the exception path.

Covers `issues bounty-pool`, `issues pending-harvest`, `admin info`, and `vote list`.
"""

import json
from unittest.mock import patch

import pytest

FORCED_MESSAGE = 'forced test failure for json-error assertion'


@pytest.mark.parametrize(
    'argv',
    [
        ['issues', 'bounty-pool', '--json'],
        ['issues', 'pending-harvest', '--json'],
        ['admin', 'info', '--json'],
        ['vote', 'list', '--json'],
    ],
)
def test_cli_commands_emit_json_on_exception(cli_root, runner, argv):
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch(
            'gittensor.cli.issue_commands.vote._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch(
            'async_substrate_interface.SubstrateInterface',
            side_effect=RuntimeError(FORCED_MESSAGE),
        ),
        patch(
            'bittensor.Subtensor',
            side_effect=RuntimeError(FORCED_MESSAGE),
        ),
    ):
        result = runner.invoke(cli_root, argv, catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert FORCED_MESSAGE in payload['error']['message']


@pytest.mark.parametrize(
    'argv,message_fragment',
    [
        (['issues', 'bounty-pool', '--json'], 'Error reading bounty pool from contract'),
        (['admin', 'info', '--json'], 'Error reading contract configuration'),
    ],
)
def test_read_only_commands_tag_contract_read_failures_as_read_failed(cli_root, runner, argv, message_fragment):
    """Contract read failures from `bounty-pool` and `admin info` must surface as
    `error.type == "read_failed"` so JSON consumers can distinguish a contract
    read failure from a generic CLI error, matching `pending-harvest` and `vote list`.
    """
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch(
            'async_substrate_interface.SubstrateInterface',
            side_effect=RuntimeError(FORCED_MESSAGE),
        ),
    ):
        result = runner.invoke(cli_root, argv, catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'
    assert message_fragment in payload['error']['message']
    assert FORCED_MESSAGE in payload['error']['message']


def test_admin_info_emits_json_on_soft_read_failure(cli_root, runner):
    """`admin info --json` must emit structured JSON when packed storage read returns None."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('async_substrate_interface.SubstrateInterface', return_value=object()),
        patch('gittensor.cli.issue_commands.view._read_contract_packed_storage', return_value=None),
    ):
        result = runner.invoke(cli_root, ['admin', 'info', '--json'], catch_exceptions=False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'read_failed'


def test_admin_info_human_mode_exits_non_zero_on_soft_read_failure(cli_root, runner):
    """`admin info` (human mode) must exit non-zero when packed storage read returns None."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('async_substrate_interface.SubstrateInterface', return_value=object()),
        patch('gittensor.cli.issue_commands.view._read_contract_packed_storage', return_value=None),
    ):
        result = runner.invoke(cli_root, ['admin', 'info'], catch_exceptions=False)

    assert result.exit_code == 1
    assert 'Could not read contract configuration' in result.output


@pytest.mark.parametrize(
    'argv,error_type,message_fragment',
    [
        # Type validation by Click on a primitive --id (issue list)
        (['issues', 'list', '--json', '--id', 'not-an-int'], 'bad_parameter', "Invalid value for '--id'"),
        # Unknown option under any command
        (['issues', 'list', '--json', '--no-such-flag'], 'usage_error', 'No such option'),
        # Missing required option (issues submissions requires --id)
        (['issues', 'submissions', '--json'], 'bad_parameter', "Missing option '--id'"),
        # Miner command Click validation under --json
        (['miner', 'post', '--json', '--netuid', 'not-an-int'], 'bad_parameter', "'--netuid'"),
    ],
)
def test_click_parse_errors_emit_canonical_json(cli_root, runner, argv, error_type, message_fragment):
    """Click's own arg-parsing errors must surface as the canonical JSON envelope
    when --json appears in argv."""
    result = runner.invoke(cli_root, argv, catch_exceptions=False)
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload['success'] is False
    assert payload['error']['type'] == error_type
    assert message_fragment in payload['error']['message']


def test_click_parse_errors_stay_human_without_json_flag(cli_root, runner):
    """Without --json, Click parse errors keep their plain-text rendering"""
    result = runner.invoke(cli_root, ['issues', 'list', '--id', 'not-an-int'], catch_exceptions=False)
    assert result.exit_code == 2
    # Plain text, not JSON
    try:
        json.loads(result.output)
        raise AssertionError('output should not be JSON without --json flag')
    except json.JSONDecodeError:
        pass
    assert 'Invalid value' in result.output
