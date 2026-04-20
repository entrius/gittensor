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

from .help import StyledGroup
from .helpers import (
    _handle_command_error,
    _make_contract_client,
    _resolve_contract_and_network,
    console,
    format_alpha,
    print_error,
    print_network_header,
    print_success,
    require_valid_issue_id,
    require_valid_ss58,
    with_network_contract_options,
    with_wallet_options,
)


@click.group(name='admin', cls=StyledGroup)
def admin():
    """Owner-only administrative commands.

    These commands require the contract owner wallet.
    """
    pass


@admin.command('cancel-issue')
@click.argument('issue_id', type=int)
@with_wallet_options()
@with_network_contract_options('Contract address (uses config if empty)')
def admin_cancel(issue_id: int, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Cancel an issue (owner only).

    [dim]Immediately cancels an issue without validator consensus. Bounty funds are returned to the alpha pool.[/dim]

    [dim]Arguments:
        ISSUE_ID: On-chain issue ID to cancel
    [/dim]

    [dim]Examples:
        $ gitt admin cancel-issue 1
        $ gitt a cancel-issue 5 --network test
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    require_valid_issue_id(issue_id)

    print_network_header(network_name, contract_addr)

    try:
        with console.status('[bold cyan]Connecting and reading issue...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            issue = client.get_issue(issue_id)

        if not issue:
            print_error(f'Issue {issue_id} not found on contract.')
            return

        console.print(
            Panel(
                f'[cyan]Issue:[/cyan] {issue.repository_full_name}#{issue.issue_number}\n'
                f'[cyan]Status:[/cyan] {issue.status.name}\n'
                f'[cyan]Bounty:[/cyan] {format_alpha(issue.bounty_amount, 4)} ALPHA',
                title=f'Cancel Issue #{issue_id}',
                border_style='yellow',
            )
        )

        with console.status('[bold cyan]Submitting cancellation...', spinner='dots'):
            result = client.cancel_issue(issue_id, wallet)

        if result:
            print_success(f'Issue {issue_id} cancelled successfully!')
        else:
            print_error('Cancellation failed.')
    except Exception as e:
        _handle_command_error(e)


@admin.command('payout-issue')
@click.argument('issue_id', type=int)
@with_wallet_options()
@with_network_contract_options('Contract address (uses config if empty)')
def admin_payout(issue_id: int, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Manual payout fallback (owner only).

    [dim]Pays out a completed issue bounty to the solver.
    The solver address is determined by validator consensus and stored in the contract.[/dim]

    [dim]Arguments:
        ISSUE_ID: On-chain ID of a completed issue
    [/dim]

    [dim]Examples:
        $ gitt admin payout-issue 1
        $ gitt a payout-issue 3 --network test
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    require_valid_issue_id(issue_id)

    print_network_header(network_name, contract_addr)

    try:
        with console.status('[bold cyan]Connecting and reading issue...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            issue = client.get_issue(issue_id)

        if not issue:
            print_error(f'Issue {issue_id} not found on contract.')
            return

        console.print(
            Panel(
                f'[cyan]Issue:[/cyan] {issue.repository_full_name}#{issue.issue_number}\n'
                f'[cyan]Status:[/cyan] {issue.status.name}\n'
                f'[cyan]Bounty:[/cyan] {format_alpha(issue.bounty_amount, 4)} ALPHA',
                title=f'Payout Issue #{issue_id}',
                border_style='green',
            )
        )

        with console.status('[bold cyan]Submitting payout...', spinner='dots'):
            result = client.payout_bounty(issue_id, wallet)

        if result:
            print_success(f'Payout successful! Amount: {format_alpha(result, 4)} ALPHA')
        else:
            print_error('Payout failed.')
    except Exception as e:
        _handle_command_error(e)


@admin.command('set-owner')
@click.argument('new_owner', type=str)
@with_wallet_options()
@with_network_contract_options('Contract address')
def admin_set_owner(new_owner: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Transfer contract ownership (owner only).

    [dim]Arguments:
        NEW_OWNER: SS58 address of the new owner
    [/dim]

    [dim]Examples:
        $ gitt admin set-owner 5Hxxx...
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    require_valid_ss58(new_owner, 'new_owner')

    print_network_header(network_name, contract_addr)

    console.print(
        Panel(
            f'[cyan]New Owner:[/cyan] {new_owner}',
            title='Transfer Ownership',
            border_style='red',
        )
    )

    try:
        with console.status('[bold cyan]Transferring ownership...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            result = client.set_owner(new_owner, wallet)

        if result:
            print_success(f'Ownership transferred to {new_owner}!')
        else:
            print_error('Ownership transfer failed.')
    except Exception as e:
        _handle_command_error(e)


@admin.command('set-treasury')
@click.argument('new_treasury', type=str)
@with_wallet_options()
@with_network_contract_options('Contract address')
def admin_set_treasury(
    new_treasury: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str
):
    """Change treasury hotkey (owner only).

    [dim]The treasury hotkey receives staking emissions that fund bounty payouts. Changing the treasury resets all
    Active/Registered issue bounty amounts to 0 (they will be re-funded on the next harvest from the new treasury).[/dim]

    [dim]Arguments:
        NEW_TREASURY: SS58 address of the new treasury hotkey
    [/dim]

    [dim]Examples:
        $ gitt admin set-treasury 5Hxxx...
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    require_valid_ss58(new_treasury, 'new_treasury')

    print_network_header(network_name, contract_addr)

    console.print(
        Panel(
            f'[cyan]New Treasury:[/cyan] {new_treasury}',
            title='Change Treasury Hotkey',
            border_style='yellow',
        )
    )

    try:
        with console.status('[bold cyan]Updating treasury hotkey...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            result = client.set_treasury_hotkey(new_treasury, wallet)

        if result:
            print_success(f'Treasury hotkey updated to {new_treasury}!')
            console.print(
                '[dim]Note: Issue bounty amounts have been reset. Run harvest to re-fund from new treasury.[/dim]'
            )
        else:
            print_error('Treasury hotkey update failed.')
    except Exception as e:
        _handle_command_error(e)


@admin.command('add-vali')
@click.argument('hotkey', type=str)
@with_wallet_options()
@with_network_contract_options('Contract address')
def admin_add_validator(hotkey: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Add a validator to the voting whitelist (owner only).

    [dim]Whitelisted validators can vote on solutions and issue cancellations.
    The consensus threshold adjusts automatically to a simple majority after 3 validators are added.[/dim]

    [dim]Arguments:
        HOTKEY: SS58 address of the validator hotkey to whitelist
    [/dim]

    [dim]Examples:
        $ gitt admin add-vali 5Hxxx...
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    require_valid_ss58(hotkey, 'hotkey')

    print_network_header(network_name, contract_addr)

    console.print(
        Panel(
            f'[cyan]Validator Hotkey:[/cyan] {hotkey}',
            title='Add Validator',
            border_style='blue',
        )
    )

    try:
        with console.status('[bold cyan]Adding validator...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            result = client.add_validator(hotkey, wallet)

        if result:
            print_success(f'Validator {hotkey} added to whitelist!')
        else:
            print_error('Failed to add validator.')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  \u2022 Caller is not the contract owner')
            console.print('  \u2022 Validator is already whitelisted')
    except Exception as e:
        _handle_command_error(e)


@admin.command('remove-vali')
@click.argument('hotkey', type=str)
@with_wallet_options()
@with_network_contract_options('Contract address')
def admin_remove_validator(
    hotkey: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str
):
    """Remove a validator from the voting whitelist (owner only).

    [dim]The consensus threshold adjusts automatically after removal.[/dim]

    [dim]Arguments:
        HOTKEY: SS58 address of the validator hotkey to remove
    [/dim]

    [dim]Examples:
        $ gitt admin remove-vali 5Hxxx...
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    require_valid_ss58(hotkey, 'hotkey')

    print_network_header(network_name, contract_addr)

    console.print(
        Panel(
            f'[cyan]Validator Hotkey:[/cyan] {hotkey}',
            title='Remove Validator',
            border_style='red',
        )
    )

    try:
        with console.status('[bold cyan]Removing validator...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            result = client.remove_validator(hotkey, wallet)

        if result:
            print_success(f'Validator {hotkey} removed from whitelist!')
        else:
            print_error('Failed to remove validator.')
            console.print('[yellow]Possible reasons:[/yellow]')
            console.print('  \u2022 Caller is not the contract owner')
            console.print('  \u2022 Validator is not in the whitelist')
    except Exception as e:
        _handle_command_error(e)
