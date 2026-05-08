# Entrius 2025

"""Shared helper utilities for miner CLI commands."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from gittensor.constants import NETWORK_MAP

console = Console()
err_console = Console(stderr=True)

NETUID_DEFAULT = 74
DEFAULT_MIN_VALIDATOR_VTRUST = 0.25
DEFAULT_MIN_VALIDATOR_STAKE = 15_000.0


def _get_validator_axons(
    metagraph,
    *,
    min_vtrust: float = DEFAULT_MIN_VALIDATOR_VTRUST,
    min_stake: float = DEFAULT_MIN_VALIDATOR_STAKE,
) -> tuple[list, list, list[dict]]:
    """Return (axons, uids, excluded) for active validators.

    A validator is broadcast to when vtrust > min_vtrust AND axon.is_serving
    AND stake >= min_stake. UIDs failing only the latter two checks are
    surfaced in `excluded` so miners can see why a high-vtrust validator
    was skipped. Sub-vtrust UIDs are dropped silently — they are not
    validators.
    """
    axons: list = []
    uids: list[int] = []
    excluded: list[dict] = []
    for uid in range(metagraph.n):
        vt = float(metagraph.validator_trust[uid])
        if vt <= min_vtrust:
            continue
        serving = bool(metagraph.axons[uid].is_serving)
        stake = float(metagraph.S[uid])
        reasons: list[str] = []
        if not serving:
            reasons.append('not serving an axon')
        if stake < min_stake:
            reasons.append(f'stake {stake:,.0f} α below {min_stake:,.0f} α threshold')
        if reasons:
            excluded.append({'uid': uid, 'vtrust': vt, 'stake': stake, 'reasons': reasons})
            continue
        axons.append(metagraph.axons[uid])
        uids.append(uid)
    return axons, uids, excluded


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


def _status(message: str):
    """Rich spinner bound to stderr so it never pollutes JSON stdout."""
    return err_console.status(message)


def _print(message: str) -> None:
    """Print a status/info message to stderr (safe under --json-output)."""
    err_console.print(message)


def _error(msg: str, json_mode: bool) -> None:
    """Print an error message in the appropriate format."""
    if json_mode:
        click.echo(json.dumps({'success': False, 'error': msg}))
    else:
        err_console.print(f'[red]Error: {msg}[/red]')


def _require_registered(wallet, metagraph, netuid: int, json_mode: bool) -> None:
    """Exit with error if wallet hotkey is not registered on the subnet."""
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        _error(f'Hotkey {wallet.hotkey.ss58_address[:16]}... is not registered on subnet {netuid}.', json_mode)
        sys.exit(1)


def _require_validator_axons(
    metagraph,
    json_mode: bool,
    *,
    min_vtrust: float = DEFAULT_MIN_VALIDATOR_VTRUST,
    min_stake: float = DEFAULT_MIN_VALIDATOR_STAKE,
) -> tuple[list, list, list[dict]]:
    """Return validator (axons, uids, excluded), or exit with error if no axons match."""
    validator_axons, validator_uids, excluded = _get_validator_axons(
        metagraph, min_vtrust=min_vtrust, min_stake=min_stake
    )
    if not validator_axons:
        _error('No reachable validator axons found on the network.', json_mode)
        sys.exit(1)
    return validator_axons, validator_uids, excluded


def _render_skipped_validators(excluded: list[dict], json_mode: bool) -> None:
    """Print a 'Skipped Validators' table when any high-vtrust UIDs were filtered."""
    if json_mode or not excluded:
        return
    table = Table(title='Skipped Validators')
    table.add_column('UID', style='cyan', justify='right')
    table.add_column('vtrust', justify='right')
    table.add_column('stake (α)', justify='right')
    table.add_column('Reason', style='dim')
    for e in excluded:
        table.add_row(
            str(e['uid']),
            f'{e["vtrust"]:.4f}',
            f'{e["stake"]:,.0f}',
            '; '.join(e['reasons']),
        )
    console.print(table)


def _pat_check_row_category(row: dict[str, Any]) -> str:
    """Classify a PAT probe row; must match `miner check` table rendering order."""
    if row.get('pat_valid') is True:
        return 'valid'
    if row.get('has_pat') is False:
        return 'no_pat'
    if row.get('has_pat') is True and row.get('pat_valid') is False:
        return 'invalid_pat'
    return 'no_response'


def _pat_check_aggregate_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """Count PAT check rows by status for JSON summaries."""
    counts = Counter(_pat_check_row_category(r) for r in results)
    return {
        'valid': counts['valid'],
        'no_pat': counts['no_pat'],
        'invalid_pat': counts['invalid_pat'],
        'no_response': counts['no_response'],
    }


def _pat_post_row_category(row: dict[str, Any]) -> str:
    """Classify a PAT broadcast row; must match `miner post` table rendering order."""
    if row.get('accepted') is True:
        return 'accepted'
    if row.get('accepted') is False:
        return 'rejected'
    return 'no_response'


def _pat_post_aggregate_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """Count PAT broadcast rows by status for JSON summaries."""
    counts = Counter(_pat_post_row_category(r) for r in results)
    return {
        'accepted': counts['accepted'],
        'rejected': counts['rejected'],
        'no_response': counts['no_response'],
    }
