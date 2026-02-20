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

import json
from decimal import Decimal

import click
from rich.panel import Panel
from rich.table import Table

from .helpers import (
    ALPHA_SCALE,
    STATUS_COLORS,
    _read_contract_packed_storage,
    _read_issues_from_child_storage,
    colorize_status,
    console,
    format_alpha,
    get_contract_address,
    print_error,
    print_network_header,
    read_issues_from_contract,
    resolve_network,
)


@click.command('list')
@click.option(
    '--id',
    'issue_id',
    default=None,
    type=int,
    help='View a specific issue by ID',
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
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'output_json', is_flag=True, help='Output raw JSON for scripting')
def issues_list(issue_id: int, network: str, rpc_url: str, contract: str, verbose: bool, output_json: bool):
    """
    List issues or view a specific issue.

    Shows all issues with their status and bounty amounts.
    Use --id to view details for a specific issue.

    \b
    Examples:
        gitt issues list
        gitt i list --network test
        gitt i list --id 1
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured. Set via: gitt config set contract_address <ADDR>')

    if not output_json:
        print_network_header(network_name, contract_addr)
        console.print()

    if verbose:
        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)
    else:
        with console.status('Reading issues from contract...', spinner='dots'):
            issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

    # JSON output mode
    if output_json:
        output = [
            {
                **issue,
                'bounty_alpha': format_alpha(issue.get('bounty_amount', 0), 4),
                'target_alpha': format_alpha(issue.get('target_bounty', 0), 4),
            }
            for issue in issues
        ]
        click.echo(json.dumps(output, indent=2))
        return

    # Single issue detail view
    if issue_id is not None:
        issue = next((i for i in issues if i['id'] == issue_id), None)

        if issue:
            fill_pct = (issue['bounty_amount'] / issue['target_bounty'] * 100) if issue['target_bounty'] > 0 else 0
            console.print(
                Panel(
                    f'[cyan]ID:[/cyan] {issue["id"]}\n'
                    f'[cyan]Repository:[/cyan] {issue["repository_full_name"]}\n'
                    f'[cyan]Issue Number:[/cyan] #{issue["issue_number"]}\n'
                    f'[cyan]Bounty Amount:[/cyan] {format_alpha(issue["bounty_amount"], 4)}\n'
                    f'[cyan]Target Bounty:[/cyan] {format_alpha(issue["target_bounty"], 4)}\n'
                    f'[cyan]Fill %:[/cyan] {fill_pct:.1f}%\n'
                    f'[cyan]Status:[/cyan] [{STATUS_COLORS.get(str(issue["status"]), "white")}]'
                    f'{issue["status"]}[/{STATUS_COLORS.get(str(issue["status"]), "white")}]',
                    title=f'Issue #{issue_id}',
                    border_style='green',
                )
            )
        else:
            console.print(f'[yellow]Issue {issue_id} not found.[/yellow]')
        return

    # Table view of all issues
    console.print('[bold cyan]Available Issues[/bold cyan]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='cyan', justify='right')
    table.add_column('Repository', style='green')
    table.add_column('Issue #', style='yellow', justify='right')
    table.add_column('Bounty Pool', style='magenta', justify='right')
    table.add_column('Status')

    if issues:
        for issue in issues:
            iid = issue.get('id', '?')
            repo = issue.get('repository_full_name', '?')
            num = issue.get('issue_number', '?')
            bounty_raw = issue.get('bounty_amount', 0)
            target_raw = issue.get('target_bounty', 0)
            status = issue.get('status', 'unknown')

            # Format bounty pool display with fill percentage.
            # Use Decimal for precision; omit " ALPHA" suffix since the
            # column header already implies the unit.
            bounty = Decimal(bounty_raw) / ALPHA_SCALE if bounty_raw else Decimal(0)
            target = Decimal(target_raw) / ALPHA_SCALE if target_raw else Decimal(0)

            if target > 0:
                fill_pct = float(bounty / target * 100)
                if fill_pct >= 100:
                    bounty_display = f'{bounty:.1f} (100%)'
                elif bounty > 0:
                    bounty_display = f'{bounty:.1f}/{target:.1f} ({fill_pct:.0f}%)'
                else:
                    bounty_display = f'0/{target:.1f} (0%)'
            else:
                bounty_display = f'{bounty:.2f}' if bounty > 0 else '0.00'

            # Format status
            if isinstance(status, dict):
                status = list(status.keys())[0] if status else 'Unknown'
            elif isinstance(status, str):
                status = status.capitalize()
            else:
                status = str(status)

            # Color-code status
            status_text = colorize_status(status)

            table.add_row(
                str(iid),
                repo,
                f'#{num}',
                bounty_display,
                status_text,
            )
        console.print(table)
        console.print(f'\n[dim]Showing {len(issues)} issue(s)[/dim]')
        console.print('[dim]Bounty Pool shows: filled/target (percentage)[/dim]')
    else:
        console.print('[yellow]No issues found. Register an issue with:[/yellow]')
        console.print('[dim]  gitt issues register --repo owner/repo --issue 1 --bounty 100[/dim]')


@click.command('bounty-pool')
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
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'output_json', is_flag=True, help='Output raw JSON for scripting')
def issues_bounty_pool(network: str, rpc_url: str, contract: str, verbose: bool, output_json: bool):
    """
    View total bounty pool (sum of all issue bounty amounts).

    \b
    Examples:
        gitt issues bounty-pool
        gitt issues bounty-pool --network test
        gitt issues bounty-pool --json
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured.')

    if not output_json:
        print_network_header(network_name, contract_addr)

    try:
        from substrateinterface import SubstrateInterface

        if verbose:
            substrate = SubstrateInterface(url=ws_endpoint)
            issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)
        else:
            with console.status('Reading contract...', spinner='dots'):
                substrate = SubstrateInterface(url=ws_endpoint)
                issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)

        if output_json:
            click.echo(
                json.dumps(
                    {
                        'bounty_pool': total_bounty_pool,
                        'bounty_pool_alpha': format_alpha(total_bounty_pool, 4),
                        'issue_count': len(issues),
                    },
                    indent=2,
                )
            )
            return

        console.print(
            f'[green]Issue Bounty Pool:[/green] {format_alpha(total_bounty_pool, 4)} ({total_bounty_pool} raw)'
        )
        console.print(f'[dim]Sum of bounty amounts from {len(issues)} issue(s)[/dim]')
        if total_bounty_pool == 0 and not issues:
            console.print('[dim]No issues registered yet. Register one with: gitt issues register --repo owner/repo --issue 1 --bounty 100[/dim]')
    except Exception as e:
        print_error(str(e))


