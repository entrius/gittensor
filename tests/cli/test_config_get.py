# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for `gitt config get <key>` CLI command."""

import json
from unittest.mock import patch


def test_config_get_prints_value_when_key_exists(cli_root, runner, tmp_path):
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps({'wallet': 'alice', 'network': 'finney'}))

    with patch('gittensor.cli.main.CONFIG_FILE', config_file):
        result = runner.invoke(cli_root, ['config', 'get', 'wallet'], catch_exceptions=False)

    assert result.exit_code == 0
    # Output is the bare value plus a newline; no Rich styling that would
    # break shell capture (`X=$(gitt config get wallet)`).
    assert result.output.rstrip('\n') == 'alice'


def test_config_get_exits_non_zero_when_key_missing(cli_root, runner, tmp_path):
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps({'wallet': 'alice'}))

    with patch('gittensor.cli.main.CONFIG_FILE', config_file):
        result = runner.invoke(cli_root, ['config', 'get', 'unset_key'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'unset_key' in result.output


def test_config_get_exits_non_zero_when_config_file_missing(cli_root, runner, tmp_path):
    config_file = tmp_path / 'nope.json'

    with patch('gittensor.cli.main.CONFIG_FILE', config_file):
        result = runner.invoke(cli_root, ['config', 'get', 'wallet'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'config' in result.output.lower()


def test_config_get_exits_non_zero_when_config_file_invalid_json(cli_root, runner, tmp_path):
    config_file = tmp_path / 'config.json'
    config_file.write_text('{not valid json')

    with patch('gittensor.cli.main.CONFIG_FILE', config_file):
        result = runner.invoke(cli_root, ['config', 'get', 'wallet'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'invalid json' in result.output.lower()
