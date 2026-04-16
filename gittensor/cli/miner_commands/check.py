# Entrius 2025

"""gitt miner check — Check how many validators have your PAT stored."""

import asyncio
import json
import sys

import click
from rich.console import Console
from rich.table import Table

from .helpers import (
    NETUID_DEFAULT,
    _connect_bittensor,
    _error,
    _load_config_value,
    _require_registered,
    _require_validator_axons,
    _resolve_endpoint,
    _status,
)

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
    from gittensor.synapses import PatCheckSynapse

    # 1. Resolve wallet and network
    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    ws_endpoint = _resolve_endpoint(network, rpc_url)

    if not json_mode:
        console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {ws_endpoint} | Netuid: {netuid}[/dim]')

    # 2. Set up bittensor objects
    with _status('[bold]Connecting to network...', json_mode):
        try:
            wallet, subtensor, metagraph, dendrite = _connect_bittensor(wallet_name, wallet_hotkey, ws_endpoint, netuid)
        except Exception as e:
            _error(f'Failed to initialize bittensor: {e}', json_mode)
            sys.exit(1)

    # Verify miner is registered
    _require_registered(wallet, metagraph, netuid, json_mode)

    # 3. Find active validator axons (vtrust > 0.1 = actively participating in consensus)
    validator_axons, validator_uids = _require_validator_axons(metagraph, json_mode)

    # 4. Send check probes
    synapse = PatCheckSynapse()

    async def _check():
        return await dendrite(
            axons=validator_axons,
            synapse=synapse,
            deserialize=False,
            timeout=15.0,
        )

    with _status(f'[bold]Checking {len(validator_axons)} validators...', json_mode):
        responses = asyncio.run(_check())

    # 5. Collect results
    results = []
    for uid, axon, resp in zip(validator_uids, validator_axons, responses):
        has_pat = getattr(resp, 'has_pat', None)
        pat_valid = getattr(resp, 'pat_valid', None)
        reason = getattr(resp, 'rejection_reason', None)
        results.append(
            {
                'uid': uid,
                'hotkey': axon.hotkey[:16] + '...',
                'has_pat': has_pat,
                'pat_valid': pat_valid,
                'rejection_reason': reason,
            }
        )

    valid_count = sum(1 for r in results if r['pat_valid'] is True)
    no_response_count = sum(1 for r in results if r['has_pat'] is None)

    # 6. Display results
    if json_mode:
        click.echo(
            json.dumps(
                {
                    'total_validators': len(results),
                    'valid': valid_count,
                    'invalid': len(results) - valid_count - no_response_count,
                    'no_response': no_response_count,
                    'results': results,
                },
                indent=2,
            )
        )
    else:
        table = Table(title='PAT Check Results')
        table.add_column('UID', style='cyan', justify='right')
        table.add_column('Validator', style='dim')
        table.add_column('Status', justify='center')
        table.add_column('Reason', style='dim')

        for r in results:
            if r['pat_valid'] is True:
                status = '[green]✓ valid[/green]'
            elif r['has_pat'] is False:
                status = '[red]✗ no PAT[/red]'
            elif r['pat_valid'] is False:
                status = '[red]✗ invalid[/red]'
            else:
                status = '[yellow]— no response[/yellow]'
            table.add_row(str(r['uid']), r['hotkey'], status, r.get('rejection_reason') or '')

        console.print(table)
        console.print(f'\n[bold]{valid_count}/{len(results)} validators have a valid PAT stored.[/bold]')
