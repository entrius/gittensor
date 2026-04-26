# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for `gitt config unset <key>` CLI command."""

import json
from unittest.mock import patch


def test_config_unset_removes_key_and_persists(cli_root, runner, tmp_path):
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps({'wallet': 'alice', 'network': 'finney'}))

    with patch('gittensor.cli.main.CONFIG_FILE', config_file):
        result = runner.invoke(cli_root, ['config', 'unset', 'wallet'], catch_exceptions=False)

    assert result.exit_code == 0
    assert 'wallet' in result.output

    # File now contains only the unrelated key.
    assert json.loads(config_file.read_text()) == {'network': 'finney'}


def test_config_unset_exits_non_zero_when_key_missing(cli_root, runner, tmp_path):
    config_file = tmp_path / 'config.json'
    original = {'wallet': 'alice'}
    config_file.write_text(json.dumps(original))

    with patch('gittensor.cli.main.CONFIG_FILE', config_file):
        result = runner.invoke(cli_root, ['config', 'unset', 'unset_key'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'unset_key' in result.output
    # File is left untouched on a no-op.
    assert json.loads(config_file.read_text()) == original


def test_config_unset_exits_non_zero_when_config_file_missing(cli_root, runner, tmp_path):
    config_file = tmp_path / 'nope.json'

    with patch('gittensor.cli.main.CONFIG_FILE', config_file):
        result = runner.invoke(cli_root, ['config', 'unset', 'wallet'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'config' in result.output.lower()
    assert not config_file.exists()


def test_config_unset_exits_non_zero_when_config_file_invalid_json(cli_root, runner, tmp_path):
    config_file = tmp_path / 'config.json'
    invalid = '{not valid json'
    config_file.write_text(invalid)

    with patch('gittensor.cli.main.CONFIG_FILE', config_file):
        result = runner.invoke(cli_root, ['config', 'unset', 'wallet'], catch_exceptions=False)

    assert result.exit_code != 0
    assert 'invalid json' in result.output.lower()
    # Don't clobber the file when we can't parse it.
    assert config_file.read_text() == invalid
