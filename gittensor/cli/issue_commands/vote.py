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
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.panel import Panel
from rich.table import Table

from gittensor.cli.json_output import emit_json
from gittensor.validator import pat_storage
from gittensor.validator.issue_competitions.vote_decision import BountyVoteDecision, explain_bounty_vote

from .help import StyledGroup
from .helpers import (
    _handle_command_error,
    _make_contract_client,
    _resolve_contract_and_network,
    confirm_or_abort,
    console,
    err_console,
    handle_exception,
    loading_context,
    print_error,
    print_network_header,
    print_success,
    with_cli_behavior_options,
    with_network_contract_options,
    with_wallet_options,
)
from .types import CONTRACT_ISSUE, SS58


def _load_registered_miners_from_pat_file(pat_file: Path) -> Dict[str, str]:
    if not pat_file.exists():
        return {}

    data = json.loads(pat_file.read_text())
    if not isinstance(data, list):
        raise click.ClickException(f'Expected a list in PAT storage file: {pat_file}')

    registered: Dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        github_id = str(entry.get('github_id') or '')
        hotkey = str(entry.get('hotkey') or '')
        if github_id and github_id != '0' and hotkey:
            registered[github_id] = hotkey
    return registered


def _decision_to_dict(decision: BountyVoteDecision) -> Dict[str, Any]:
    return asdict(decision)


