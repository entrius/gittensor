# Entrius 2025

"""Shared helper utilities for miner CLI commands."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
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


def _resolve_wallet_hotkey_name(wallet_name: str, hotkey_spec: str) -> str:
    """Resolve CLI --hotkey to a wallet hotkey *file name*.

    Bittensor stores keys as ``~/.bittensor/wallets/<wallet>/hotkeys/<name>``.
    Users often paste the SS58 address instead of the key name; map that when possible.
    """
    hdir = Path.home() / '.bittensor' / 'wallets' / wallet_name / 'hotkeys'
    if not hdir.is_dir():
        return hotkey_spec
    direct = hdir / hotkey_spec
    if direct.is_file():
        return hotkey_spec

    import bittensor as bt

    matches: list[str] = []
    for p in sorted(hdir.iterdir()):
        if not p.is_file() or p.name.endswith('pub.txt'):
            continue
        try:
            w = bt.Wallet(name=wallet_name, hotkey=p.name)
            if w.hotkey.ss58_address == hotkey_spec:
                matches.append(p.name)
        except Exception:
            continue

    if len(matches) > 1:
        raise ValueError(f'Multiple hotkey files match SS58 {hotkey_spec}: {matches}')
    if len(matches) == 1:
        return matches[0]

    available = sorted(
        p.name for p in hdir.iterdir() if p.is_file() and not p.name.endswith('pub.txt')
    )
    raise ValueError(
        f"No hotkey file {hotkey_spec!r} under wallet {wallet_name!r} "
        f'({hdir}). Use the key name (filename), not the SS58, unless that address '
        f'matches a local hotkey file. Available names: {", ".join(available) or "(none)"}'
    )


def _connect_bittensor(wallet_name: str, wallet_hotkey: str, ws_endpoint: str, netuid: int):
    """Set up and return bittensor wallet, subtensor, metagraph and dendrite."""
    import bittensor as bt

    resolved_hotkey = _resolve_wallet_hotkey_name(wallet_name, wallet_hotkey)
    w = bt.Wallet(name=wallet_name, hotkey=resolved_hotkey)
    _ = w.hotkey.ss58_address  # load before Dendrite so failures do not construct a half-initialized dendrite
    st = bt.Subtensor(network=ws_endpoint)
    mg = st.metagraph(netuid=netuid)
    dd = bt.Dendrite(wallet=w)
    return w, st, mg, dd


def _status(message: str, json_mode: bool):
    """Rich spinner in TTY mode, no-op in JSON mode."""
    return nullcontext() if json_mode else console.status(message)


def _print(message: str, json_mode: bool) -> None:
    """Print a message in TTY mode; no-op in JSON mode."""
    if not json_mode:
        console.print(message)


def _error(msg: str, json_mode: bool) -> None:
    """Print an error message in the appropriate format."""
    if json_mode:
        click.echo(json.dumps({'success': False, 'error': msg}))
    else:
        console.print(f'[red]Error: {msg}[/red]')


def _require_registered(wallet, metagraph, netuid: int, json_mode: bool) -> None:
    """Exit with error if wallet hotkey is not registered on the subnet."""
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        _error(f'Hotkey {wallet.hotkey.ss58_address[:16]}... is not registered on subnet {netuid}.', json_mode)
        sys.exit(1)


def _require_validator_axons(metagraph, json_mode: bool) -> tuple[list, list]:
    """Return validator (axons, uids), or exit with error if none found."""
    validator_axons, validator_uids = _get_validator_axons(metagraph)
    if not validator_axons:
        _error('No reachable validator axons found on the network.', json_mode)
        sys.exit(1)
    return validator_axons, validator_uids
