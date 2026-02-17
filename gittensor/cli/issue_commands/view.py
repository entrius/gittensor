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
    _read_contract_packed_storage,
    _read_issues_from_child_storage,
    console,
    get_contract_address,
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
    help='Contract address (uses default if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output for contract reads')
def issues_list(issue_id: int, network: str, rpc_url: str, contract: str, verbose: bool):
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
        console.print('[red]Error: Contract address not configured.[/red]')
        console.print('[dim]Set via: gitt config set contract_address <ADDR>[/dim]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr[:20]}...[/dim]\n')

    issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

    # Single issue detail view
    if issue_id is not None:
        issue = next((i for i in issues if i['id'] == issue_id), None)

        if issue:
            console.print(
                Panel(
                    f'[cyan]ID:[/cyan] {issue["id"]}\n'
                    f'[cyan]Repository:[/cyan] {issue["repository_full_name"]}\n'
                    f'[cyan]Issue Number:[/cyan] #{issue["issue_number"]}\n'
                    f'[cyan]Bounty Amount:[/cyan] {issue["bounty_amount"] / 1e9:.4f} ALPHA\n'
                    f'[cyan]Target Bounty:[/cyan] {issue["target_bounty"] / 1e9:.4f} ALPHA\n'
                    f'[cyan]Fill %:[/cyan] {(issue["bounty_amount"] / issue["target_bounty"] * 100) if issue["target_bounty"] > 0 else 0:.1f}%\n'
                    f'[cyan]Status:[/cyan] {issue["status"]}',
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
    table.add_column('Status', style='blue')

    if issues:
        for issue in issues:
            issue_id = issue.get('id', '?')
            repo = issue.get('repository_full_name', '?')
            num = issue.get('issue_number', '?')
            bounty_raw = issue.get('bounty_amount', 0)
            target_raw = issue.get('target_bounty', 0)
            status = issue.get('status', 'unknown')

            try:
                bounty = float(bounty_raw) / 1_000_000_000 if bounty_raw else 0.0
                target = float(target_raw) / 1_000_000_000 if target_raw else 0.0
            except (ValueError, TypeError):
                bounty = 0.0
                target = 0.0

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

            # Format status
            if isinstance(status, dict):
                status = list(status.keys())[0] if status else 'Unknown'
            elif isinstance(status, str):
                status = status.capitalize()
            else:
                status = str(status)

            table.add_row(
                str(issue_id),
                repo,
                f'#{num}',
                bounty_display,
                status,
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
def issues_bounty_pool(network: str, rpc_url: str, contract: str, verbose: bool):
    """View total bounty pool (sum of all issue bounty amounts)."""
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)
        console.print(
            f'[green]Issue Bounty Pool:[/green] {total_bounty_pool / 1e9:.4f} ALPHA ({total_bounty_pool} raw)'
        )
        console.print(f'[dim]Sum of bounty amounts from {len(issues)} issue(s)[/dim]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


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
def issues_pending_harvest(network: str, rpc_url: str, contract: str, verbose: bool):
    """View pending harvest (treasury stake minus allocated bounties)."""
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        import bittensor as bt
        from substrateinterface import SubstrateInterface

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

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

        console.print(f'[green]Treasury Stake:[/green] {treasury_stake / 1e9:.4f} ALPHA')
        console.print(f'[green]Allocated to Bounties:[/green] {total_bounty_pool / 1e9:.4f} ALPHA')
        console.print(f'[green]Pending Harvest:[/green] {pending_harvest / 1e9:.4f} ALPHA')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


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
def admin_info(network: str, rpc_url: str, contract: str, verbose: bool):
    """View contract configuration."""
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print('[dim]Reading config...[/dim]\n')

    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        packed = _read_contract_packed_storage(substrate, contract_addr, verbose)

        if packed:
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
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')
