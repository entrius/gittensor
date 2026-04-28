# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for `gitt config set`.

Covers two related guarantees:

1. **Key whitelist (PR #813):** only recognised CONFIG_KEYS are accepted, so
   typos like `wallet_name` cannot silently shadow the canonical names.
2. **Corrupt-file refusal (PR #817 / issue #845):** a JSONDecodeError on the
   existing config file aborts non-zero instead of clobbering the file with a
   fresh single-key config. Operator's `network`, `contract_address`,
   `ws_endpoint`, and `hotkey` would otherwise silently disappear and the next
   `gitt issues ...` would fall through to finney mainnet.
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


class TestConfigSetCorruption:
    """`gitt config set` must not destroy other keys when the file is corrupt."""

    def test_corrupt_config_aborts_with_nonzero_exit(self, temp_config_dir):
        config_dir, config_file = temp_config_dir
        config_dir.mkdir(parents=True)
        # Truncated JSON — simulates an interrupted write or manual edit.
        config_file.write_text('{"network": "test"')

        runner = CliRunner()
        result = runner.invoke(config_group, ['set', 'hotkey', 'default'])

        assert result.exit_code != 0
        assert 'not valid JSON' in result.output
        assert 'Refusing to overwrite' in result.output

    def test_corrupt_config_preserves_existing_file(self, temp_config_dir):
        """The whole point: the bad file is left alone, not clobbered."""
        config_dir, config_file = temp_config_dir
        config_dir.mkdir(parents=True)
        original_bytes = b'{"network": "test"'
        config_file.write_bytes(original_bytes)

        runner = CliRunner()
        runner.invoke(config_group, ['set', 'hotkey', 'default'])

        # Byte-for-byte identical: the operator can recover the values they
        # had configured before the corruption.
        assert config_file.read_bytes() == original_bytes

    def test_valid_config_still_round_trips(self, temp_config_dir):
        """Non-corrupt files must still merge new keys without loss."""
        config_dir, config_file = temp_config_dir
        config_dir.mkdir(parents=True)
        config_file.write_text(json.dumps({'network': 'test', 'wallet': 'alice'}))

        runner = CliRunner()
        result = runner.invoke(config_group, ['set', 'hotkey', 'default'])

        assert result.exit_code == 0, result.output
        assert _read(config_file) == {'network': 'test', 'wallet': 'alice', 'hotkey': 'default'}

    def test_missing_config_creates_fresh(self, temp_config_dir):
        """First-run case: no existing file is fine — write a fresh one."""
        _, config_file = temp_config_dir
        assert not config_file.exists()

        runner = CliRunner()
        result = runner.invoke(config_group, ['set', 'wallet', 'alice'])

        assert result.exit_code == 0, result.output
        assert _read(config_file) == {'wallet': 'alice'}
