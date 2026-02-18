# The MIT License (MIT)
# Copyright (c) 2025 Entrius

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
    resolve_network,
    validate_issue_id,
    validate_ss58_address,
)


def _print_admin_action_panel(title: str, network_name: str, contract_addr: str, fields: list[tuple[str, str]]) -> None:
    """Render a consistent transaction summary panel for admin write commands."""
    lines = [f'[cyan]Network:[/cyan] {network_name}', f'[cyan]Contract:[/cyan] {contract_addr}']
    for key, value in fields:
        lines.append(f'[cyan]{key}:[/cyan] {value}')
    console.print(Panel('\n'.join(lines), title=title, border_style='blue'))


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
    help='Contract address (uses configured/default value if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (owner wallet)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name for signing',
)
def admin_cancel(issue_id: int, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Cancel an issue (owner only).

    Immediately cancels an issue without requiring validator consensus.
    Bounty funds are returned to the alpha pool.

    \b
    Arguments:
        ISSUE_ID: Issue to cancel

    \b
    Examples:
        gitt admin cancel-issue 7
        gitt a cancel-issue 7 --network test
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        console.print(f'[red]Error: {e.format_message()}[/red]')
        return

    _print_admin_action_panel(
        title='Admin Cancel Issue',
        network_name=network_name,
        contract_addr=contract_addr,
        fields=[('Issue ID', str(issue_id)), ('RPC', ws_endpoint)],
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold yellow]Connecting to subtensor...'):
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
            console.print(f'  Bounty: {format_alpha(issue.bounty_amount, 4)} ALPHA\n')

        with console.status('[bold yellow]Cancelling issue...'):
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
    help='Contract address (uses configured/default value if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (owner wallet)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name for signing',
)
def admin_payout(issue_id: int, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Manual payout fallback (owner only).

    Pays out a completed issue bounty to the solver. The solver address
    is determined by validator consensus and stored in the contract.

    \b
    Arguments:
        ISSUE_ID: Completed issue ID

    \b
    Examples:
        gitt admin payout-issue 7
        gitt a payout-issue 7 --network finney
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        console.print(f'[red]Error: {e.format_message()}[/red]')
        return

    _print_admin_action_panel(
        title='Admin Payout Issue',
        network_name=network_name,
        contract_addr=contract_addr,
        fields=[('Issue ID', str(issue_id)), ('RPC', ws_endpoint)],
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold yellow]Connecting to subtensor...'):
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
            console.print(f'  Bounty: {format_alpha(issue.bounty_amount, 4)} ALPHA\n')

        with console.status('[bold yellow]Processing payout...'):
            result = client.payout_bounty(issue_id, wallet)
        if result:
            console.print(f'[green]Payout successful! Amount: {format_alpha(result, 4)} ALPHA[/green]')
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
    help='Contract address (uses configured/default value if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (owner wallet)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name for signing',
)
def admin_set_owner(new_owner: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Transfer contract ownership (owner only).

    \b
    Arguments:
        NEW_OWNER: SS58 address of the new owner

    \b
    Examples:
        gitt admin set-owner 5F...
        gitt a set-owner 5F... --network test
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    try:
        validate_ss58_address(new_owner, 'New owner')
    except click.BadParameter as e:
        console.print(f'[red]Error: {e.format_message()}[/red]')
        return

    _print_admin_action_panel(
        title='Admin Set Owner',
        network_name=network_name,
        contract_addr=contract_addr,
        fields=[('New owner', new_owner), ('RPC', ws_endpoint)],
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold yellow]Connecting to subtensor...'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        with console.status('[bold yellow]Transferring ownership...'):
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
    help='Contract address (uses configured/default value if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (owner wallet)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name for signing',
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

    \b
    Examples:
        gitt admin set-treasury 5F...
        gitt a set-treasury 5F... --network test
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    try:
        validate_ss58_address(new_treasury, 'New treasury')
    except click.BadParameter as e:
        console.print(f'[red]Error: {e.format_message()}[/red]')
        return

    _print_admin_action_panel(
        title='Admin Set Treasury',
        network_name=network_name,
        contract_addr=contract_addr,
        fields=[('New treasury', new_treasury), ('RPC', ws_endpoint)],
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold yellow]Connecting to subtensor...'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        with console.status('[bold yellow]Updating treasury...'):
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
    help='Contract address (uses configured/default value if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (owner wallet)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name for signing',
)
def admin_add_validator(hotkey: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str):
    """Add a validator to the voting whitelist (owner only).

    Whitelisted validators can vote on solutions and issue cancellations.
    The consensus threshold adjusts automatically: simple majority after
    3 validators are added.

    \b
    Arguments:
        HOTKEY: SS58 address of the validator hotkey to whitelist

    \b
    Examples:
        gitt admin add-vali 5F...
        gitt a add-vali 5F... --network finney
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    try:
        validate_ss58_address(hotkey, 'Validator hotkey')
    except click.BadParameter as e:
        console.print(f'[red]Error: {e.format_message()}[/red]')
        return

    _print_admin_action_panel(
        title='Admin Add Validator',
        network_name=network_name,
        contract_addr=contract_addr,
        fields=[('Validator hotkey', hotkey), ('RPC', ws_endpoint)],
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold yellow]Connecting to subtensor...'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        with console.status('[bold yellow]Adding validator...'):
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
    help='Contract address (uses configured/default value if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name (owner wallet)',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name for signing',
)
def admin_remove_validator(
    hotkey: str, network: str, rpc_url: str, contract: str, wallet_name: str, wallet_hotkey: str
):
    """Remove a validator from the voting whitelist (owner only).

    The consensus threshold adjusts automatically after removal.

    \b
    Arguments:
        HOTKEY: SS58 address of the validator hotkey to remove

    \b
    Examples:
        gitt admin remove-vali 5F...
        gitt a remove-vali 5F... --network finney
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    try:
        validate_ss58_address(hotkey, 'Validator hotkey')
    except click.BadParameter as e:
        console.print(f'[red]Error: {e.format_message()}[/red]')
        return

    _print_admin_action_panel(
        title='Admin Remove Validator',
        network_name=network_name,
        contract_addr=contract_addr,
        fields=[('Validator hotkey', hotkey), ('RPC', ws_endpoint)],
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold yellow]Connecting to subtensor...'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        with console.status('[bold yellow]Removing validator...'):
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
