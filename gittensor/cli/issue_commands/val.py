# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Validator subgroup commands for issue CLI

Commands:
    gitt issue val vote-solution (alias: solution)
    gitt issue val vote-cancel-issue (alias: cancel)
"""

import click

from .helpers import (
    console,
    get_contract_address,
    get_ws_endpoint,
)


@click.group(name='val')
def val():
    """Validator consensus operations

    These commands are used by validators to manage issue bounty payouts.

    \b
    Commands:
        vote-solution        Vote for a solver (alias: solution)
        vote-cancel-issue    Vote to cancel issue (alias: cancel)
    """
    pass


@val.command('vote-solution')
@click.argument('issue_id', type=int)
@click.argument('solver_hotkey', type=str)
@click.argument('solver_coldkey', type=str)
@click.argument('pr_url', type=str)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
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
def val_vote_solution(
    issue_id: int,
    solver_hotkey: str,
    solver_coldkey: str,
    pr_url: str,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Vote for a solution on an active issue (triggers auto-payout on consensus).

    \b
    Arguments:
        ISSUE_ID: Issue to vote on
        SOLVER_HOTKEY: Solver's hotkey
        SOLVER_COLDKEY: Solver's coldkey (payout destination)
        PR_URL: URL of the solving PR

    \b
    Examples:
        gitt issue val vote-solution 1 5Hxxx... 5Hyyy... https://github.com/.../pull/123
        gitt i val solution 42 <hotkey> <coldkey> <pr_url>
    """
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Voting on solution for issue {issue_id}...[/yellow]\n')
    console.print(f'  Solver Hotkey:  {solver_hotkey}')
    console.print(f'  Solver Coldkey: {solver_coldkey}')
    console.print(f'  PR URL: {pr_url}\n')

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

        result = client.vote_solution(issue_id, solver_hotkey, solver_coldkey, pr_url, wallet)
        if result:
            console.print(f'[green]Solution vote submitted![/green]')
        else:
            console.print('[red]Vote failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')

# Add 'solution' alias
val.add_command(val_vote_solution, name='solution')


@val.command('vote-cancel-issue')
@click.argument('issue_id', type=int)
@click.argument('reason', type=str)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
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
def val_vote_cancel_issue(
    issue_id: int,
    reason: str,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Vote to cancel an issue (works on Registered or Active).

    \b
    Arguments:
        ISSUE_ID: Issue to cancel
        REASON: Reason for cancellation

    \b
    Examples:
        gitt issue val vote-cancel-issue 1 "External solution found"
        gitt i val cancel 42 "Issue invalid"
    """
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Voting to cancel issue {issue_id}...[/yellow]\n')
    console.print(f'  Reason: {reason}\n')

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

        result = client.vote_cancel_issue(issue_id, reason, wallet)
        if result:
            console.print(f'[green]Vote cancel submitted![/green]')
        else:
            console.print('[red]Vote cancel failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')

# Add 'cancel' alias
val.add_command(val_vote_cancel_issue, name='cancel')
