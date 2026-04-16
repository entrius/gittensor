# Entrius 2025

"""Shared helper utilities for miner CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console

from gittensor.constants import NETWORK_MAP

console = Console()

NETUID_DEFAULT = 74


def _get_validator_axons(metagraph) -> tuple[list, list]:
    """Return (axons, uids) for all active validators (vtrust > 0.1, serving)."""
    axons = []
    uids = []
    for uid in range(metagraph.n):
        if metagraph.validator_trust[uid] > 0.1 and metagraph.axons[uid].is_serving:
            axons.append(metagraph.axons[uid])
            uids.append(uid)
    return axons, uids


def _load_config_value(key: str):
    """Load a value from ~/.gittensor/config.json, or None."""
    config_file = Path.home() / '.gittensor' / 'config.json'
    if not config_file.exists():
        return None
    try:
        config = json.loads(config_file.read_text())
        return config.get(key)
    except (json.JSONDecodeError, OSError):
        return None


def _resolve_endpoint(network: str | None, rpc_url: str | None) -> str:
    """Resolve the subtensor endpoint from CLI args or config."""
    if rpc_url:
        return rpc_url
    if network:
        return NETWORK_MAP.get(network.lower(), network)
    config_network = _load_config_value('network')
    config_endpoint = _load_config_value('ws_endpoint')
    if config_endpoint:
        return config_endpoint
    if config_network:
        return NETWORK_MAP.get(config_network.lower()) or config_network
    return NETWORK_MAP['finney']


def _connect_bittensor(wallet_name: str, wallet_hotkey: str, ws_endpoint: str, netuid: int):
    """Set up and return bittensor wallet, subtensor, metagraph and dendrite."""
    import bittensor as bt

    w = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
    st = bt.Subtensor(network=ws_endpoint)
    mg = st.metagraph(netuid=netuid)
    dd = bt.Dendrite(wallet=w)
    return w, st, mg, dd


def _error(msg: str, json_mode: bool) -> None:
    """Print an error message in the appropriate format."""
    if json_mode:
        click.echo(json.dumps({'success': False, 'error': msg}))
    else:
        console.print(f'[red]Error: {msg}[/red]')
