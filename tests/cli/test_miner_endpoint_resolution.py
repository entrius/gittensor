"""Unit tests for miner CLI `_resolve_endpoint` (no network, no chain, no wallet)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gittensor.cli.miner_commands.helpers import _resolve_endpoint
from gittensor.constants import NETWORK_MAP


def _write_gittensor_config(home: Path, payload: dict) -> None:
    cfg_dir = home / '.gittensor'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'config.json').write_text(json.dumps(payload), encoding='utf-8')


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at tmp_path so ~/.gittensor/config.json resolves under tmp_path."""
    monkeypatch.setenv('HOME', str(tmp_path))
    return tmp_path


def test_rpc_url_wins_over_network(isolated_home: Path) -> None:
    assert _resolve_endpoint(network='test', rpc_url='ws://custom:9944') == 'ws://custom:9944'


def test_rpc_url_wins_over_config_file(isolated_home: Path) -> None:
    _write_gittensor_config(isolated_home, {'network': 'test', 'ws_endpoint': 'wss://ignored.example/ws'})
    assert _resolve_endpoint(network=None, rpc_url='ws://override:1') == 'ws://override:1'


def test_named_network_test_uses_network_map(isolated_home: Path) -> None:
    assert _resolve_endpoint(network='test', rpc_url=None) == NETWORK_MAP['test']


def test_named_network_finney_uses_network_map(isolated_home: Path) -> None:
    assert _resolve_endpoint(network='finney', rpc_url=None) == NETWORK_MAP['finney']


def test_unknown_network_string_passthrough(isolated_home: Path) -> None:
    custom = 'ws://custom-testnet:9944'
    assert _resolve_endpoint(network=custom, rpc_url=None) == custom


def test_cli_network_wins_over_config_file(isolated_home: Path) -> None:
    _write_gittensor_config(isolated_home, {'network': 'finney'})
    assert _resolve_endpoint(network='test', rpc_url=None) == NETWORK_MAP['test']


def test_config_network_used_when_no_cli_args(isolated_home: Path) -> None:
    """Flat `network` key matches `_load_config_value('network')` in helpers."""
    _write_gittensor_config(isolated_home, {'network': 'test'})
    assert _resolve_endpoint(network=None, rpc_url=None) == NETWORK_MAP['test']


def test_config_ws_endpoint_wins_over_config_network(isolated_home: Path) -> None:
    """When both are set in config, `ws_endpoint` is checked first in `_resolve_endpoint`."""
    _write_gittensor_config(
        isolated_home,
        {'network': 'test', 'ws_endpoint': 'wss://priority.example/ws'},
    )
    assert _resolve_endpoint(network=None, rpc_url=None) == 'wss://priority.example/ws'


def test_config_network_unknown_string_passthrough(isolated_home: Path) -> None:
    custom = 'ws://from-config-only:1234'
    _write_gittensor_config(isolated_home, {'network': custom})
    assert _resolve_endpoint(network=None, rpc_url=None) == custom


def test_defaults_to_finney_when_no_config_and_no_args(isolated_home: Path) -> None:
    assert _resolve_endpoint(network=None, rpc_url=None) == NETWORK_MAP['finney']
