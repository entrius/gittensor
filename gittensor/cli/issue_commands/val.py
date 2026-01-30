# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Validator subgroup commands for issue CLI.

Commands:
    gitt issue val propose-competition (alias: propose)
    gitt issue val vote-solution (alias: solution)
    gitt issue val vote-timeout (alias: timeout)
    gitt issue val vote-cancel-issue (alias: cancel)
"""

import click
from rich.console import Console

from .helpers import (
    console,
    get_contract_address,
    get_ws_endpoint,
)


@click.group(name='val')
def val():
    """Validator consensus operations.

    These commands are used by validators to manage the competition lifecycle.

    \b
    Commands:
        propose-competition  Propose a miner pair (alias: propose)
        vote-solution        Vote for a winner (alias: solution)
        vote-timeout         Vote to timeout competition (alias: timeout)
        vote-cancel-issue    Vote to cancel issue (alias: cancel)
    """
    pass


@val.command('propose-competition')
@click.argument('issue_id', type=int)
@click.argument('miner1_hotkey', type=str)
@click.argument('miner2_hotkey', type=str)
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
def val_propose_competition(
    issue_id: int,
    miner1_hotkey: str,
    miner2_hotkey: str,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Propose a miner pair for competition (or vote if same pair already proposed).

    \b
    Arguments:
        ISSUE_ID: Issue to start competition for
        MINER1_HOTKEY: First miner's hotkey
        MINER2_HOTKEY: Second miner's hotkey

    \b
    Examples:
        gitt issue val propose-competition 1 5Hxxx... 5Hyyy...
        gitt i val propose 42 <hotkey1> <hotkey2>
    """
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Proposing pair for issue {issue_id}...[/yellow]\n')
    console.print(f'  Miner 1: {miner1_hotkey}')
    console.print(f'  Miner 2: {miner2_hotkey}\n')

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

        result = client.propose_competition(issue_id, miner1_hotkey, miner2_hotkey, wallet)
        if result:
            console.print(f'[green]Competition proposal submitted![/green]')
        else:
            console.print('[red]Proposal failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')

# Add 'propose' alias
val.add_command(val_propose_competition, name='propose')


@val.command('vote-solution')
@click.argument('competition_id', type=int)
@click.argument('winner_hotkey', type=str)
@click.argument('winner_coldkey', type=str)
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
    competition_id: int,
    winner_hotkey: str,
    winner_coldkey: str,
    pr_url: str,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Vote for a solution winner in an active competition (triggers auto-payout).

    \b
    Arguments:
        COMPETITION_ID: Competition to vote on
        WINNER_HOTKEY: Winner's hotkey
        WINNER_COLDKEY: Winner's coldkey (payout destination)
        PR_URL: URL of the winning PR

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
    console.print(f'[yellow]Voting on solution for competition {competition_id}...[/yellow]\n')
    console.print(f'  Winner Hotkey:  {winner_hotkey}')
    console.print(f'  Winner Coldkey: {winner_coldkey}')
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

        result = client.vote_solution(competition_id, winner_hotkey, winner_coldkey, pr_url, wallet)
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


@val.command('vote-timeout')
@click.argument('competition_id', type=int)
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
def val_vote_timeout(
    competition_id: int,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Vote to timeout an expired competition.

    \b
    Arguments:
        COMPETITION_ID: Competition to timeout

    \b
    Examples:
        gitt issue val vote-timeout 1
        gitt i val timeout 42
    """
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Voting to timeout competition {competition_id}...[/yellow]\n')

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

        result = client.vote_timeout(competition_id, wallet)
        if result:
            console.print(f'[green]Vote timeout submitted![/green]')
        else:
            console.print('[red]Vote timeout failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')

# Add 'timeout' alias
val.add_command(val_vote_timeout, name='timeout')


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
    """Vote to cancel an issue (works on Registered, Active, or InCompetition).

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