@click.command('pending-harvest')
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
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'output_json', is_flag=True, help='Output raw JSON for scripting')
def issues_pending_harvest(network: str, rpc_url: str, contract: str, verbose: bool, output_json: bool):
    """
    View pending harvest (treasury stake minus allocated bounties).

    \b
    Examples:
        gitt issues pending-harvest
        gitt issues pending-harvest --network test
        gitt issues pending-harvest --json
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured.')

    if not output_json:
        print_network_header(network_name, contract_addr)

    try:
        import bittensor as bt
        from substrateinterface import SubstrateInterface

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        # Get treasury stake
        if verbose:
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            treasury_stake = client.get_treasury_stake()
            substrate = SubstrateInterface(url=ws_endpoint)
            issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)
        else:
            with console.status('Connecting to subtensor...', spinner='dots'):
                subtensor = bt.Subtensor(network=ws_endpoint)
                client = IssueCompetitionContractClient(
                    contract_address=contract_addr,
                    subtensor=subtensor,
                )

            with console.status('Reading contract state...', spinner='dots'):
                treasury_stake = client.get_treasury_stake()
                substrate = SubstrateInterface(url=ws_endpoint)
                issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)

        # Pending harvest = treasury stake - allocated bounties
        pending_harvest = max(0, treasury_stake - total_bounty_pool)

        if output_json:
            click.echo(
                json.dumps(
                    {
                        'treasury_stake': treasury_stake,
                        'treasury_stake_alpha': format_alpha(treasury_stake, 4),
                        'allocated_bounties': total_bounty_pool,
                        'allocated_bounties_alpha': format_alpha(total_bounty_pool, 4),
                        'pending_harvest': pending_harvest,
                        'pending_harvest_alpha': format_alpha(pending_harvest, 4),
                    },
                    indent=2,
                )
            )
            return

        console.print(f'[green]Treasury Stake:[/green] {format_alpha(treasury_stake, 4)}')
        console.print(f'[green]Allocated to Bounties:[/green] {format_alpha(total_bounty_pool, 4)}')
        console.print(f'[green]Pending Harvest:[/green] {format_alpha(pending_harvest, 4)}')
        if pending_harvest == 0 and treasury_stake == 0:
            console.print('[dim]No treasury stake found. Fund the contract or run: gitt harvest[/dim]')
    except ImportError as e:
        print_error(f'Missing dependency - {e}')
    except Exception as e:
        print_error(str(e))


@click.command('info')
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
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'output_json', is_flag=True, help='Output raw JSON for scripting')
def admin_info(network: str, rpc_url: str, contract: str, verbose: bool, output_json: bool):
    """
    View contract configuration.

    Shows the contract owner, treasury hotkey, netuid, and next issue ID.

    \b
    Examples:
        gitt admin info
        gitt admin info --network test
        gitt admin info --json
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured.')

    if not output_json:
        print_network_header(network_name, contract_addr)

    try:
        from substrateinterface import SubstrateInterface

        if verbose:
            substrate = SubstrateInterface(url=ws_endpoint)
            packed = _read_contract_packed_storage(substrate, contract_addr, verbose)
        else:
            with console.status('Reading contract configuration...', spinner='dots'):
                substrate = SubstrateInterface(url=ws_endpoint)
                packed = _read_contract_packed_storage(substrate, contract_addr, verbose)

        if packed:
            if output_json:
                click.echo(json.dumps(packed, indent=2))
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
            console.print('[yellow]Could not read contract configuration.[/yellow]')
            console.print('[dim]Verify the contract address is correct: gitt admin info --verbose[/dim]')
    except Exception as e:
        print_error(str(e))
