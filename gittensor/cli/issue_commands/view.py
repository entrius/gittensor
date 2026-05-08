# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Read-only issue commands

Commands:
    gitt issues list [--id <ID>]
    gitt issues bounty-pool
    gitt issues pending-harvest
    gitt admin info
"""

from decimal import Decimal

import click
from rich.panel import Panel
from rich.table import Table

from .help import StyledCommand
from .helpers import (
    _read_contract_packed_storage,
    _read_issues_from_child_storage,
    _resolve_contract_and_network,
    colorize_status,
    console,
    emit_json,
    err_console,
    format_alpha,
    handle_exception,
    loading_context,
    print_network_header,
    read_issues_from_contract,
    validate_issue_id,
    validate_repository,
    with_cli_behavior_options,
    with_network_contract_options,
)


def _fill_percent(bounty: int, target: int) -> float:
    """Compute bounty fill percentage with Decimal precision.

    Returns 0.0 when target is non-positive, matching the existing fallback.
    Both render paths (Panel single-issue view and table all-issues view) call
    this so the same on-chain values render identically regardless of mode.
    """
    if target <= 0:
        return 0.0
    return float(Decimal(bounty) / Decimal(target) * 100)


@click.command('list', cls=StyledCommand)
@click.option(
    '--id',
    'issue_id',
    default=None,
    type=int,
    help='View a specific issue by ID',
)
@click.option(
    '--repo',
    'repo_filter',
    default=None,
    type=str,
    help='Filter issues to a specific repository (owner/name).',
)
@with_cli_behavior_options(
    include_verbose=True,
    include_json=True,
    verbose_help='Show debug output for contract reads',
)
@with_network_contract_options('Contract address (uses default if empty)')
def issues_list(
    issue_id: int, repo_filter: str, network: str, rpc_url: str, contract: str, verbose: bool, as_json: bool
):
    """List issues or view a specific issue.

    [dim]Examples:
        $ gitt issues list
        $ gitt i list --network test
        $ gitt i list --id 1
        $ gitt i list --json
    [/dim]
    """
    if issue_id is not None:
        try:
            validate_issue_id(issue_id, 'id')
        except click.BadParameter as e:
            handle_exception(as_json, str(e), 'bad_parameter')

    # Normalize and validate the --repo filter before any contract reads so
    # malformed input is rejected up-front and whitespace-padded valid input
    # still matches `repository_full_name` from the contract. validate_repository
    # strips whitespace, enforces owner/name format, and raises
    # click.BadParameter on bad input — same contract used by mutating commands.
    if repo_filter is not None:
        try:
            owner, repo_name = validate_repository(repo_filter, verify_exists=False)
            repo_filter = f'{owner}/{repo_name}'
        except click.BadParameter as e:
            handle_exception(as_json, str(e), 'bad_parameter')

    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(
        contract,
        network,
        rpc_url,
        missing_contract_message='Contract address not configured. Set via: gitt config set contract_address <ADDR>.',
    )

    print_network_header(network_name, contract_addr)

    with loading_context('Reading issues from contract...', as_json):
        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

    if as_json:
        # Enrich with human-readable ALPHA amounts for JSON consumers
        for issue in issues:
            issue['bounty_alpha'] = format_alpha(issue.get('bounty_amount', 0), 4)
            issue['target_alpha'] = format_alpha(issue.get('target_bounty', 0), 4)

        # Apply --repo filter before rendering (--id takes precedence)
        if repo_filter and issue_id is None:
            issues = [i for i in issues if i.get('repository_full_name', '').lower() == repo_filter.lower()]

        if issue_id is not None:
            issue = next((i for i in issues if i['id'] == issue_id), None)
            if issue is None:
                handle_exception(as_json, f'Issue {issue_id} not found on-chain.', 'not_found')
            emit_json({'success': True, 'issue': issue})
        else:
            emit_json({'success': True, 'issue_count': len(issues), 'issues': issues})
        return

    # Single issue detail view
    if issue_id is not None:
        issue = next((i for i in issues if i['id'] == issue_id), None)

        if issue:
            bounty_raw = issue.get('bounty_amount', 0)
            target_raw = issue.get('target_bounty', 0)
            fill_pct = _fill_percent(bounty_raw, target_raw)
            console.print(
                Panel(
                    f'[cyan]ID:[/cyan] {issue["id"]}\n'
                    f'[cyan]Repository:[/cyan] {issue["repository_full_name"]}\n'
                    f'[cyan]Issue Number:[/cyan] #{issue["issue_number"]}\n'
                    f'[cyan]Bounty Amount:[/cyan] {format_alpha(bounty_raw, 4)} ALPHA\n'
                    f'[cyan]Target Bounty:[/cyan] {format_alpha(target_raw, 4)} ALPHA\n'
                    f'[cyan]Fill %:[/cyan] {fill_pct:.1f}%\n'
                    f'[cyan]Status:[/cyan] {colorize_status(str(issue["status"]))}',
                    title=f'Issue #{issue_id}',
                    border_style='green',
                )
            )
        else:
            handle_exception(as_json, f'Issue {issue_id} not found on-chain.', 'not_found')
        return

    # Apply --repo filter before table render (--id takes precedence)
    if repo_filter and issue_id is None:
        issues = [i for i in issues if i.get('repository_full_name', '').lower() == repo_filter.lower()]

    # Table view of all issues
    err_console.print('[bold cyan]Available Issues[/bold cyan]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='cyan', justify='right')
    table.add_column('Repository', style='green')
    table.add_column('Issue #', style='yellow', justify='right')
    table.add_column('Bounty Pool', style='magenta', justify='right')
    table.add_column('Status', justify='center')

    if issues:
        for issue in issues:
            iid = issue.get('id', '?')
            repo = issue.get('repository_full_name', '?')
            num = issue.get('issue_number', '?')
            bounty_raw = issue.get('bounty_amount', 0)
            target_raw = issue.get('target_bounty', 0)
            status = issue.get('status', 'unknown')

            # Keep list rendering resilient to unexpected storage payload shapes.
            try:
                bounty_val = int(bounty_raw) if bounty_raw else 0
            except (TypeError, ValueError):
                bounty_val = 0
            try:
                target_val = int(target_raw) if target_raw else 0
            except (TypeError, ValueError):
                target_val = 0

            bounty_str = format_alpha(bounty_val, 1) if bounty_val else '0.0'
            target_str = format_alpha(target_val, 1) if target_val else '0.0'

            if target_val > 0:
                fill_pct = _fill_percent(bounty_val, target_val)
                if fill_pct >= 100:
                    bounty_display = f'{bounty_str} (100%)'
                elif bounty_val > 0:
                    bounty_display = f'{bounty_str}/{target_str} ({fill_pct:.0f}%)'
                else:
                    bounty_display = f'0/{target_str} (0%)'
            else:
                bounty_display = bounty_str if bounty_val > 0 else '0.00'

            if isinstance(status, dict):
                status = list(status.keys())[0] if status else 'Unknown'
            elif isinstance(status, str):
                status = status.capitalize()
            else:
                status = str(status)

            table.add_row(
                str(iid),
                repo,
                f'#{num}',
                bounty_display,
                colorize_status(status),
            )
        console.print(table)
        err_console.print(f'\n[dim]Showing {len(issues)} issue(s)[/dim]')
        err_console.print('[dim]Bounty Pool shows: filled/target (percentage)[/dim]')
    else:
        err_console.print('[yellow]No issues found.[/yellow]')
        err_console.print('[dim]Register an issue: gitt issues register --repo owner/repo --issue 1 --bounty 100[/dim]')


@click.command('bounty-pool', cls=StyledCommand)
@with_cli_behavior_options(include_verbose=True, include_json=True)
@with_network_contract_options('Contract address (uses config if empty)')
def issues_bounty_pool(network: str, rpc_url: str, contract: str, verbose: bool, as_json: bool):
    """View total bounty pool (sum of all issue bounty amounts).

    [dim]Examples:
        $ gitt issues bounty-pool
        $ gitt i bounty-pool --json
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    print_network_header(network_name, contract_addr)

    try:
        from substrateinterface import SubstrateInterface

        with loading_context('Reading contract storage...', as_json):
            substrate = SubstrateInterface(url=ws_endpoint)
            issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)

        if as_json:
            emit_json(
                {
                    'success': True,
                    'total_bounty_pool_raw': total_bounty_pool,
                    'total_bounty_pool_alpha': format_alpha(total_bounty_pool, 4),
                    'issue_count': len(issues),
                }
            )
            return

        console.print(
            f'[green]Issue Bounty Pool:[/green] {format_alpha(total_bounty_pool, 4)} ALPHA ({total_bounty_pool} raw)'
        )
        err_console.print(f'[dim]Sum of bounty amounts from {len(issues)} issue(s)[/dim]')
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))


