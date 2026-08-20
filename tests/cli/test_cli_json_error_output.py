# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests: --json mode must emit the canonical JSON envelope on Click parse errors."""

import json

import pytest


@pytest.mark.parametrize(
    'argv, error_type, message_fragment',
    [
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
    result = runner.invoke(cli_root, ['miner', 'post', '--netuid', 'not-an-int'], catch_exceptions=False)
    assert result.exit_code == 2
    # Plain text, not JSON
    try:
        json.loads(result.output)
        raise AssertionError('output should not be JSON without --json flag')
    except json.JSONDecodeError:
        pass
    assert 'Invalid value' in result.output
