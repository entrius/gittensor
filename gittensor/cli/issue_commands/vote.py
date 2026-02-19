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
from rich.table import Table

from .helpers import (
    console,
    get_contract_address,
    print_error,
    print_success,
    resolve_network,
    validate_ss58,
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
        SOLVER_HOTKEY: Solver's hotkey
        SOLVER_COLDKEY: Solver's coldkey (payout destination)
        PR_NUMBER_OR_URL: PR number or full URL (e.g., 123 or https://github.com/.../pull/123)

    \b
    Examples:
        gitt vote solution 1 5Hxxx... 5Hyyy... 123
        gitt vote solution 1 5Hxxx... 5Hyyy... https://github.com/.../pull/123
    """
    if issue_id < 1 or issue_id >= 1_000_000:
        print_error('Issue ID must be between 1 and 999,999.')
        return

    if not validate_ss58(solver_hotkey):
        print_error(f'Invalid SS58 address for solver hotkey: {solver_hotkey}')
        return

    if not validate_ss58(solver_coldkey):
        print_error(f'Invalid SS58 address for solver coldkey: {solver_coldkey}')
        return

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    try:
        pr_number = parse_pr_number(pr_number_or_url)
    except ValueError as e:
        print_error(str(e))
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Voting on solution for issue {issue_id}...[/yellow]\n')
    console.print(f'  Solver Hotkey:  {solver_hotkey}')
    console.print(f'  Solver Coldkey: {solver_coldkey}')
    console.print(f'  PR Number: {pr_number}\n')

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

        with console.status('[yellow]Submitting solution vote...[/yellow]'):
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
    console.print(f'[yellow]Voting to cancel issue {issue_id}...[/yellow]\n')
    console.print(f'  Reason: {reason}\n')

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

        with console.status('[yellow]Submitting cancellation vote...[/yellow]'):
            result = client.vote_cancel_issue(issue_id, reason, wallet)

        if result:
            print_success('Vote cancel submitted!')
        else:
            print_error('Vote cancel failed.')
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
@click.option('--json', 'json_output', is_flag=True, help='Output in JSON format')
def vote_list_validators(network: str, rpc_url: str, contract: str, json_output: bool):
    """List whitelisted validators and consensus threshold.

    Shows all validator hotkeys that are authorized to vote on
    solutions and issue cancellations.

    \b
    Examples:
        gitt vote list
        gitt vote list --network test
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        if json_output:
            import json as json_lib
            print(json_lib.dumps({'status': 'error', 'message': 'Contract address not configured'}))
        else:
            print_error('Contract address not configured.')
        return

    if not json_output:
        console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
        console.print(f'[dim]Contract: {contract_addr}[/dim]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[yellow]Fetching validators...[/yellow]', disable=json_output):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            validators = client.get_validators()

        n = len(validators)
        required = (n // 2) + 1

        if json_output:
            import json as json_lib
            print(json_lib.dumps({'validators': validators, 'required_votes': required, 'total_validators': n}))
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
            console.print('[yellow]No validators whitelisted.[/yellow]')
            console.print('[dim]Add validators with: gitt admin add-vali <HOTKEY>[/dim]')

    except ImportError as e:
        if json_output:
            import json as json_lib
            print(json_lib.dumps({'status': 'error', 'message': f'Missing dependency - {e}'}))
        else:
            print_error(f'Missing dependency - {e}')
    except Exception as e:
        if json_output:
            import json as json_lib
            print(json_lib.dumps({'status': 'error', 'message': str(e)}))
        else:
            print_error(str(e))