@click.command('pending-harvest', cls=StyledCommand)
@with_cli_behavior_options(include_verbose=True, include_json=True)
@with_network_contract_options('Contract address (uses config if empty)')
def issues_pending_harvest(network: str, rpc_url: str, contract: str, verbose: bool, as_json: bool):
    """View pending harvest (treasury stake minus allocated bounties).

    [dim]Examples:
        $ gitt issues pending-harvest
        $ gitt i pending-harvest --json
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    print_network_header(network_name, contract_addr)

    try:
        import bittensor as bt
        from substrateinterface import SubstrateInterface

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with loading_context('Reading treasury and contract data...', as_json):
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            treasury_stake = client.get_treasury_stake()

            substrate = SubstrateInterface(url=ws_endpoint)
            issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)
            total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)

        pending_harvest = max(0, treasury_stake - total_bounty_pool)

        if as_json:
            emit_json(
                {
                    'success': True,
                    'treasury_stake_raw': treasury_stake,
                    'treasury_stake_alpha': format_alpha(treasury_stake, 4),
                    'allocated_bounties_raw': total_bounty_pool,
                    'allocated_bounties_alpha': format_alpha(total_bounty_pool, 4),
                    'pending_harvest_raw': pending_harvest,
                    'pending_harvest_alpha': format_alpha(pending_harvest, 4),
                }
            )
            return

        console.print(f'[green]Treasury Stake:[/green] {format_alpha(treasury_stake, 4)} ALPHA')
        console.print(f'[green]Allocated to Bounties:[/green] {format_alpha(total_bounty_pool, 4)} ALPHA')
        console.print(f'[green]Pending Harvest:[/green] {format_alpha(pending_harvest, 4)} ALPHA')
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))


@click.command('info', cls=StyledCommand)
@with_cli_behavior_options(include_verbose=True, include_json=True)
@with_network_contract_options('Contract address (uses config if empty)')
def admin_info(network: str, rpc_url: str, contract: str, verbose: bool, as_json: bool):
    """View contract configuration.

    [dim]Examples:
        $ gitt admin info
        $ gitt a info --json
    [/dim]
    """
    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(contract, network, rpc_url)

    print_network_header(network_name, contract_addr)

    try:
        from substrateinterface import SubstrateInterface

        with loading_context('Reading contract configuration...', as_json):
            substrate = SubstrateInterface(url=ws_endpoint)
            packed = _read_contract_packed_storage(substrate, contract_addr, verbose)

        if packed:
            if as_json:
                emit_json({'success': True, **packed})
                return

            console.print(
                Panel(
                    f'[cyan]Owner:[/cyan] {packed.get("owner", "N/A")}\n'
                    f'[cyan]Treasury Hotkey:[/cyan] {packed.get("treasury_hotkey", "N/A")}\n'
                    f'[cyan]Netuid:[/cyan] {packed.get("netuid", "N/A")}\n'
                    f'[cyan]Next Issue ID:[/cyan] {packed.get("next_issue_id", "N/A")}',
                    title='Contract Configuration (v0)',
                    border_style='green',
                )
            )
        else:
            msg = 'Could not read contract configuration.'
            if as_json:
                handle_exception(as_json=as_json, message=msg, error_type='read_failed')
            err_console.print(f'[yellow]{msg}[/yellow]')
            err_console.print('[dim]Try running with --verbose to see debug details.[/dim]')
            raise SystemExit(1)
    except Exception as e:
        handle_exception(as_json=as_json, message=str(e))
