# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
View subgroup commands for issue CLI

Commands:
    gitt view issues (alias: v issues)
    gitt view issue <id> (alias: v issue <id>)
    gitt view issue-bounty-pool (alias: v issue-bounty-pool)
    gitt view issue-pending-harvest (alias: v issue-pending-harvest)
    gitt view issue-contract-config (alias: v issue-contract-config)
"""

import json
import urllib.request
import urllib.error

import click
from rich.table import Table
from rich.panel import Panel

from .helpers import (
    console,
    load_config,
    get_contract_address,
    get_ws_endpoint,
    read_issues_from_contract,
    _read_contract_packed_storage,
    _read_issues_from_child_storage,
)


@click.group(name='view')
def view():
    """View contract state and API data (read-only commands).

    \b
    Contract reads:
        issues                   List issues by status
        issue <id>               View raw issue data
        issue-bounty-pool        View current alpha pool balance
        issue-pending-harvest    View pending emissions value
        issue-contract-config    View contract configuration
    """
    pass


@view.command('issues')
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses default if empty)',
)
@click.option('--testnet', is_flag=True, help='Use testnet contract address')
@click.option('--from-api', is_flag=True, help='Force reading from API instead of contract')
@click.option('--verbose', '-v', is_flag=True, help='Show debug output for contract reads')
def view_issues(rpc_url: str, contract: str, testnet: bool, from_api: bool, verbose: bool):
    """
    List available issues.

    Shows all issues with their status and bounty amounts.

    By default, reads directly from the smart contract (no API dependency).
    Use --from-api to read from the API instead.

    \b
    Example:
        gitt view issues
        gitt v issues --testnet
    """
    console.print('\n[bold cyan]Available Issues[/bold cyan]\n')

    # Load configuration
    config = load_config()
    contract_addr = get_contract_address(contract, testnet)
    ws_endpoint = get_ws_endpoint(rpc_url)

    issues = []

    # Default: read from contract directly (no API dependency)
    if not from_api and contract_addr:
        console.print(f'[dim]Data source: Contract at {contract_addr[:20]}...[/dim]')
        console.print(f'[dim]Endpoint: {ws_endpoint}[/dim]\n')

        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

        if not issues:
            console.print('[yellow]No issues found in contract or contract read failed.[/yellow]')
            if verbose:
                console.print('[dim]Debug: Contract read returned empty list[/dim]')
            console.print('[dim]Falling back to API...[/dim]\n')
            from_api = True

    # Fallback or explicit API mode
    if from_api or not contract_addr:
        api_url = config.get('api_url', 'http://localhost:3000')
        console.print(f'[dim]Data source: API at {api_url}[/dim]')
        if contract_addr:
            console.print(f'[dim]Contract: {contract_addr[:20]}... @ {ws_endpoint}[/dim]\n')

        issues_endpoint = f'{api_url}/issues'

        try:
            req = urllib.request.Request(issues_endpoint, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                # Handle both direct array and wrapped response
                if isinstance(data, list):
                    issues = data
                elif isinstance(data, dict) and 'issues' in data:
                    issues = data['issues']
                elif isinstance(data, dict) and 'data' in data:
                    issues = data['data']
        except urllib.error.URLError as e:
            console.print(f'[yellow]Could not reach API ({e.reason}).[/yellow]')
            console.print('[dim]Ensure API is running or use direct contract reads.[/dim]\n')
        except Exception as e:
            console.print(f'[yellow]Error fetching issues: {e}[/yellow]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='cyan', justify='right')
    table.add_column('Repository', style='green')
    table.add_column('Issue #', style='yellow', justify='right')
    table.add_column('Bounty Pool', style='magenta', justify='right')
    table.add_column('Status', style='blue')

    if issues:
        for issue in issues:
            # Handle different field naming conventions (camelCase from API, snake_case from contract)
            issue_id = issue.get('id', issue.get('issue_id', '?'))
            repo = issue.get('repositoryFullName', issue.get('repository_full_name', issue.get('repo', '?')))
            num = issue.get('issueNumber', issue.get('issue_number', issue.get('number', '?')))
            # Get bounty - prefer bounty_amount if funded, otherwise show target_bounty
            bounty_raw = issue.get('bountyAmount', issue.get('bounty_amount', 0))
            target_raw = issue.get('targetBounty', issue.get('target_bounty', 0))
            status = issue.get('status', 'unknown')

            # Parse bounty - might be string with decimals or numeric
            try:
                bounty = float(bounty_raw) if bounty_raw else 0.0
                target = float(target_raw) if target_raw else 0.0
                # Always convert from smallest units (9 decimals) to ALPHA
                bounty = bounty / 1_000_000_000
                target = target / 1_000_000_000
            except (ValueError, TypeError):
                bounty = 0.0
                target = 0.0

            # Format bounty pool display with fill percentage
            if target > 0:
                fill_pct = (bounty / target) * 100 if target > 0 else 0
                if fill_pct >= 100:
                    bounty_display = f'{bounty:.1f} (100%)'
                elif bounty > 0:
                    bounty_display = f'{bounty:.1f}/{target:.1f} ({fill_pct:.0f}%)'
                else:
                    bounty_display = f'0/{target:.1f} (0%)'
            else:
                bounty_display = f'{bounty:.2f}' if bounty > 0 else '0.00'

            # Format status (handle enum values)
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
        console.print('[dim]  gitt register issue --repo owner/repo --issue 1 --bounty 100[/dim]')


@view.command('issue-bounty-pool')
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def view_bounty_pool(rpc_url: str, contract: str, verbose: bool):
    """View total bounty pool (sum of all issue bounty amounts)."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        total_bounty_pool = sum(issue.get('bounty_amount', 0) for issue in issues)
        console.print(f'[green]Issue Bounty Pool:[/green] {total_bounty_pool / 1e9:.4f} ALPHA ({total_bounty_pool} raw)')
        console.print(f'[dim]Sum of bounty amounts from {len(issues)} issue(s)[/dim]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@view.command('issue-pending-harvest')
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def view_pending_harvest(rpc_url: str, contract: str, verbose: bool):
    """View pending harvest (treasury stake minus allocated bounties)."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from substrateinterface import SubstrateInterface
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

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


@view.command('issue')
@click.argument('issue_id', type=int)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def view_issue(issue_id: int, rpc_url: str, contract: str, verbose: bool):
    """View raw issue data from contract."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Reading issue {issue_id}...[/dim]\n')

    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        issue = next((i for i in issues if i['id'] == issue_id), None)

        if issue:
            console.print(Panel(
                f'[cyan]ID:[/cyan] {issue["id"]}\n'
                f'[cyan]Repository:[/cyan] {issue["repository_full_name"]}\n'
                f'[cyan]Issue Number:[/cyan] #{issue["issue_number"]}\n'
                f'[cyan]Bounty Amount:[/cyan] {issue["bounty_amount"] / 1e9:.4f} ALPHA\n'
                f'[cyan]Target Bounty:[/cyan] {issue["target_bounty"] / 1e9:.4f} ALPHA\n'
                f'[cyan]Fill %:[/cyan] {(issue["bounty_amount"] / issue["target_bounty"] * 100) if issue["target_bounty"] > 0 else 0:.1f}%\n'
                f'[cyan]Status:[/cyan] {issue["status"]}',
                title=f'Issue #{issue_id}',
                border_style='green',
            ))
        else:
            console.print(f'[yellow]Issue {issue_id} not found.[/yellow]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@view.command('issue-contract-config')
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def view_issue_contract_config(rpc_url: str, contract: str, verbose: bool):
    """View contract configuration."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Reading config...[/dim]\n')

    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        packed = _read_contract_packed_storage(substrate, contract_addr, verbose)

        if packed:
            console.print(Panel(
                f'[cyan]Owner:[/cyan] {packed.get("owner", "N/A")}\n'
                f'[cyan]Treasury Hotkey:[/cyan] {packed.get("treasury_hotkey", "N/A")}\n'
                f'[cyan]Netuid:[/cyan] {packed.get("netuid", "N/A")}\n'
                f'[cyan]Next Issue ID:[/cyan] {packed.get("next_issue_id", "N/A")}',
                title='Contract Configuration (v0)',
                border_style='green',
            ))
        else:
            console.print('[yellow]Could not read contract configuration.[/yellow]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')
