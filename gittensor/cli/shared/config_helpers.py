"""Shared config loading and endpoint resolution for CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from gittensor.constants import NETWORK_MAP

CONFIG_FILE = Path.home() / '.gittensor' / 'config.json'

_URL_TO_NETWORK = {url: name for name, url in NETWORK_MAP.items()}


def load_config() -> Dict[str, Any]:
    """Load ~/.gittensor/config.json, returning empty dict if missing."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def resolve_network(
    network: Optional[str] = None,
    rpc_url: Optional[str] = None,
) -> Tuple[str, str]:
    """Resolve --network and --rpc-url into (ws_endpoint, network_name).

    Precedence:
        1. --rpc-url (explicit URL always wins)
        2. --network (mapped to known endpoint)
        3. Config file ws_endpoint / network
        4. Default: finney (mainnet)
    """
    if rpc_url:
        name = _URL_TO_NETWORK.get(rpc_url, 'custom')
        return rpc_url, name

    if network:
        key = network.lower()
        if key in NETWORK_MAP:
            return NETWORK_MAP[key], key
        return network, 'custom'

    config = load_config()
    if config.get('ws_endpoint'):
        endpoint = config['ws_endpoint']
        name = _URL_TO_NETWORK.get(endpoint, config.get('network', 'custom'))
        return endpoint, name

    config_network = config.get('network', '').lower()
    if config_network and config_network in NETWORK_MAP:
        return NETWORK_MAP[config_network], config_network

    return NETWORK_MAP['finney'], 'finney'
