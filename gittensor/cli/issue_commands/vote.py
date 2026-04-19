# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Validator vote commands for issue CLI

Commands:
    gitt vote solution
    gitt vote cancel
    gitt vote list
"""

import json as json_mod
import re

import click
from rich.panel import Panel
from rich.table import Table

from .help import StyledGroup
from .helpers import (
    _handle_command_error,
    _make_contract_client,
    _resolve_contract_and_network,
    console,
    emit_json,
    print_error,
    print_network_header,
    print_success,
    require_valid_issue_id,
    validate_issue_id,
    validate_ss58_address,
    with_cli_behavior_options,
    with_network_contract_options,
    with_wallet_options,
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


@click.group(name='vote', cls=StyledGroup)
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
@with_wallet_options()
@with_network_contract_options('Contract address (uses config if empty)')
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

    [dim]Arguments:
        ISSUE_ID: On-chain issue ID to vote on
        SOLVER_HOTKEY: SS58 address of the solver's hotkey
        SOLVER_COLDKEY: SS58 address of the solver's coldkey (payout destination)
        PR_NUMBER_OR_URL: PR number or full GitHub PR URL
    [/dim]

    [dim]Examples:
        $ gitt vote solution 1 5Hxxx... 5Hyyy... 123
        $ gitt vote solution 1 5Hxxx... 5Hyyy... https://github.com/.../pull/123
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    try:
        validate_issue_id(issue_id)
        validate_ss58_address(solver_hotkey, 'solver_hotkey')
        validate_ss58_address(solver_coldkey, 'solver_coldkey')
        pr_number = parse_pr_number(pr_number_or_url)
        if pr_number < 1:
            raise click.BadParameter(
                f'PR number must be positive (got {pr_number})',
                param_hint='pr_number_or_url',
            )
    except click.BadParameter:
        raise
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint='pr_number_or_url')

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
        with console.status('[bold cyan]Submitting vote...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            result = client.vote_solution(issue_id, solver_hotkey, solver_coldkey, pr_number, wallet)

        if result:
            print_success('Solution vote submitted!')
        else:
            print_error('Vote failed.')
    except Exception as e:
        _handle_command_error(e)


@vote.command('cancel')
@click.argument('issue_id', type=int)
@click.argument('reason', type=str)
@with_wallet_options()
@with_network_contract_options('Contract address (uses config if empty)')
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

    [dim]Arguments:
        ISSUE_ID: On-chain issue ID to cancel
        REASON: Reason for cancellation
    [/dim]

    [dim]Examples:
        $ gitt vote cancel 1 "External solution found"
        $ gitt vote cancel 42 "Issue invalid"
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    require_valid_issue_id(issue_id)

    print_network_header(network_name, contract_addr)

    console.print(
        Panel(
            f'[cyan]Issue ID:[/cyan] {issue_id}\n[cyan]Reason:[/cyan] {reason}',
            title='Vote Cancel Issue',
            border_style='yellow',
        )
    )

    try:
        with console.status('[bold cyan]Submitting cancel vote...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            result = client.vote_cancel_issue(issue_id, reason, wallet)

        if result:
            print_success('Cancel vote submitted!')
        else:
            print_error('Cancel vote failed.')
    except Exception as e:
        _handle_command_error(e)


@vote.command('list')
@with_cli_behavior_options(include_json=True)
@with_network_contract_options('Contract address (uses config if empty)')
def vote_list_validators(network: str, rpc_url: str, contract: str, as_json: bool):
    """List whitelisted validators and consensus threshold.

    [dim]Examples:
        $ gitt vote list
        $ gitt vote list --network test
        $ gitt vote list --json
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    if not as_json:
        print_network_header(network_name, contract_addr)

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Reading validator whitelist...', spinner='dots'):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            validators = client.get_validators()

        n = len(validators)
        required = (n // 2) + 1

        if as_json:
            emit_json(
                {
                    'validators': validators,
                    'count': n,
                    'consensus_threshold': required,
                }
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

    except Exception as e:
        _handle_command_error(e)
