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
    format_alpha,
    get_contract_address,
    print_error,
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
@click.option('--json', 'json_output', is_flag=True, help='Output in JSON format')
@click.option('--verbose', '-v', is_flag=True, help='Show debug output for contract reads')
def issues_list(issue_id: int, network: str, rpc_url: str, contract: str, json_output: bool, verbose: bool):
    """
    List issues or view a specific issue.

    Shows all issues with their status and bounty amounts.
    Use --id to view details for a specific issue.

    \b
    Examples:
        gitt list
        gitt list --network test
        gitt list --id 1
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        if json_output:
            import json as json_lib
            print(json_lib.dumps({'status': 'error', 'message': 'Contract address not configured'}))
        else:
            print_error('Contract address not configured. Set via: gitt config set contract_address <ADDR>')
        return

    if not json_output:
        console.print(f'[dim]Network: {network_name} ({ws_endpoint})[/dim]')
        console.print(f'[dim]Contract: {contract_addr[:20]}...[/dim]\n')

    with console.status('[yellow]Reading issues from contract...[/yellow]', spinner='dots', disable=json_output):
        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

    if json_output:
        import json as json_lib
        if issue_id is not None:
            issue = next((i for i in issues if i['id'] == issue_id), None)
            print(json_lib.dumps(issue if issue else {'status': 'error', 'message': f'Issue {issue_id} not found'}))
        else:
            print(json_lib.dumps(issues))
        return

    # Single issue detail view
    if issue_id is not None:
        issue = next((i for i in issues if i['id'] == issue_id), None)

        if issue:
            console.print(
                Panel(
                    f'[cyan]ID:[/cyan] {issue["id"]}\n'
                    f'[cyan]Repository:[/cyan] {issue["repository_full_name"]}\n'
                    f'[cyan]Issue Number:[/cyan] #{issue["issue_number"]}\n'
                    f'[cyan]Bounty Amount:[/cyan] {format_alpha(issue["bounty_amount"])}\n'
                    f'[cyan]Target Bounty:[/cyan] {format_alpha(issue["target_bounty"])}\n'
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

            # Format bounty pool display with fill percentage
            if target_raw > 0:
                fill_pct = (bounty_raw / target_raw) * 100
                bounty_display = f'{bounty_raw / 1e9:.1f}/{target_raw / 1e9:.1f} ({fill_pct:.0f}%)'
            else:
                bounty_display = f'{bounty_raw / 1e9:.2f}'

            # Format status with colors
            if isinstance(status, dict):
                status_str = list(status.keys())[0] if status else 'Unknown'
            elif isinstance(status, str):
                status_str = status.capitalize()
            else:
                status_str = str(status)

            status_style = 'blue'
            if status_str.lower() == 'active':
                status_style = 'bold green'
            elif status_str.lower() == 'completed':
                status_style = 'green'
            elif status_str.lower() == 'cancelled':
                status_style = 'red'
            elif status_str.lower() == 'registered':
                status_style = 'cyan'

            table.add_row(
                str(issue_id),
                repo,
                f'#{num}',
                bounty_display,
                f'[{status_style}]{status_str}[/{status_style}]',
            )
        console.print(table)
        console.print(f'\n[dim]Showing {len(issues)} issue(s)[/dim]')
        console.print('[dim]Bounty Pool shows: filled/target (percentage)[/dim]')
    else:
        console.print('[yellow]No issues found. Register an issue with:[/yellow]')
        console.print('[dim]  gitt register --repo owner/repo --issue 1 --bounty 100[/dim]')


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
@click.option('--json', 'json_output', is_flag=True, help='Output in JSON format')
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def issues_bounty_pool(network: str, rpc_url: str, contract: str, json_output: bool, verbose: bool):
    """View total bounty pool (sum of all issue bounty amounts)."""
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
        console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from substrateinterface import SubstrateInterface

        with console.status('[yellow]Reading issues...[/yellow]', disable=json_output):
            substrate = SubstrateInterface(url=ws_endpoint)
            issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)

        if json_output:
            import json as json_lib
            print(json_lib.dumps({'total_bounty_pool': total_bounty_pool, 'issue_count': len(issues)}))
        else:
            console.print(f'[green]Issue Bounty Pool:[/green] {format_alpha(total_bounty_pool)}')
            console.print(f'[dim]Sum of bounty amounts from {len(issues)} issue(s)[/dim]')
    except Exception as e:
        if json_output:
            import json as json_lib
            print(json_lib.dumps({'status': 'error', 'message': str(e)}))
        else:
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
@click.option('--json', 'json_output', is_flag=True, help='Output in JSON format')
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def issues_pending_harvest(network: str, rpc_url: str, contract: str, json_output: bool, verbose: bool):
    """View pending harvest (treasury stake minus allocated bounties)."""
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
        console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        import bittensor as bt
        from substrateinterface import SubstrateInterface

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with console.status('[yellow]Reading contract data...[/yellow]', disable=json_output):
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

        if json_output:
            import json as json_lib
            print(
                json_lib.dumps(
                    {
                        'treasury_stake': treasury_stake,
                        'allocated_to_bounties': total_bounty_pool,
                        'pending_harvest': pending_harvest,
                    }
                )
            )
        else:
            console.print(f'[green]Treasury Stake:[/green] {format_alpha(treasury_stake)}')
            console.print(f'[green]Allocated to Bounties:[/green] {format_alpha(total_bounty_pool)}')
            console.print(f'[green]Pending Harvest:[/green] {format_alpha(pending_harvest)}')
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
@click.option('--json', 'json_output', is_flag=True, help='Output in JSON format')
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def admin_info(network: str, rpc_url: str, contract: str, json_output: bool, verbose: bool):
    """View contract configuration."""
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
        console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from substrateinterface import SubstrateInterface

        with console.status('[yellow]Reading contract configuration...[/yellow]', disable=json_output):
            substrate = SubstrateInterface(url=ws_endpoint)
            packed = _read_contract_packed_storage(substrate, contract_addr, verbose)

        if packed:
            if json_output:
                import json as json_lib
                print(json_lib.dumps(packed))
            else:
                console.print(
                    Panel(
                        f'[cyan]Owner:[/cyan] {packed.get("owner", "N/A")}\n'
                        f'[cyan]Treasury Hotkey:[/cyan] {packed.get("treasury_hotkey", "N/A")}\n'
                        f'[cyan]Netuid:[/cyan] {packed.get("netuid", "N/A")}\n'
                        f'[cyan]Next Issue ID:[/cyan] {packed.get("next_issue_id", "N/A")}',
                        title='Contract Configuration',
                        border_style='green',
                    )
                )
        else:
            if json_output:
                import json as json_lib
                print(json_lib.dumps({'status': 'error', 'message': 'Could not read contract configuration'}))
            else:
                console.print('[yellow]Could not read contract configuration.[/yellow]')
    except Exception as e:
        if json_output:
            import json as json_lib
            print(json_lib.dumps({'status': 'error', 'message': str(e)}))
        else:
            print_error(str(e))
