# Entrius 2025

"""Shared helper utilities for miner CLI commands."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from gittensor.cli.json_output import emit_error_json
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
    if config_network and config_network.lower() in NETWORK_MAP:
        return NETWORK_MAP[config_network.lower()]
    if config_endpoint:
        return config_endpoint
    if config_network:
        return config_network
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
    """Print a status/info message to stderr (safe under --json)."""
    err_console.print(message)


def _error(msg: str, json_mode: bool, error_type: str = 'cli_error') -> None:
    """Print an error message in the appropriate format."""
    if json_mode:
        emit_error_json(msg, error_type=error_type)
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
        if excluded:
            msg = (
                f'No validators passed --min-vtrust={min_vtrust:g} / '
                f'--min-stake={min_stake:,.0f} α; all {len(excluded)} candidate(s) excluded.'
            )
            if json_mode:
                emit_error_json(msg, error_type='no_validators_eligible', skipped=excluded)
            else:
                _render_skipped_validators(excluded, json_mode)
                console.print(f'[red]Error: {msg}[/red]')
        else:
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
    if row.get('has_pat') is True and row.get('pat_valid') is None:
        return 'inconclusive'
    return 'no_response'


def _pat_check_aggregate_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """Count PAT check rows by status for JSON summaries."""
    counts = Counter(_pat_check_row_category(r) for r in results)
    return {
        'valid': counts['valid'],
        'no_pat': counts['no_pat'],
        'invalid_pat': counts['invalid_pat'],
        'inconclusive': counts['inconclusive'],
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


def _probe_pat(dendrite, axons: list, uids: list[int]) -> list[dict[str, Any]]:
    """Send a PatCheckSynapse probe to each axon and return check-result rows.

    No PAT is transmitted. Row shape matches `miner check`:
    {uid, hotkey, has_pat, pat_valid, rejection_reason}.
    """
    import asyncio

    from gittensor.synapses import PatCheckSynapse

    synapse = PatCheckSynapse()

    async def _check():
        return await dendrite(axons=axons, synapse=synapse, deserialize=False, timeout=15.0)

    responses = asyncio.run(_check())
    results: list[dict[str, Any]] = []
    for uid, axon, resp in zip(uids, axons, responses):
        results.append(
            {
                'uid': uid,
                'hotkey': axon.hotkey[:16] + '...',
                'has_pat': getattr(resp, 'has_pat', None),
                'pat_valid': getattr(resp, 'pat_valid', None),
                'rejection_reason': getattr(resp, 'rejection_reason', None),
            }
        )
    return results


def _broadcast_pat_with_retry(
    dendrite,
    axons: list,
    uids: list[int],
    pat: str,
    *,
    retries: int = 2,
    delay: float = 2.0,
) -> list[dict[str, Any]]:
    """Broadcast a PAT to validator axons, retrying ONLY those that did not respond.

    A single broadcast is best-effort: a validator that is briefly unreachable
    (mid-restart, transient network blip) returns no response and, without a
    retry, is silently and permanently dropped until the next manual post. This
    retries no-response validators up to ``retries`` times (an explicit
    accept/reject is final), so transient unreachability does not become a silent
    coverage gap.

    Returns one row per uid (input order), shaped like `miner post`:
    {uid, hotkey, accepted, rejection_reason, status_code}.
    """
    import asyncio
    import time

    from gittensor.synapses import PatBroadcastSynapse

    final: dict[int, dict[str, Any]] = {}
    pending: list[tuple[int, Any]] = list(zip(uids, axons))
    for attempt in range(retries + 1):
        if not pending:
            break
        cur_uids = [u for u, _ in pending]
        cur_axons = [a for _, a in pending]
        synapse = PatBroadcastSynapse(github_access_token=pat)

        async def _broadcast(_axons=cur_axons, _synapse=synapse):
            return await dendrite(axons=_axons, synapse=_synapse, deserialize=False, timeout=30.0)

        responses = asyncio.run(_broadcast())
        retry_next: list[tuple[int, Any]] = []
        for (uid, axon), resp in zip(zip(cur_uids, cur_axons), responses):
            accepted = getattr(resp, 'accepted', None)
            reason = getattr(resp, 'rejection_reason', None)
            status_code = getattr(resp.dendrite, 'status_code', None) if hasattr(resp, 'dendrite') else None
            final[uid] = {
                'uid': uid,
                'hotkey': axon.hotkey[:16] + '...',
                'accepted': accepted,
                'rejection_reason': reason,
                'status_code': status_code,
            }
            # Only a no-response (neither accepted nor explicitly rejected) is retried.
            if accepted is None and reason is None:
                retry_next.append((uid, axon))
        pending = retry_next
        if pending and attempt < retries:
            time.sleep(delay)
    return [final[u] for u in uids]