def _render_vote_explain_table(decisions: List[BountyVoteDecision]) -> None:
    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='cyan', justify='right')
    table.add_column('Repo')
    table.add_column('Issue', justify='right')
    table.add_column('Action', style='green')
    table.add_column('Reason')
    table.add_column('Solver')
    table.add_column('PR', justify='right')

    for decision in decisions:
        table.add_row(
            str(decision.issue_id),
            decision.repository_full_name,
            f'#{decision.issue_number}',
            decision.action,
            decision.reason,
            decision.solver_github_id or '-',
            f'#{decision.pr_number}' if decision.pr_number else '-',
        )

    console.print(table)


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
@click.argument('issue_id', type=CONTRACT_ISSUE)
@click.argument('solver_hotkey', type=SS58)
@click.argument('solver_coldkey', type=SS58)
@click.argument('pr_number_or_url', type=str)
@with_wallet_options()
@with_network_contract_options('Contract address (uses config if empty)')
@with_cli_behavior_options(include_yes=True)
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
    yes: bool,
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

    err_console.print(
        Panel(
            f'[cyan]Issue ID:[/cyan] {issue_id}\n'
            f'[cyan]Solver Hotkey:[/cyan] {solver_hotkey}\n'
            f'[cyan]Solver Coldkey:[/cyan] {solver_coldkey}\n'
            f'[cyan]PR Number:[/cyan] {pr_number}',
            title='Vote Solution',
            border_style='blue',
        )
    )

    if not confirm_or_abort(f'Vote that {solver_hotkey} solved issue {issue_id} via PR #{pr_number}?', yes):
        return

    try:
        with err_console.status('[bold cyan]Submitting vote...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            result = client.vote_solution(issue_id, solver_hotkey, solver_coldkey, pr_number, wallet)

        if result:
            print_success('Solution vote submitted!')
        else:
            print_error('Vote failed.')
            raise SystemExit(1)
    except Exception as e:
        _handle_command_error(e)


@vote.command('cancel')
@click.argument('issue_id', type=CONTRACT_ISSUE)
@click.argument('reason', type=str)
@with_wallet_options()
@with_network_contract_options('Contract address (uses config if empty)')
@with_cli_behavior_options(include_yes=True)
def val_vote_cancel_issue(
    issue_id: int,
    reason: str,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    yes: bool,
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

    print_network_header(network_name, contract_addr)

    err_console.print(
        Panel(
            f'[cyan]Issue ID:[/cyan] {issue_id}\n[cyan]Reason:[/cyan] {reason}',
            title='Vote Cancel Issue',
            border_style='yellow',
        )
    )

    if not confirm_or_abort(f'Vote to cancel issue {issue_id}?', yes):
        return

    try:
        with err_console.status('[bold cyan]Submitting cancel vote...', spinner='dots'):
            wallet, client = _make_contract_client(contract_addr, ws_endpoint, wallet_name, wallet_hotkey)
            result = client.vote_cancel_issue(issue_id, reason, wallet)

        if result:
            print_success('Cancel vote submitted!')
        else:
            print_error('Cancel vote failed.')
            raise SystemExit(1)
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

    print_network_header(network_name, contract_addr)

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with loading_context('Reading validator whitelist...', as_json):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            validators = client.get_validators()

        n = len(validators)
        required = (n // 2) + 1 if n > 0 else 0

        if as_json:
            emit_json(
                {
                    'success': True,
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
            err_console.print('[yellow]No validators whitelisted.[/yellow]')
            err_console.print('[dim]Add validators with: gitt admin add-vali <HOTKEY>[/dim]')

    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))


@vote.command('explain')
@click.option('--issue-id', type=CONTRACT_ISSUE, default=None, help='Explain one on-chain issue ID.')
@click.option(
    '--github-token',
    envvar='GITTENSOR_VALIDATOR_PAT',
    default='',
    help='GitHub token for closure lookup. Prefer GITTENSOR_VALIDATOR_PAT.',
)
@click.option(
    '--pat-file',
    type=click.Path(path_type=Path),
    default=pat_storage.PATS_FILE,
    show_default='data/miner_pats.json',
    help='Validator PAT storage file used to map GitHub IDs to miner hotkeys.',
)
@with_cli_behavior_options(include_json=True)
@with_network_contract_options('Contract address (uses config if empty)')
def vote_explain(
    issue_id: Optional[int],
    github_token: str,
    pat_file: Path,
    network: str,
    rpc_url: str,
    contract: str,
    as_json: bool,
):
    """Dry-run validator issue-bounty voting decisions.

    [dim]No votes are submitted. The command reads active contract issues,
    checks GitHub closure state, maps the solver GitHub ID through validator PAT
    storage, resolves the miner coldkey, and reports the action the validator
    would take.[/dim]
    """
    if not github_token:
        handle_exception(
            as_json=as_json,
            message='GITTENSOR_VALIDATOR_PAT is required for GitHub solver lookup.',
            error_type='missing_github_token',
        )

    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)
    if not as_json:
        print_network_header(network_name, contract_addr)

    try:
        import bittensor as bt

        from gittensor.utils.github_api_tools import check_github_issue_closed
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
            IssueStatus,
        )
        from gittensor.validator.utils.issue_competitions import get_miner_coldkey

        registered_miners = _load_registered_miners_from_pat_file(pat_file)

        with loading_context('Reading bounty issues from contract...', as_json):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            if issue_id is None:
                issues = client.get_issues_by_status(IssueStatus.ACTIVE)
            else:
                issue = client.get_issue(issue_id)
                if issue is None:
                    handle_exception(as_json=as_json, message=f'Issue {issue_id} not found.', error_type='not_found')
                assert issue is not None
                issues = [issue]

        decisions: List[BountyVoteDecision] = []
        with loading_context('Explaining vote decisions...', as_json):
            for issue in issues:
                github_state = check_github_issue_closed(
                    issue.repository_full_name,
                    issue.issue_number,
                    github_token,
                )
                decisions.append(
                    explain_bounty_vote(
                        issue=issue,
                        github_state=github_state,
                        registered_miners=registered_miners,
                        coldkey_lookup=lambda hotkey: get_miner_coldkey(hotkey, subtensor),
                    )
                )

        if as_json:
            emit_json(
                {
                    'success': True,
                    'dry_run': True,
                    'issue_count': len(issues),
                    'registered_miner_count': len(registered_miners),
                    'decisions': [_decision_to_dict(decision) for decision in decisions],
                }
            )
            return

        if not decisions:
            err_console.print('[yellow]No active issues found.[/yellow]')
            return

        _render_vote_explain_table(decisions)
        err_console.print('\n[dim]Dry run only; no votes were submitted.[/dim]')

    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
