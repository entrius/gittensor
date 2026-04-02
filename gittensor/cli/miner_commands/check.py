# Entrius 2025

"""gitt miner check — Check how many validators have your PAT stored."""

import asyncio
import json
import sys

import click
from rich.console import Console
from rich.table import Table

from .post import NETUID_DEFAULT, NETWORK_MAP, _load_config_value, _resolve_endpoint

console = Console()


@click.command()
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option('--json-output', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def miner_check(wallet_name, wallet_hotkey, netuid, network, rpc_url, json_mode):
    """Check how many validators have your PAT stored.

    Sends a lightweight probe to each validator — no PAT is transmitted.

    \b
    Examples:
        gitt miner check --wallet alice --hotkey default
        gitt miner check --wallet alice --hotkey default --network test
    """
    import bittensor as bt

    from gittensor.synapses import PatCheckSynapse

    # 1. Resolve wallet and network
    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    ws_endpoint = _resolve_endpoint(network, rpc_url)

    if not json_mode:
        console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {ws_endpoint} | Netuid: {netuid}[/dim]')

    # 2. Set up bittensor objects
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

    # 3. Find validator axons
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
        console.print(f'[dim]Checking {len(validator_axons)} validators...[/dim]')

    # 4. Send check probes
    synapse = PatCheckSynapse()

    responses = asyncio.get_event_loop().run_until_complete(
        dendrite(
            axons=validator_axons,
            synapse=synapse,
            deserialize=False,
            timeout=15.0,
        )
    )

    # 5. Collect results
    results = []
    for uid, axon, resp in zip(validator_uids, validator_axons, responses):
        has_pat = getattr(resp, 'has_pat', None)
        results.append({
            'uid': uid,
            'hotkey': axon.hotkey[:16] + '...',
            'has_pat': has_pat,
        })

    has_count = sum(1 for r in results if r['has_pat'] is True)
    no_response_count = sum(1 for r in results if r['has_pat'] is None)

    # 6. Display results
    if json_mode:
        click.echo(json.dumps({
            'total_validators': len(results),
            'have_pat': has_count,
            'missing_pat': len(results) - has_count - no_response_count,
            'no_response': no_response_count,
            'results': results,
        }, indent=2))
    else:
        table = Table(title='PAT Check Results')
        table.add_column('UID', style='cyan', justify='right')
        table.add_column('Validator', style='dim')
        table.add_column('Has PAT', justify='center')

        for r in results:
            if r['has_pat'] is True:
                status = '[green]Yes[/green]'
            elif r['has_pat'] is False:
                status = '[red]No[/red]'
            else:
                status = '[yellow]No Response[/yellow]'
            table.add_row(str(r['uid']), r['hotkey'], status)

        console.print(table)
        console.print(f'\n[bold]{has_count}/{len(results)} validators have your PAT stored.[/bold]')


def _error(msg: str, json_mode: bool):
    """Print an error message in the appropriate format."""
    if json_mode:
        click.echo(json.dumps({'success': False, 'error': msg}))
    else:
        console.print(f'[red]Error: {msg}[/red]')
