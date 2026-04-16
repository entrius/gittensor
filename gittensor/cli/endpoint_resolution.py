# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared Subtensor WS endpoint resolution for CLI commands (issues, miner, etc.)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from gittensor.constants import NETWORK_MAP

GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'

# Reverse lookup: URL -> network name
_URL_TO_NETWORK = {url: name for name, url in NETWORK_MAP.items()}


def load_config() -> Dict[str, Any]:
    """
    Load configuration from ~/.gittensor/config.json.

    Priority:
    1. CLI arguments (highest - handled by callers)
    2. ~/.gittensor/config.json
    3. Defaults

    Returns:
        Dict with all config keys (empty dict if missing or invalid).
    """
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def resolve_network(
    network: Optional[str] = None, rpc_url: Optional[str] = None
) -> Tuple[str, str]:
    """
    Resolve --network and --rpc-url into (ws_endpoint, network_name).

    Priority:
        1. --rpc-url (explicit URL always wins)
        2. --network (mapped to known endpoint)
        3. Config file ws_endpoint / network
        4. Default: finney (mainnet)

    Args:
        network: Network name from --network option (test/finney/local)
        rpc_url: Explicit RPC URL from --rpc-url option

    Returns:
        Tuple of (ws_endpoint, network_name)
    """
    # --rpc-url takes highest priority
    if rpc_url:
        name = _URL_TO_NETWORK.get(rpc_url, 'custom')
        return rpc_url, name

    # --network maps to a known endpoint
    if network:
        key = network.lower()
        if key in NETWORK_MAP:
            return NETWORK_MAP[key], key
        # Treat unknown network value as a custom URL
        return network, 'custom'

    # Fall back to config file
    config = load_config()
    if config.get('ws_endpoint'):
        endpoint = config['ws_endpoint']
        name = _URL_TO_NETWORK.get(endpoint, config.get('network', 'custom'))
        return endpoint, name

    config_network = config.get('network', '').lower()
    if config_network and config_network in NETWORK_MAP:
        return NETWORK_MAP[config_network], config_network

    # Default: finney (mainnet)
    return NETWORK_MAP['finney'], 'finney'


def resolve_ws_endpoint(network: Optional[str] = None, rpc_url: Optional[str] = None) -> str:
    """Return only the WebSocket endpoint URL (same rules as resolve_network)."""
    return resolve_network(network, rpc_url)[0]
