# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Admin subgroup commands for issue CLI

Commands:
    gitt admin cancel-issue (alias: a cancel-issue)
    gitt admin payout-issue (alias: a payout-issue)
    gitt admin set-owner (alias: a set-owner)
    gitt admin set-treasury (alias: a set-treasury)
    gitt admin add-vali (alias: a add-vali)
    gitt admin remove-vali (alias: a remove-vali)
"""

import click

from .helpers import (
    console,
    format_alpha,
    get_contract_address,
    resolve_network,
    validate_ss58,
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
        add-vali       Add a validator to the whitelist
        remove-vali    Remove a validator from the whitelist
    """
    pass


@admin.command('cancel-issue')
@click.argument('issue_id', type=int)
@click.option(
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (must be owner)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
def admin_cancel(issue_id: int, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Cancel an issue (owner only).

    Immediately cancels an issue without requiring validator consensus.
    Bounty funds are returned to the alpha pool.

    \b
    Arguments:
        ISSUE_ID: Issue to cancel
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Cancelling issue {issue_id}...[/yellow]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        # Show issue info before cancellation
        with console.status('[yellow]Fetching issue info...[/yellow]'):
            issue = client.get_issue(issue_id)

        if issue:
            console.print(f'  Issue: {issue.repository_full_name}#{issue.issue_number}')
            console.print(f'  Status: {issue.status.name}')
            console.print(f'  Bounty: {format_alpha(issue.bounty_amount)}\n')

        with console.status('[yellow]Submitting cancellation transaction...[/yellow]'):
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
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (must be owner)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
def admin_payout(issue_id: int, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Manual payout fallback (owner only).

    Pays out a completed issue bounty to the solver. The solver address
    is determined by validator consensus and stored in the contract.

    \b
    Arguments:
        ISSUE_ID: Completed issue ID
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Manual payout for issue {issue_id}...[/yellow]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        # Show issue info before payout
        with console.status('[yellow]Fetching issue info...[/yellow]'):
            issue = client.get_issue(issue_id)

        if issue:
            console.print(f'  Issue: {issue.repository_full_name}#{issue.issue_number}')
            console.print(f'  Status: {issue.status.name}')
            console.print(f'  Bounty: {format_alpha(issue.bounty_amount)}\n')

        with console.status('[yellow]Submitting payout transaction...[/yellow]'):
            result = client.payout_bounty(issue_id, wallet)

        if result:
            console.print(f'[green]Payout successful! Amount: {format_alpha(result)}[/green]')
        else:
            console.print('[red]Payout failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('set-owner')
@click.argument('new_owner', type=str)
@click.option(
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (must be current owner)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
def admin_set_owner(new_owner: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Transfer contract ownership (owner only).

    \b
    Arguments:
        NEW_OWNER: SS58 address of the new owner
    """
    if not validate_ss58(new_owner):
        console.print(f'[red]Error: Invalid SS58 address for new owner: {new_owner}[/red]')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Transferring ownership to {new_owner}...[/yellow]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        with console.status('[yellow]Submitting ownership transfer transaction...[/yellow]'):
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
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (must be owner)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
def admin_set_treasury(
    new_treasury: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str
):
    """Change treasury hotkey (owner only).

    The treasury hotkey receives staking emissions that fund bounty payouts.
    Changing the treasury resets all Active/Registered issue bounty amounts
    to 0 (they will be re-funded on next harvest from the new treasury).

    \b
    Arguments:
        NEW_TREASURY: SS58 address of the new treasury hotkey
    """
    if not validate_ss58(new_treasury):
        console.print(f'[red]Error: Invalid SS58 address for new treasury: {new_treasury}[/red]')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Setting treasury hotkey to {new_treasury}...[/yellow]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        with console.status('[yellow]Submitting treasury update transaction...[/yellow]'):
            result = client.set_treasury_hotkey(new_treasury, wallet)

        if result:
            console.print(f'[green]Treasury hotkey updated to {new_treasury}![/green]')
            console.print(
                '[dim]Note: Issue bounty amounts have been reset. Run harvest to re-fund from new treasury.[/dim]'
            )
        else:
            console.print('[red]Treasury hotkey update failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('add-vali')
@click.argument('hotkey', type=str)
@click.option(
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (must be owner)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
def admin_add_validator(hotkey: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Add a validator to the voting whitelist (owner only).

    Whitelisted validators can vote on solutions and issue cancellations.
    The consensus threshold adjusts automatically: simple majority after
    3 validators are added.

    \b
    Arguments:
        HOTKEY: SS58 address of the validator hotkey to whitelist
    """
    if not validate_ss58(hotkey):
        console.print(f'[red]Error: Invalid SS58 address for validator hotkey: {hotkey}[/red]')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Adding validator {hotkey}...[/yellow]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        with console.status(f'[yellow]Adding validator {hotkey[:8]}... to whitelist...[/yellow]'):
            result = client.add_validator(hotkey, wallet)

        if result:
            console.print(f'[green]Validator {hotkey} added to whitelist![/green]')
        else:
            console.print('[red]Failed to add validator.[/red]')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  - Caller is not the contract owner')
            console.print('  - Validator is already whitelisted')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('remove-vali')
@click.argument('hotkey', type=str)
@click.option(
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (must be owner)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
def admin_remove_validator(
    hotkey: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str
):
    """Remove a validator from the voting whitelist (owner only).

    The consensus threshold adjusts automatically after removal.

    \b
    Arguments:
        HOTKEY: SS58 address of the validator hotkey to remove
    """
    if not validate_ss58(hotkey):
        console.print(f'[red]Error: Invalid SS58 address for validator hotkey: {hotkey}[/red]')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Removing validator {hotkey}...[/yellow]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        with console.status(f'[yellow]Removing validator {hotkey[:8]}... from whitelist...[/yellow]'):
            result = client.remove_validator(hotkey, wallet)

        if result:
            console.print(f'[green]Validator {hotkey} removed from whitelist![/green]')
        else:
            console.print('[red]Failed to remove validator.[/red]')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  - Caller is not the contract owner')
            console.print('  - Validator is not in the whitelist')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')
