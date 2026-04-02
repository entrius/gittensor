# Entrius 2025

"""gitt miner post — Broadcast GitHub PAT to validators."""

import asyncio
import json
import os
import sys

import click
import requests
from rich.console import Console
from rich.table import Table

from gittensor.constants import BASE_GITHUB_API_URL

console = Console()

# Shared CLI options for wallet/network configuration
NETUID_DEFAULT = 2


@click.command()
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option('--json-output', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def miner_post(wallet_name, wallet_hotkey, netuid, network, rpc_url, json_mode):
    """Broadcast your GitHub PAT to all validators on the network.

    Validators will validate your PAT (test GitHub API access, check account age),
    then store it locally for use during scoring rounds.

    \b
    Requires:
        GITTENSOR_MINER_PAT environment variable set to a valid GitHub PAT.

    \b
    Examples:
        gitt miner post --wallet alice --hotkey default
        gitt miner post --wallet alice --hotkey default --network test
        gitt miner post --wallet alice --hotkey default --rpc-url ws://localhost:9944
    """
    import bittensor as bt

    from gittensor.synapses import PatBroadcastSynapse

    # 1. Load and validate PAT locally
    pat = os.environ.get('GITTENSOR_MINER_PAT')
    if not pat:
        _error('GITTENSOR_MINER_PAT environment variable is not set.', json_mode)
        sys.exit(1)

    if not json_mode:
        console.print('[dim]Validating PAT locally...[/dim]')

    if not _validate_pat_locally(pat):
        _error('GitHub PAT is invalid or expired. Check your GITTENSOR_MINER_PAT.', json_mode)
        sys.exit(1)

    if not json_mode:
        console.print('[green]PAT is valid.[/green]')

    # 2. Resolve wallet and network
    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    ws_endpoint = _resolve_endpoint(network, rpc_url)

    if not json_mode:
        console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {ws_endpoint} | Netuid: {netuid}[/dim]')

    # 3. Set up bittensor objects
    try:
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        metagraph = subtensor.metagraph(netuid=netuid)
        dendrite = bt.Dendrite(wallet=wallet)
    except Exception as e:
        _error(f'Failed to initialize bittensor: {e}', json_mode)
        sys.exit(1)

    # Verify miner is registered
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        _error(f'Hotkey {wallet.hotkey.ss58_address[:16]}... is not registered on subnet {netuid}.', json_mode)
        sys.exit(1)

    # 4. Find validator axons
    validator_axons = []
    validator_uids = []
    for uid in range(metagraph.n):
        if metagraph.validator_permit[uid] and metagraph.axons[uid].is_serving:
            validator_axons.append(metagraph.axons[uid])
            validator_uids.append(uid)

    if not validator_axons:
        _error('No reachable validator axons found on the network.', json_mode)
        sys.exit(1)

    if not json_mode:
        console.print(f'[dim]Broadcasting to {len(validator_axons)} validators...[/dim]')

    # 5. Broadcast
    synapse = PatBroadcastSynapse(github_access_token=pat)

    responses = asyncio.get_event_loop().run_until_complete(
        dendrite(
            axons=validator_axons,
            synapse=synapse,
            deserialize=False,
            timeout=30.0,
        )
    )

    # 6. Collect results
    results = []
    for uid, axon, resp in zip(validator_uids, validator_axons, responses):
        accepted = getattr(resp, 'accepted', None)
        reason = getattr(resp, 'rejection_reason', None)
        status_code = getattr(resp.dendrite, 'status_code', None) if hasattr(resp, 'dendrite') else None
        results.append({
            'uid': uid,
            'hotkey': axon.hotkey[:16] + '...',
            'accepted': accepted,
            'rejection_reason': reason,
            'status_code': status_code,
        })

    accepted_count = sum(1 for r in results if r['accepted'] is True)

    # 7. Display results
    if json_mode:
        click.echo(json.dumps({
            'success': accepted_count > 0,
            'total_validators': len(results),
            'accepted': accepted_count,
            'rejected': len(results) - accepted_count,
            'results': results,
        }, indent=2))
    else:
        table = Table(title='PAT Broadcast Results')
        table.add_column('UID', style='cyan', justify='right')
        table.add_column('Validator', style='dim')
        table.add_column('Status', justify='center')
        table.add_column('Reason', style='dim')

        for r in results:
            if r['accepted'] is True:
                status = '[green]✓[/green]'
            elif r['accepted'] is False:
                status = '[red]✗[/red]'
            else:
                status = '[yellow]—[/yellow]'
            table.add_row(str(r['uid']), r['hotkey'], status, r.get('rejection_reason') or '')

        console.print(table)
        console.print(f'\n[bold]{accepted_count}/{len(results)} validators accepted your PAT.[/bold]')


def _validate_pat_locally(pat: str) -> bool:
    """Quick check that the PAT works against GitHub API."""
    headers = {'Authorization': f'token {pat}', 'Accept': 'application/vnd.github.v3+json'}
    try:
        response = requests.get(f'{BASE_GITHUB_API_URL}/user', headers=headers, timeout=15)
        return response.status_code == 200
    except requests.RequestException:
        return False


def _load_config_value(key: str):
    """Load a value from ~/.gittensor/config.json, or None."""
    from pathlib import Path
    config_file = Path.home() / '.gittensor' / 'config.json'
    if not config_file.exists():
        return None
    try:
        config = json.loads(config_file.read_text())
        return config.get(key)
    except (json.JSONDecodeError, OSError):
        return None


NETWORK_MAP = {
    'local': 'ws://127.0.0.1:9944',
    'test': 'wss://test.finney.opentensor.ai:443/',
    'finney': 'wss://entrypoint-finney.opentensor.ai:443/',
}


def _resolve_endpoint(network: str | None, rpc_url: str | None) -> str:
    """Resolve the subtensor endpoint from CLI args or config."""
    if rpc_url:
        return rpc_url
    if network:
        return NETWORK_MAP.get(network, network)
    # Try config file
    config_network = _load_config_value('network')
    config_endpoint = _load_config_value('ws_endpoint')
    if config_endpoint:
        return config_endpoint
    if config_network:
        return NETWORK_MAP.get(config_network, config_network)
    return NETWORK_MAP['finney']


def _error(msg: str, json_mode: bool):
    """Print an error message in the appropriate format."""
    if json_mode:
        click.echo(json.dumps({'success': False, 'error': msg}))
    else:
        console.print(f'[red]Error: {msg}[/red]')
