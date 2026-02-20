# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Validator vote commands for issue CLI

Commands:
    gitt vote solution
    gitt vote cancel
    gitt vote list
"""

import json
import re

import click
from rich.panel import Panel
from rich.table import Table

from .helpers import (
    console,
    get_contract_address,
    print_error,
    print_network_header,
    print_success,
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
    # Validate inputs
    validate_issue_id(issue_id, 'issue ID')
    validate_ss58_address(solver_hotkey, 'solver hotkey')
    validate_ss58_address(solver_coldkey, 'solver coldkey')

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured.')

    try:
        pr_number = parse_pr_number(pr_number_or_url)
    except ValueError as e:
        raise click.ClickException(str(e))

    if pr_number < 1:
        raise click.ClickException(f'PR number must be >= 1 (got {pr_number})')

    print_network_header(network_name, contract_addr)
    console.print(
        Panel(
            f'[cyan]Issue ID:[/cyan] {issue_id}\n'
            f'[cyan]Solver Hotkey:[/cyan] {solver_hotkey}\n'
            f'[cyan]Solver Coldkey:[/cyan] {solver_coldkey}\n'
            f'[cyan]PR Number:[/cyan] {pr_number}',
            title='Vote Solution',
            border_style='blue',
        )
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)

        with console.status('Connecting to subtensor...', spinner='dots'):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        with console.status('Submitting solution vote...', spinner='dots'):
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
    # Validate inputs
    validate_issue_id(issue_id, 'issue ID')

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured.')

    print_network_header(network_name, contract_addr)
    console.print(
        Panel(
            f'[cyan]Issue ID:[/cyan] {issue_id}\n[cyan]Reason:[/cyan] {reason}',
            title='Vote Cancel Issue',
            border_style='blue',
        )
    )

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)

        with console.status('Connecting to subtensor...', spinner='dots'):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        with console.status('Submitting cancel vote...', spinner='dots'):
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
@click.option('--json', 'output_json', is_flag=True, help='Output raw JSON for scripting')
def vote_list_validators(network: str, rpc_url: str, contract: str, output_json: bool):
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
        raise click.ClickException('Contract address not configured.')

    if not output_json:
        print_network_header(network_name, contract_addr)
        console.print()

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('Connecting to subtensor...', spinner='dots'):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        with console.status('Reading validators...', spinner='dots'):
            validators = client.get_validators()
        n = len(validators)
        required = (n // 2) + 1

        if output_json:
            click.echo(
                json.dumps(
                    {
                        'validators': validators,
                        'count': n,
                        'consensus_threshold': required,
                    },
                    indent=2,
                )
            )
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
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))
