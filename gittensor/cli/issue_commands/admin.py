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
    print_error,
    print_success,
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
    if issue_id < 1 or issue_id >= 1_000_000:
        print_error('Issue ID must be between 1 and 999,999.')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
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
    if issue_id < 1 or issue_id >= 1_000_000:
        print_error('Issue ID must be between 1 and 999,999.')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
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
            print_success(f'Payout successful! Amount: {format_alpha(result)}')
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
    if not validate_ss58(new_owner):
        print_error(f'Invalid SS58 address for new owner: {new_owner}')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
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
    if not validate_ss58(new_treasury):
        print_error(f'Invalid SS58 address for new treasury: {new_treasury}')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
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
    if not validate_ss58(hotkey):
        print_error(f'Invalid SS58 address for validator hotkey: {hotkey}')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
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
            print_success(f'Validator {hotkey} added to whitelist!')
        else:
            print_error('Failed to add validator.')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  - Caller is not the contract owner')
            console.print('  - Validator is already whitelisted')
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
    if not validate_ss58(hotkey):
        print_error(f'Invalid SS58 address for validator hotkey: {hotkey}')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
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
            print_success(f'Validator {hotkey} removed from whitelist!')
        else:
            print_error('Failed to remove validator.')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  - Caller is not the contract owner')
            console.print('  - Validator is not in the whitelist')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))
