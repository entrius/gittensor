# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Admin subgroup commands for issue CLI

Commands:
    gitt admin cancel-issue (alias: a cancel-issue)
    gitt admin payout-issue (alias: a payout-issue)
    gitt admin set-owner (alias: a set-owner)
    gitt admin set-treasury (alias: a set-treasury)
"""

from typing import Optional

import click
from rich.console import Console

from .helpers import (
    console,
    get_contract_address,
    get_ws_endpoint,
)


@click.group(name='admin')
def admin():
    """Owner-only administrative commands.

    These commands require the contract owner wallet.

    \b
    Commands:
        cancel-issue   Cancel an issue
        payout-issue   Manual payout fallback
        set-owner      Transfer ownership
        set-treasury   Change treasury hotkey
    """
    pass


@admin.command('cancel-issue')
@click.argument('issue_id', type=int)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name (must be owner)',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
def admin_cancel(issue_id: int, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Cancel an issue (owner only)."""
    console.print(f'[yellow]Admin cancel not yet implemented for CLI.[/yellow]')
    console.print(f'[dim]Use the contract directly or vote_cancel_issue for validator cancellation.[/dim]')


@admin.command('payout-issue')
@click.argument('issue_id', type=int)
@click.argument('solver_coldkey', type=str)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name (must be owner)',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
def admin_payout(issue_id: int, solver_coldkey: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Manual payout fallback (owner only).

    \b
    Arguments:
        ISSUE_ID: Completed issue
        SOLVER_COLDKEY: Payout destination
    """
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Manual payout for issue {issue_id}...[/yellow]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        result = client.payout_bounty(issue_id, solver_coldkey, wallet)
        if result:
            console.print(f'[green]Payout successful! Amount: {result / 1e9:.4f} ALPHA[/green]')
        else:
            console.print('[red]Payout failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('set-owner')
@click.argument('new_owner', type=str)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address',
)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name (must be current owner)',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
def admin_set_owner(new_owner: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Transfer contract ownership."""
    console.print('[yellow]set-owner not yet implemented in CLI.[/yellow]')


@admin.command('set-treasury')
@click.argument('new_treasury', type=str)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address',
)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name (must be owner)',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
def admin_set_treasury(new_treasury: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Change treasury hotkey."""
    console.print('[yellow]set-treasury not yet implemented in CLI.[/yellow]')
