# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for #845: gitt config set must refuse to overwrite a corrupt config.json.

Same family as #781 (pat_storage refusal) and #817 (load_config refusal on read).
The write path was the missing piece — without this fix, a single corrupt-config
event silently destroys every previously configured key.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def temp_config(tmp_path: Path):
    """Redirect CONFIG_FILE / GITTENSOR_DIR to a temp directory for one test."""
    config_dir = tmp_path / '.gittensor'
    config_file = config_dir / 'config.json'
    with (
        patch('gittensor.cli.main.GITTENSOR_DIR', config_dir),
        patch('gittensor.cli.main.CONFIG_FILE', config_file),
    ):
        yield config_dir, config_file


def test_config_set_aborts_on_corrupt_existing_json(runner, temp_config):
    from gittensor.cli.main import config_group

    _config_dir, config_file = temp_config
    config_file.parent.mkdir(parents=True, exist_ok=True)
    corrupt_payload = '{"network": "test"'  # truncated, not valid JSON
    config_file.write_text(corrupt_payload)

    result = runner.invoke(
        config_group,
        ['set', 'wallet', 'alice'],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert 'not valid JSON' in result.output or 'Refusing to overwrite' in result.output
    # Critical invariant: the corrupt file must remain on disk untouched so
    # the operator can inspect/repair it. The refused write path must NOT
    # have replaced it with a fresh single-key dict.
    assert config_file.read_text() == corrupt_payload


def test_config_set_preserves_existing_keys_when_appending(runner, temp_config):
    """Sanity check the happy path: a normal `set` on a valid file must keep every other key."""
    from gittensor.cli.main import config_group

    _config_dir, config_file = temp_config
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps({'network': 'test', 'contract_address': '5XYZ...'}))

    result = runner.invoke(
        config_group,
        ['set', 'wallet', 'alice'],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    written = json.loads(config_file.read_text())
    assert written == {
        'network': 'test',
        'contract_address': '5XYZ...',
        'wallet': 'alice',
    }


def test_config_set_creates_file_when_missing(runner, temp_config):
    """When the file doesn't exist, `set` should still write a fresh single-key config."""
    from gittensor.cli.main import config_group

    _config_dir, config_file = temp_config
    assert not config_file.exists()

    result = runner.invoke(
        config_group,
        ['set', 'wallet', 'alice'],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(config_file.read_text()) == {'wallet': 'alice'}
