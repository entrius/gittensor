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
            'substrateinterface.SubstrateInterface',
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


def test_admin_info_emits_json_on_soft_read_failure(cli_root, runner):
    """`admin info --json` must emit structured JSON when packed storage read returns None."""
    with (
        patch(
            'gittensor.cli.issue_commands.view._resolve_contract_and_network',
            return_value=('5Fakeaddr', 'ws://x', 'test'),
        ),
        patch('substrateinterface.SubstrateInterface', return_value=object()),
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
        patch('substrateinterface.SubstrateInterface', return_value=object()),
        patch('gittensor.cli.issue_commands.view._read_contract_packed_storage', return_value=None),
    ):
        result = runner.invoke(cli_root, ['admin', 'info'], catch_exceptions=False)

    assert result.exit_code == 1
    assert 'Could not read contract configuration' in result.output


def test_issues_list_json_mode_bad_id_type_emits_bad_parameter(cli_root, runner):
    """Invalid --id value must yield JSON on stdout when --json is set (Click parse path)."""
    result = runner.invoke(cli_root, ['issues', 'list', '--json', '--id', 'not-an-int'], catch_exceptions=False)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'bad_parameter'
    msg = payload['error']['message'].lower()
    assert 'not-an-int' in msg or 'integer' in msg or 'valid' in msg


def test_issues_list_json_mode_unknown_option_emits_usage_error(cli_root, runner):
    result = runner.invoke(cli_root, ['issues', 'list', '--json', '--not-a-real-option'], catch_exceptions=False)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'usage_error'
    assert 'not-a-real-option' in payload['error']['message'] or 'no such option' in payload['error']['message'].lower()


def test_miner_check_json_mode_unknown_option_emits_usage_error(cli_root, runner):
    result = runner.invoke(cli_root, ['miner', 'check', '--json', '--not-a-real-option'], catch_exceptions=False)
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload['success'] is False
    assert payload['error']['type'] == 'usage_error'
