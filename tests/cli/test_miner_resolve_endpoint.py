# Entrius 2025

"""Tests for miner CLI subtensor endpoint resolution."""

from unittest.mock import patch

import pytest

from gittensor.cli.miner_commands.helpers import _resolve_endpoint
from gittensor.constants import NETWORK_MAP


def test_resolve_endpoint_rpc_url_requires_ws_scheme() -> None:
    with pytest.raises(ValueError, match='rpc-url'):
        _resolve_endpoint(None, 'entrypoint-finney.opentensor.ai:443')


def test_resolve_endpoint_rpc_url_strips_and_accepts_wss() -> None:
    out = _resolve_endpoint(None, '  wss://entrypoint-finney.opentensor.ai:443  ')
    assert out == 'wss://entrypoint-finney.opentensor.ai:443'


def test_resolve_endpoint_rpc_url_whitespace_only_falls_through_to_network() -> None:
    assert _resolve_endpoint('finney', '  \t') == NETWORK_MAP['finney']


def test_resolve_endpoint_config_ws_endpoint_requires_scheme() -> None:
    def _load(key: str):
        if key == 'ws_endpoint':
            return 'bad.example.com:443'
        if key == 'network':
            return None
        return None

    with patch('gittensor.cli.miner_commands.helpers._load_config_value', side_effect=_load):
        with pytest.raises(ValueError, match='ws_endpoint'):
            _resolve_endpoint(None, None)


def test_resolve_endpoint_config_ws_endpoint_ok() -> None:
    def _load(key: str):
        if key == 'ws_endpoint':
            return '  wss://custom.example/  '
        if key == 'network':
            return None
        return None

    with patch('gittensor.cli.miner_commands.helpers._load_config_value', side_effect=_load):
        assert _resolve_endpoint(None, None) == 'wss://custom.example/'
