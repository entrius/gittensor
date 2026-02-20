# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Validator vote commands for issue CLI

Commands:
    gitt vote solution
    gitt vote cancel
    gitt vote list
"""

import re

import click
from rich.panel import Panel
from rich.table import Table

from .helpers import (
    console,
    get_contract_address,
    output_json,
    print_error,
    print_network_header,
    print_success,
    print_warning,
    resolve_network,
    validate_issue_id,
    validate_ss58_address,
)


def parse_pr_number(pr_input: str) -> int:
    """
    Parse PR number from either a number or a URL.

    Args:
        pr_input: Either a PR number as string, or a full GitHub PR URL

    Returns:
        PR number as integer

    Examples:
        parse_pr_number("123") -> 123
        parse_pr_number("https://github.com/owner/repo/pull/123") -> 123
    """
    # First try as plain number
    if pr_input.isdigit():
        return int(pr_input)

    # Try to extract from URL
    match = re.search(r'/pull/(\d+)', pr_input)
    if match:
        return int(match.group(1))

    # Invalid input
    raise ValueError(f'Cannot parse PR number from: {pr_input}')


@click.group(name='vote')
def vote():
    """Validator consensus operations.

    These commands are used by validators to manage issue bounty payouts.

    \b
    Commands:
        solution   Vote for a solver on an active issue
        cancel     Vote to cancel an issue
        list       List whitelisted validators
    """
    pass


@vote.command('solution')
@click.argument('issue_id', type=int)
@click.argument('solver_hotkey', type=str)
@click.argument('solver_coldkey', type=str)
@click.argument('pr_number_or_url', type=str)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
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
def val_vote_solution(
    issue_id: int,
    solver_hotkey: str,
    solver_coldkey: str,
    pr_number_or_url: str,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
):
    """Vote for a solution on an active issue (triggers auto-payout on consensus).

    \b
    Arguments:
        ISSUE_ID: Issue to vote on
        SOLVER_HOTKEY: Solver's hotkey (SS58 address)
        SOLVER_COLDKEY: Solver's coldkey (SS58 address, payout destination)
        PR_NUMBER_OR_URL: PR number or full URL (e.g., 123 or https://github.com/.../pull/123)

    \b
    Examples:
        gitt vote solution 1 5Hxxx... 5Hyyy... 123
        gitt vote solution 1 5Hxxx... 5Hyyy... https://github.com/.../pull/123
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    # --- Validate inputs ---

    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        print_error(e.format_message())
        return

    try:
        solver_hotkey = validate_ss58_address(solver_hotkey, 'solver_hotkey')
        solver_coldkey = validate_ss58_address(solver_coldkey, 'solver_coldkey')
    except click.BadParameter as e:
        print_error(e.format_message())
        return

    try:
        pr_number = parse_pr_number(pr_number_or_url)
    except ValueError as e:
        print_error(str(e))
        return

    # --- Display vote summary ---

    print_network_header(network_name, ws_endpoint, contract_addr)

    console.print(
        Panel(
            f'[cyan]Issue ID:[/cyan] {issue_id}\n'
            f'[cyan]Solver Hotkey:[/cyan] {solver_hotkey}\n'
            f'[cyan]Solver Coldkey:[/cyan] {solver_coldkey}\n'
            f'[cyan]PR Number:[/cyan] #{pr_number}',
            title='Solution Vote',
            border_style='blue',
        )
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Submitting solution vote...[/bold cyan]'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            result = client.vote_solution(issue_id, solver_hotkey, solver_coldkey, pr_number, wallet)

        if result:
            print_success('Solution vote submitted!')
        else:
            print_error('Vote failed.')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))


@vote.command('cancel')
@click.argument('issue_id', type=int)
@click.argument('reason', type=str)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
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
def val_vote_cancel_issue(
    issue_id: int,
    reason: str,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
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
        gitt vote cancel 1 "External solution found"
        gitt vote cancel 42 "Issue invalid"
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

    console.print(
        Panel(
            f'[cyan]Issue ID:[/cyan] {issue_id}\n'
            f'[cyan]Reason:[/cyan] {reason}',
            title='Cancel Vote',
            border_style='yellow',
        )
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Submitting cancel vote...[/bold cyan]'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            result = client.vote_cancel_issue(issue_id, reason, wallet)

        if result:
            print_success('Cancel vote submitted!')
        else:
            print_error('Cancel vote failed.')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))


@vote.command('list')
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
@click.option('--json', 'output_json_flag', is_flag=True, help='Output as JSON')
def vote_list_validators(network: str, rpc_url: str, contract: str, output_json_flag: bool):
    """List whitelisted validators and consensus threshold.

    Shows all validator hotkeys that are authorized to vote on
    solutions and issue cancellations.

    \b
    Examples:
        gitt vote list
        gitt vote list --network test
        gitt vote list --json
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    if not output_json_flag:
        print_network_header(network_name, ws_endpoint, contract_addr)

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Reading validators...[/bold cyan]'):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            validators = client.get_validators()

        n = len(validators)
        required = (n // 2) + 1

        if output_json_flag:
            output_json({
                'validators': validators,
                'count': n,
                'consensus_threshold': required,
            })
            return

        if validators:
            table = Table(show_header=True, header_style='bold magenta')
            table.add_column('#', style='dim', justify='right')
            table.add_column('Validator Hotkey', style='cyan')

            for i, v in enumerate(validators, 1):
                table.add_row(str(i), v)

            console.print(table)
            console.print(f'\n[green]Validators:[/green] {n}')
            console.print(f'[green]Consensus threshold:[/green] {required} of {n} votes required')
        else:
            console.print(
                Panel(
                    '[yellow]No validators whitelisted.[/yellow]\n\n'
                    '[dim]Add validators with:[/dim]\n'
                    '  gitt admin add-vali <HOTKEY>',
                    title='Validators',
                    border_style='dim',
                )
            )

    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))
