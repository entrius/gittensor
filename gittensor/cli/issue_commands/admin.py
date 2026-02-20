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
from rich.panel import Panel

from .helpers import (
    console,
    format_alpha,
    get_contract_address,
    print_error,
    print_network_header,
    print_success,
    print_warning,
    resolve_network,
    validate_issue_id,
    validate_ss58_address,
)


@click.group(name='admin')
def admin():
    """Owner-only administrative commands.

    These commands require the contract owner wallet.

    \b
    Commands:
        info           View contract configuration
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
        print_error('Contract address not configured.')
        return

    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        print_error(e.format_message())
        return

    print_network_header(network_name, ws_endpoint, contract_addr)

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Connecting...[/bold cyan]'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        # Show issue info before cancellation
        issue = client.get_issue(issue_id)
        if issue:
            console.print(
                Panel(
                    f'[cyan]Issue:[/cyan] {issue.repository_full_name}#{issue.issue_number}\n'
                    f'[cyan]Status:[/cyan] {issue.status.name}\n'
                    f'[cyan]Bounty:[/cyan] {format_alpha(issue.bounty_amount, 4)}',
                    title=f'Cancel Issue #{issue_id}',
                    border_style='yellow',
                )
            )

        with console.status('[bold cyan]Cancelling issue...[/bold cyan]'):
            result = client.cancel_issue(issue_id, wallet)

        if result:
            print_success(f'Issue {issue_id} cancelled successfully!')
        else:
            print_error('Cancellation failed.')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))


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
        print_error('Contract address not configured.')
        return

    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        print_error(e.format_message())
        return

    print_network_header(network_name, ws_endpoint, contract_addr)

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Connecting...[/bold cyan]'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        # Show issue info before payout
        issue = client.get_issue(issue_id)
        if issue:
            console.print(
                Panel(
                    f'[cyan]Issue:[/cyan] {issue.repository_full_name}#{issue.issue_number}\n'
                    f'[cyan]Status:[/cyan] {issue.status.name}\n'
                    f'[cyan]Bounty:[/cyan] {format_alpha(issue.bounty_amount, 4)}',
                    title=f'Payout Issue #{issue_id}',
                    border_style='green',
                )
            )

        with console.status('[bold cyan]Processing payout...[/bold cyan]'):
            result = client.payout_bounty(issue_id, wallet)

        if result:
            print_success(f'Payout successful! Amount: {format_alpha(result, 4)}')
        else:
            print_error('Payout failed.')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))


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
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    try:
        new_owner = validate_ss58_address(new_owner, 'new_owner')
    except click.BadParameter as e:
        print_error(e.format_message())
        return

    print_network_header(network_name, ws_endpoint, contract_addr)

    console.print(
        Panel(
            f'[cyan]New Owner:[/cyan] {new_owner}',
            title='Transfer Ownership',
            border_style='yellow',
        )
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Transferring ownership...[/bold cyan]'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            result = client.set_owner(new_owner, wallet)

        if result:
            print_success(f'Ownership transferred to {new_owner}!')
        else:
            print_error('Ownership transfer failed.')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))


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
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    try:
        new_treasury = validate_ss58_address(new_treasury, 'new_treasury')
    except click.BadParameter as e:
        print_error(e.format_message())
        return

    print_network_header(network_name, ws_endpoint, contract_addr)

    console.print(
        Panel(
            f'[cyan]New Treasury:[/cyan] {new_treasury}',
            title='Set Treasury Hotkey',
            border_style='yellow',
        )
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Updating treasury hotkey...[/bold cyan]'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            result = client.set_treasury_hotkey(new_treasury, wallet)

        if result:
            print_success(f'Treasury hotkey updated to {new_treasury}!')
            console.print(
                '[dim]Note: Issue bounty amounts have been reset. Run harvest to re-fund from new treasury.[/dim]'
            )
        else:
            print_error('Treasury hotkey update failed.')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))


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
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    try:
        hotkey = validate_ss58_address(hotkey, 'hotkey')
    except click.BadParameter as e:
        print_error(e.format_message())
        return

    print_network_header(network_name, ws_endpoint, contract_addr)

    console.print(
        Panel(
            f'[cyan]Validator Hotkey:[/cyan] {hotkey}',
            title='Add Validator',
            border_style='blue',
        )
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Adding validator...[/bold cyan]'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            result = client.add_validator(hotkey, wallet)

        if result:
            print_success(f'Validator {hotkey} added to whitelist!')
        else:
            print_error('Failed to add validator.')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  \u2022 Caller is not the contract owner')
            console.print('  \u2022 Validator is already whitelisted')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))


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
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    try:
        hotkey = validate_ss58_address(hotkey, 'hotkey')
    except click.BadParameter as e:
        print_error(e.format_message())
        return

    print_network_header(network_name, ws_endpoint, contract_addr)

    console.print(
        Panel(
            f'[cyan]Validator Hotkey:[/cyan] {hotkey}',
            title='Remove Validator',
            border_style='red',
        )
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Removing validator...[/bold cyan]'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            result = client.remove_validator(hotkey, wallet)

        if result:
            print_success(f'Validator {hotkey} removed from whitelist!')
        else:
            print_error('Failed to remove validator.')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  \u2022 Caller is not the contract owner')
            console.print('  \u2022 Validator is not in the whitelist')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))
