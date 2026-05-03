# Entrius 2025

"""gitt doctor — One-shot health check for the local miner/validator environment."""

from __future__ import annotations

import json
import os
import sys

import click
import requests
from rich.console import Console

from gittensor.cli.miner_commands.helpers import (
    NETUID_DEFAULT,
    _load_config_value,
    _resolve_endpoint,
)
from gittensor.constants import BASE_GITHUB_API_URL, GITHUB_HTTP_TIMEOUT_SECONDS

console = Console()

_PASS = '[green]✓[/green]'
_FAIL = '[red]✗[/red]'
_WARN = '[yellow]⚠[/yellow]'


def _check(label: str, ok: bool, detail: str = '') -> bool:
    icon = _PASS if ok else _FAIL
    suffix = f'  [dim]{detail}[/dim]' if detail else ''
    console.print(f'  {icon} {label}{suffix}')
    return ok


@click.command('doctor')
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, show_default=True, help='Subnet UID.')
@click.option('--network', default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option(
    '--pat',
    default=None,
    help='GitHub Personal Access Token. Falls back to GITTENSOR_MINER_PAT env var.',
)
def doctor(wallet_name, wallet_hotkey, netuid, network, rpc_url, pat):
    """Run a one-shot health check across the local miner environment.

    Checks wallet access, subtensor connectivity, GitHub PAT validity,
    rate limits, and contract reachability.

    \b
    Examples:
        gitt doctor --wallet alice --hotkey default
        gitt doctor --wallet alice --hotkey default --network test
    """
    console.print('\n[bold cyan]Gittensor Doctor[/bold cyan]\n')
    failures = 0

    # ── 1. Wallet ──────────────────────────────────────────────────────────
    console.print('[bold]Wallet[/bold]')
    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'

    try:
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        coldkey_path = wallet.coldkeypub_file.path
        hotkey_addr = wallet.hotkey.ss58_address
        if not _check('Coldkey readable', os.path.exists(coldkey_path), coldkey_path):
            failures += 1
        _check('Hotkey loaded', True, f'{hotkey_addr[:12]}...')
    except Exception as e:
        if not _check('Wallet loaded', False, str(e)):
            failures += 1
        wallet = None
        hotkey_addr = None

    # ── 2. Subtensor ───────────────────────────────────────────────────────
    console.print('\n[bold]Subtensor[/bold]')
    ws_endpoint = _resolve_endpoint(network, rpc_url)

    try:
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        block = subtensor.get_current_block()
        _check('Subtensor reachable', True, f'{ws_endpoint} (block {block:,})')

        if wallet and hotkey_addr:
            metagraph = subtensor.metagraph(netuid)
            registered = hotkey_addr in metagraph.hotkeys
            if not _check(
                f'Hotkey registered on netuid {netuid}',
                registered,
                '' if registered else f'{hotkey_addr[:12]}... not found',
            ):
                failures += 1
    except Exception as e:
        if not _check('Subtensor reachable', False, str(e)):
            failures += 1

    # ── 3. GitHub PAT ──────────────────────────────────────────────────────
    console.print('\n[bold]GitHub PAT[/bold]')
    pat = pat or os.environ.get('GITTENSOR_MINER_PAT')

    if not pat:
        if not _check('PAT configured', False, 'Set --pat or GITTENSOR_MINER_PAT'):
            failures += 1
    else:
        try:
            headers = {
                'Authorization': f'token {pat}',
                'Accept': 'application/vnd.github.v3+json',
            }
            resp = requests.get(
                f'{BASE_GITHUB_API_URL}/user',
                headers=headers,
                timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                login = resp.json().get('login', 'unknown')
                limit = resp.headers.get('X-RateLimit-Remaining', '?')
                total = resp.headers.get('X-RateLimit-Limit', '?')
                _check('PAT valid', True, f'{login} (rate limit {limit}/{total})')
            else:
                if not _check('PAT valid', False, f'HTTP {resp.status_code}'):
                    failures += 1
        except Exception as e:
            if not _check('PAT valid', False, str(e)):
                failures += 1

    # ── 4. Contract ────────────────────────────────────────────────────────
    console.print('\n[bold]Contract[/bold]')
    try:
        from gittensor.cli.issue_commands.helpers import (
            _make_contract_client,
            _resolve_contract_and_network,
        )
        from gittensor.validator.issue_competitions.contract_client import IssueStatus

        contract = _load_config_value('contract') or ''
        contract_addr, ws_ep, network_name = _resolve_contract_and_network(contract, network, rpc_url)
        _, client = _make_contract_client(contract_addr, ws_ep, wallet_name, wallet_hotkey)
        issues = client.get_issues_by_status(IssueStatus.ACTIVE)
        _check('Contract reachable', True, f'{contract_addr[:12]}... ({len(issues)} issues)')
    except Exception as e:
        if not _check('Contract reachable', False, str(e)):
            failures += 1

    # ── 5. miner_pats.json ─────────────────────────────────────────────────
    console.print('\n[bold]Local State[/bold]')
    from pathlib import Path

    pats_file = Path.home() / '.gittensor' / 'miner_pats.json'
    if pats_file.exists():
        try:
            data = json.loads(pats_file.read_text())
            _check('miner_pats.json readable', True, f'{len(data)} entries')
        except Exception as e:
            if not _check('miner_pats.json readable', False, str(e)):
                failures += 1
    else:
        _check('miner_pats.json', True, 'not present (normal for fresh install)')

    # ── Summary ────────────────────────────────────────────────────────────
    console.print()
    if failures == 0:
        console.print('[bold green]All checks passed.[/bold green]')
    else:
        console.print(f'[bold red]{failures} check(s) failed.[/bold red]')
        sys.exit(1)
