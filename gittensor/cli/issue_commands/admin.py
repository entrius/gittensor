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
    """Cancel an issue (owner only).

    Immediately cancels an issue without requiring validator consensus.
    Bounty funds are returned to the alpha pool.

    \b
    Arguments:
        ISSUE_ID: Issue to cancel
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Cancelling issue {issue_id}...[/yellow]\n')

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

        # Show issue info before cancellation
        issue = client.get_issue(issue_id)
        if issue:
            console.print(f'  Issue: {issue.repository_full_name}#{issue.issue_number}')
            console.print(f'  Status: {issue.status.name}')
            console.print(f'  Bounty: {issue.bounty_amount / 1e9:.4f} ALPHA\n')

        result = client.cancel_issue(issue_id, wallet)
        if result:
            console.print(f'[green]Issue {issue_id} cancelled successfully![/green]')
        else:
            console.print('[red]Cancellation failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('payout-issue')
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
def admin_payout(issue_id: int, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Manual payout fallback (owner only).

    Pays out a completed issue bounty to the solver. The solver address
    is determined by validator consensus and stored in the contract.

    \b
    Arguments:
        ISSUE_ID: Completed issue ID
    """
    contract_addr = get_contract_address(contract)
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

        # Show issue info before payout
        issue = client.get_issue(issue_id)
        if issue:
            console.print(f'  Issue: {issue.repository_full_name}#{issue.issue_number}')
            console.print(f'  Status: {issue.status.name}')
            console.print(f'  Bounty: {issue.bounty_amount / 1e9:.4f} ALPHA\n')

        result = client.payout_bounty(issue_id, wallet)
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
    """Transfer contract ownership (owner only).

    \b
    Arguments:
        NEW_OWNER: SS58 address of the new owner
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Transferring ownership to {new_owner}...[/yellow]\n')

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

        result = client.set_owner(new_owner, wallet)
        if result:
            console.print(f'[green]Ownership transferred to {new_owner}![/green]')
        else:
            console.print('[red]Ownership transfer failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


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
    """Change treasury hotkey (owner only).

    The treasury hotkey receives staking emissions that fund bounty payouts.
    Changing the treasury resets all Active/Registered issue bounty amounts
    to 0 (they will be re-funded on next harvest from the new treasury).

    \b
    Arguments:
        NEW_TREASURY: SS58 address of the new treasury hotkey
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Setting treasury hotkey to {new_treasury}...[/yellow]\n')

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

        result = client.set_treasury_hotkey(new_treasury, wallet)
        if result:
            console.print(f'[green]Treasury hotkey updated to {new_treasury}![/green]')
            console.print(f'[dim]Note: Issue bounty amounts have been reset. Run harvest to re-fund from new treasury.[/dim]')
        else:
            console.print('[red]Treasury hotkey update failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')
