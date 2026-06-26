# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for `gitt config set` key whitelist.

Validates that only recognised CONFIG_KEYS are accepted by `gitt config set`,
preventing typos like `wallet_name` from silently writing a dead entry that
downstream commands (which read by canonical key name) will ignore.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor.cli.main import CONFIG_KEYS, config_group


@pytest.fixture
def temp_config_dir(tmp_path: Path):
    """Redirect CONFIG_FILE/GITTENSOR_DIR to a temp dir for the duration of one test."""
    config_dir = tmp_path / '.gittensor'
    config_file = config_dir / 'config.json'
    with (
        patch('gittensor.cli.main.GITTENSOR_DIR', config_dir),
        patch('gittensor.cli.main.CONFIG_FILE', config_file),
    ):
        yield config_dir, config_file


def _read(config_file: Path) -> dict:
    return json.loads(config_file.read_text()) if config_file.exists() else {}


class TestConfigSetWhitelist:
    """Reject typo'd keys so they can't silently shadow the canonical names."""

    def test_recognised_key_writes_value(self, temp_config_dir):
        _, config_file = temp_config_dir
        runner = CliRunner()
        result = runner.invoke(config_group, ['set', 'wallet', 'alice'])

        assert result.exit_code == 0, result.output
        assert _read(config_file) == {'wallet': 'alice'}

    def test_unknown_key_is_rejected(self, temp_config_dir):
        """`wallet_name` is the canonical example: it looks plausible but is wrong."""
        _, config_file = temp_config_dir
        runner = CliRunner()
        result = runner.invoke(config_group, ['set', 'wallet_name', 'alice'])

        assert result.exit_code != 0
        assert "'wallet_name' is not one of" in result.output or 'wallet_name' in result.output
        # No file should have been written for a rejected key.
        assert not config_file.exists()

    def test_unknown_key_does_not_clobber_existing_config(self, temp_config_dir):
        config_dir, config_file = temp_config_dir
        config_dir.mkdir(parents=True)
        config_file.write_text(json.dumps({'wallet': 'alice'}, indent=2))

        runner = CliRunner()
        result = runner.invoke(config_group, ['set', 'wallet_name', 'bob'])

        assert result.exit_code != 0
        # Existing valid config preserved untouched.
        assert _read(config_file) == {'wallet': 'alice'}

    @pytest.mark.parametrize('key', list(CONFIG_KEYS))
    def test_every_recognised_key_round_trips(self, temp_config_dir, key):
        _, config_file = temp_config_dir
        runner = CliRunner()
        result = runner.invoke(config_group, ['set', key, 'value-for-' + key])

        assert result.exit_code == 0, result.output
        assert _read(config_file)[key] == 'value-for-' + key

    def test_uppercase_key_normalised_to_lowercase(self, temp_config_dir):
        """`click.Choice(case_sensitive=False)` matches mixed case; we persist the canonical lowercase form."""
        _, config_file = temp_config_dir
        runner = CliRunner()
        result = runner.invoke(config_group, ['set', 'WALLET', 'alice'])

        assert result.exit_code == 0, result.output
        # Stored under lowercase key so downstream `config.get('wallet')` finds it.
        assert _read(config_file) == {'wallet': 'alice'}

    def test_update_message_shown_when_overwriting(self, temp_config_dir):
        config_dir, config_file = temp_config_dir
        config_dir.mkdir(parents=True)
        config_file.write_text(json.dumps({'wallet': 'alice'}, indent=2))

        runner = CliRunner()
        result = runner.invoke(config_group, ['set', 'wallet', 'bob'])

        assert result.exit_code == 0, result.output
        assert 'alice' in result.output and 'bob' in result.output
        assert _read(config_file) == {'wallet': 'bob'}
