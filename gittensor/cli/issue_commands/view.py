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

import click
from rich.panel import Panel
from rich.table import Table

from .helpers import (
    ALPHA_RAW_UNIT,
    _read_contract_packed_storage,
    _read_issues_from_child_storage,
    console,
    format_alpha,
    get_contract_address,
    output_json,
    print_error,
    print_network_header,
    read_issues_from_contract,
    resolve_network,
    style_status,
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
    help='Contract address (uses default if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output for contract reads')
@click.option('--json', 'output_json_flag', is_flag=True, help='Output as JSON')
def issues_list(issue_id: int, network: str, rpc_url: str, contract: str, verbose: bool, output_json_flag: bool):
    """
    List issues or view a specific issue.

    Shows all issues with their status and bounty amounts.
    Use --id to view details for a specific issue.

    \b
    Examples:
        gitt issues list
        gitt i list --network test
        gitt i list --id 1
        gitt i list --json
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        console.print('[dim]Set via: gitt config set contract_address <ADDR>[/dim]')
        return

    if not output_json_flag:
        print_network_header(network_name, ws_endpoint, contract_addr)

    with console.status('[bold cyan]Reading issues from contract...[/bold cyan]'):
        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

    # --- JSON output ---
    if output_json_flag:
        json_data = []
        for issue in issues:
            bounty_raw = issue.get('bounty_amount', 0)
            target_raw = issue.get('target_bounty', 0)
            json_data.append({
                'id': issue.get('id'),
                'repository': issue.get('repository_full_name'),
                'issue_number': issue.get('issue_number'),
                'bounty_amount_raw': bounty_raw,
                'bounty_amount_alpha': bounty_raw / ALPHA_RAW_UNIT,
                'target_bounty_raw': target_raw,
                'target_bounty_alpha': target_raw / ALPHA_RAW_UNIT,
                'status': issue.get('status'),
            })
        output_json(json_data if issue_id is None else next((i for i in json_data if i['id'] == issue_id), None))
        return

    # --- Single issue detail view ---
    if issue_id is not None:
        issue = next((i for i in issues if i['id'] == issue_id), None)

        if issue:
            bounty_raw = issue['bounty_amount']
            target_raw = issue['target_bounty']
            fill_pct = (bounty_raw / target_raw * 100) if target_raw > 0 else 0

            console.print(
                Panel(
                    f'[cyan]ID:[/cyan] {issue["id"]}\n'
                    f'[cyan]Repository:[/cyan] {issue["repository_full_name"]}\n'
                    f'[cyan]Issue Number:[/cyan] #{issue["issue_number"]}\n'
                    f'[cyan]Bounty Amount:[/cyan] {format_alpha(bounty_raw, 4)}\n'
                    f'[cyan]Target Bounty:[/cyan] {format_alpha(target_raw, 4)}\n'
                    f'[cyan]Fill %:[/cyan] {fill_pct:.1f}%\n'
                    f'[cyan]Status:[/cyan] {style_status(issue["status"])}',
                    title=f'Issue #{issue_id}',
                    border_style='green',
                )
            )
        else:
            print_error(f'Issue {issue_id} not found.')
            console.print('[dim]Use "gitt issues list" to see all available issues.[/dim]')
        return

    # --- Table view of all issues ---
    console.print('[bold cyan]Available Issues[/bold cyan]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='cyan', justify='right')
    table.add_column('Repository', style='green')
    table.add_column('Issue #', style='yellow', justify='right')
    table.add_column('Bounty Pool', style='magenta', justify='right')
    table.add_column('Status')

    if issues:
        for issue in issues:
            issue_id_val = issue.get('id', '?')
            repo = issue.get('repository_full_name', '?')
            num = issue.get('issue_number', '?')
            bounty_raw = issue.get('bounty_amount', 0)
            target_raw = issue.get('target_bounty', 0)
            status = issue.get('status', 'unknown')

            bounty = bounty_raw / ALPHA_RAW_UNIT if bounty_raw else 0.0
            target = target_raw / ALPHA_RAW_UNIT if target_raw else 0.0

            # Format bounty pool display with fill percentage
            if target > 0:
                fill_pct = (bounty / target) * 100
                if fill_pct >= 100:
                    bounty_display = f'{bounty:.1f} (100%)'
                elif bounty > 0:
                    bounty_display = f'{bounty:.1f}/{target:.1f} ({fill_pct:.0f}%)'
                else:
                    bounty_display = f'0/{target:.1f} (0%)'
            else:
                bounty_display = f'{bounty:.2f}' if bounty > 0 else '0.00'

            # Format status with color
            if isinstance(status, dict):
                status = list(status.keys())[0] if status else 'Unknown'
            elif isinstance(status, str):
                status = status.capitalize()
            else:
                status = str(status)

            table.add_row(
                str(issue_id_val),
                repo,
                f'#{num}',
                bounty_display,
                style_status(status),
            )
        console.print(table)
        console.print(f'\n[dim]Showing {len(issues)} issue(s)[/dim]')
        console.print('[dim]Bounty Pool shows: filled/target (percentage)[/dim]')
    else:
        console.print(
            Panel(
                '[yellow]No issues found.[/yellow]\n\n'
                '[dim]Register your first issue:[/dim]\n'
                '  gitt issues register --repo owner/repo --issue 1 --bounty 100',
                title='Issues',
                border_style='dim',
            )
        )


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
@click.option('--json', 'output_json_flag', is_flag=True, help='Output as JSON')
def issues_bounty_pool(network: str, rpc_url: str, contract: str, verbose: bool, output_json_flag: bool):
    """View total bounty pool (sum of all issue bounty amounts)."""
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    if not output_json_flag:
        print_network_header(network_name, ws_endpoint, contract_addr)

    try:
        from substrateinterface import SubstrateInterface

        with console.status('[bold cyan]Reading bounty pool...[/bold cyan]'):
            substrate = SubstrateInterface(url=ws_endpoint)
            issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)

        if output_json_flag:
            output_json({
                'bounty_pool_raw': total_bounty_pool,
                'bounty_pool_alpha': total_bounty_pool / ALPHA_RAW_UNIT,
                'issue_count': len(issues),
            })
            return

        console.print(f'[green]Issue Bounty Pool:[/green] {format_alpha(total_bounty_pool, 4)} ({total_bounty_pool:,} raw)')
        console.print(f'[dim]Sum of bounty amounts from {len(issues)} issue(s)[/dim]')
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
@click.option('--json', 'output_json_flag', is_flag=True, help='Output as JSON')
def issues_pending_harvest(network: str, rpc_url: str, contract: str, verbose: bool, output_json_flag: bool):
    """View pending harvest (treasury stake minus allocated bounties)."""
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    if not output_json_flag:
        print_network_header(network_name, ws_endpoint, contract_addr)

    try:
        import bittensor as bt
        from substrateinterface import SubstrateInterface

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[bold cyan]Reading pending harvest...[/bold cyan]'):
            # Get treasury stake
            subtensor = bt.Subtensor(network=ws_endpoint)
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )
            treasury_stake = client.get_treasury_stake()

            # Get total bounty pool (sum of all issue bounty amounts)
            substrate = SubstrateInterface(url=ws_endpoint)
            issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)
            total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)

        # Pending harvest = treasury stake - allocated bounties
        pending_harvest = max(0, treasury_stake - total_bounty_pool)

        if output_json_flag:
            output_json({
                'treasury_stake_raw': treasury_stake,
                'treasury_stake_alpha': treasury_stake / ALPHA_RAW_UNIT,
                'allocated_raw': total_bounty_pool,
                'allocated_alpha': total_bounty_pool / ALPHA_RAW_UNIT,
                'pending_harvest_raw': pending_harvest,
                'pending_harvest_alpha': pending_harvest / ALPHA_RAW_UNIT,
            })
            return

        console.print(f'[green]Treasury Stake:[/green] {format_alpha(treasury_stake, 4)}')
        console.print(f'[green]Allocated to Bounties:[/green] {format_alpha(total_bounty_pool, 4)}')
        console.print(f'[green]Pending Harvest:[/green] {format_alpha(pending_harvest, 4)}')
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
@click.option('--json', 'output_json_flag', is_flag=True, help='Output as JSON')
def admin_info(network: str, rpc_url: str, contract: str, verbose: bool, output_json_flag: bool):
    """View contract configuration."""
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        print_error('Contract address not configured.')
        return

    if not output_json_flag:
        print_network_header(network_name, ws_endpoint, contract_addr)

    try:
        from substrateinterface import SubstrateInterface

        with console.status('[bold cyan]Reading contract config...[/bold cyan]'):
            substrate = SubstrateInterface(url=ws_endpoint)
            packed = _read_contract_packed_storage(substrate, contract_addr, verbose)

        if packed:
            if output_json_flag:
                output_json(packed)
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
            print_error('Could not read contract configuration.')
    except Exception as e:
        print_error(str(e))
