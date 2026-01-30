# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
View subgroup commands for issue CLI.

Commands:
    gitt issue view issues
    gitt issue view bounty-pool
    gitt issue view pending-harvest
    gitt issue view issue <id>
    gitt issue view competition <id>
    gitt issue view competition-proposal <id>
    gitt issue view config
    gitt issue view active-competitions
    gitt issue view status
    gitt issue view elo
    gitt issue view competitions
    gitt issue view leaderboard
"""

import json
import urllib.request
import urllib.error
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .helpers import (
    console,
    load_preferences,
    load_contract_config,
    get_contract_address,
    get_ws_endpoint,
    get_api_url,
    read_issues_from_contract,
    _read_contract_packed_storage,
    _read_issues_from_child_storage,
    DEFAULT_API_URL,
    ISSUE_PREFERENCES_FILE,
)


@click.group(name='view')
def view():
    """View contract state and API data (read-only commands).

    \b
    Contract reads:
        issues              List issues by status
        bounty-pool         View current alpha pool balance
        pending-harvest     View pending emissions value
        issue <id>          View raw issue data
        competition <id>    View competition details
        competition-proposal <id>  View proposal state
        config              View contract configuration
        active-competitions List all active competitions

    \b
    API reads:
        status              View your competition status
        elo                 View your ELO rating
        competitions        View all competitions from API
        leaderboard         View ELO leaderboard
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
    List available issues for competition.

    Shows all issues with status=Active that have funded bounties
    and are available for competition.

    By default, reads directly from the smart contract (no API dependency).
    Use --from-api to read from the API instead.

    \b
    Example:
        gitt issue view issues
        gitt i v issues --testnet
    """
    console.print('\n[bold cyan]Available Issues for Competition[/bold cyan]\n')

    # Load configuration
    config = load_contract_config()
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
        console.print('[dim]  gitt issue register --repo owner/repo --issue 1 --bounty 100[/dim]')

    console.print('\n[dim]Use "gitt issue prefer <id1> <id2> ..." to set preferences[/dim]')


@view.command('bounty-pool')
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
    """View current alpha pool balance."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        packed = _read_contract_packed_storage(substrate, contract_addr, verbose)

        if packed:
            alpha_pool = packed.get('alpha_pool', 0)
            console.print(f'[green]Alpha Pool:[/green] {alpha_pool / 1e9:.4f} ALPHA ({alpha_pool} raw)')
        else:
            console.print('[yellow]Could not read contract storage.[/yellow]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@view.command('pending-harvest')
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
    """View pending emissions value (current stake on treasury)."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        pending = client.get_treasury_stake()
        console.print(f'[green]Treasury Stake:[/green] {pending / 1e9:.4f} ALPHA')
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


@view.command('competition')
@click.argument('competition_id', type=int)
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
def view_competition(competition_id: int, rpc_url: str, contract: str, verbose: bool):
    """View competition details from contract."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Reading competition {competition_id}...[/dim]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        comp = client.get_competition(competition_id)

        if comp:
            # Handle dataclass return type
            payout = comp.payout_amount if comp.payout_amount else 0
            winner = comp.winner_hotkey if comp.winner_hotkey else 'None'
            console.print(Panel(
                f'[cyan]ID:[/cyan] {comp.id}\n'
                f'[cyan]Issue ID:[/cyan] {comp.issue_id}\n'
                f'[cyan]Miner 1:[/cyan] {comp.miner1_hotkey}\n'
                f'[cyan]Miner 2:[/cyan] {comp.miner2_hotkey}\n'
                f'[cyan]Status:[/cyan] {comp.status.name}\n'
                f'[cyan]Start Block:[/cyan] {comp.start_block}\n'
                f'[cyan]Deadline Block:[/cyan] {comp.deadline_block}\n'
                f'[cyan]Winner:[/cyan] {winner}\n'
                f'[cyan]Payout Amount:[/cyan] {payout / 1e9:.4f} ALPHA',
                title=f'Competition #{competition_id}',
                border_style='green',
            ))
        else:
            console.print(f'[yellow]Competition {competition_id} not found.[/yellow]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@view.command('competition-proposal')
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
def view_competition_proposal(issue_id: int, rpc_url: str, contract: str, verbose: bool):
    """View competition proposal state for an issue."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Reading proposal for issue {issue_id}...[/dim]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        proposal = client.get_competition_proposal(issue_id)

        if proposal:
            # Handle dataclass return type
            console.print(Panel(
                f'[cyan]Issue ID:[/cyan] {proposal.issue_id}\n'
                f'[cyan]Miner 1:[/cyan] {proposal.miner1_hotkey}\n'
                f'[cyan]Miner 2:[/cyan] {proposal.miner2_hotkey}\n'
                f'[cyan]Proposer:[/cyan] {proposal.proposer}\n'
                f'[cyan]Proposed At Block:[/cyan] {proposal.proposed_at_block}\n'
                f'[cyan]Total Stake Voted:[/cyan] {proposal.total_stake_voted / 1e9:.4f}',
                title=f'Competition Proposal for Issue #{issue_id}',
                border_style='yellow',
            ))
        else:
            console.print(f'[yellow]No active proposal for issue {issue_id}.[/yellow]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@view.command('config')
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
def view_config(rpc_url: str, contract: str, verbose: bool):
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
                f'[cyan]Validator Hotkey:[/cyan] {packed.get("validator_hotkey", "N/A")}\n'
                f'[cyan]Netuid:[/cyan] {packed.get("netuid", "N/A")}\n'
                f'[cyan]Submission Window (blocks):[/cyan] {packed.get("submission_window_blocks", "N/A")}\n'
                f'[cyan]Competition Deadline (blocks):[/cyan] {packed.get("competition_deadline_blocks", "N/A")}\n'
                f'[cyan]Proposal Expiry (blocks):[/cyan] {packed.get("proposal_expiry_blocks", "N/A")}\n'
                f'[cyan]Next Issue ID:[/cyan] {packed.get("next_issue_id", "N/A")}\n'
                f'[cyan]Next Competition ID:[/cyan] {packed.get("next_competition_id", "N/A")}',
                title='Contract Configuration',
                border_style='green',
            ))
        else:
            console.print('[yellow]Could not read contract configuration.[/yellow]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@view.command('active-competitions')
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
def view_active_competitions(rpc_url: str, contract: str, verbose: bool):
    """List all active competitions from contract."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print('[dim]Reading active competitions...[/dim]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        competitions = client.get_active_competitions()

        if competitions:
            table = Table(show_header=True, header_style='bold magenta')
            table.add_column('ID', style='cyan', justify='right')
            table.add_column('Issue ID', style='green', justify='right')
            table.add_column('Miner 1', style='yellow')
            table.add_column('Miner 2', style='yellow')
            table.add_column('Status', style='blue')
            table.add_column('Deadline', style='magenta', justify='right')

            for comp in competitions:
                # Handle dataclass return type
                table.add_row(
                    str(comp.id),
                    str(comp.issue_id),
                    comp.miner1_hotkey[:12] + '...',
                    comp.miner2_hotkey[:12] + '...',
                    comp.status.name,
                    str(comp.deadline_block),
                )

            console.print(table)
            console.print(f'\n[dim]Found {len(competitions)} active competition(s)[/dim]')
        else:
            console.print('[dim]No active competitions found.[/dim]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@view.command('status')
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL',
)
def view_status(wallet_name: str, wallet_hotkey: str, api_url: str):
    """
    View your current competition status.

    Shows:
    - Local preferences (what you've enrolled for)
    - Active competition (if you're currently competing)
    - Competition details (opponent, deadline, bounty)

    \b
    Example:
        gitt issue view status
        gitt i v status --wallet-name mywallet
    """
    console.print('\n[bold cyan]Issue Competition Status[/bold cyan]\n')

    # Show local preferences
    preferences = load_preferences()
    if preferences:
        console.print(Panel(
            f'[cyan]Preferred Issues:[/cyan] {preferences}\n'
            '[dim]Status: Waiting for validator pairing...[/dim]',
            title='Local Preferences',
            border_style='blue',
        ))
    else:
        console.print(Panel(
            '[yellow]No preferences set.[/yellow]\n'
            '[dim]Use "gitt issue prefer" to join competitions.[/dim]',
            title='Local Preferences',
            border_style='yellow',
        ))

    # Query API for active competition status
    console.print('\n[dim]Checking for active competitions...[/dim]')

    resolved_api_url = get_api_url(api_url)
    try:
        req = urllib.request.Request(f'{resolved_api_url}/competitions/active')
        with urllib.request.urlopen(req, timeout=5) as resp:
            competitions = json.loads(resp.read().decode())
            if competitions:
                for comp in competitions[:3]:  # Show up to 3 active competitions
                    comp_panel = Panel(
                        f'[green]Competition ID:[/green] {comp.get("id", "?")}\n'
                        f'[green]Issue:[/green] {comp.get("repository_full_name", "?")}#{comp.get("issue_number", "?")}\n'
                        f'[green]Bounty:[/green] {comp.get("bounty_amount", 0) / 1e9:.2f} ALPHA\n'
                        f'[green]Miner 1:[/green] {comp.get("miner1_hotkey", "?")[:12]}...\n'
                        f'[green]Miner 2:[/green] {comp.get("miner2_hotkey", "?")[:12]}...\n'
                        f'[green]Status:[/green] {comp.get("status", "Unknown")}',
                        title='Active Competition',
                        border_style='green',
                    )
                    console.print(comp_panel)
            else:
                console.print('[dim]No active competitions found.[/dim]')
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        console.print(f'[red]Error: Cannot connect to API at {resolved_api_url}[/red]')
        console.print('[dim]Ensure the API is running: cd das-gittensor && npm run start:dev[/dim]')


@view.command('elo')
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL for ELO lookup',
)
def view_elo(wallet_name: str, wallet_hotkey: str, api_url: str):
    """
    View your ELO rating and competition history.

    Shows your current ELO rating, win/loss record, and eligibility
    status for competitions.

    \b
    Example:
        gitt issue view elo
        gitt i v elo --wallet-name mywallet
    """
    console.print('\n[bold cyan]ELO Rating[/bold cyan]\n')

    # Resolve API URL from CLI, env, or config
    resolved_api_url = get_api_url(api_url)

    # Get hotkey address for lookup
    try:
        import bittensor as bt
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        hotkey_address = wallet.hotkey.ss58_address
    except Exception:
        console.print('[red]Error: Cannot load wallet. Check wallet name and hotkey.[/red]')
        return

    # Query API for actual ELO rating
    try:
        req = urllib.request.Request(f'{resolved_api_url}/elo/{hotkey_address}')
        with urllib.request.urlopen(req, timeout=5) as resp:
            elo_data = json.loads(resp.read().decode())
            wins = elo_data.get('wins', 0)
            losses = elo_data.get('losses', 0)
            total = wins + losses
            win_rate = (wins / total * 100) if total > 0 else 0
            elo_score = elo_data.get('elo', 800)
            is_eligible = elo_score >= 700

            elo_panel = Panel(
                f'[bold green]Current ELO:[/bold green] {elo_score}\n'
                f'[green]Wins:[/green] {wins}\n'
                f'[green]Losses:[/green] {losses}\n'
                f'[green]Win Rate:[/green] {win_rate:.1f}%\n'
                f'[green]Eligible:[/green] {"Yes" if is_eligible else "No"} (ELO >= 700)',
                title='Your ELO Rating',
                border_style='green',
            )
            console.print(elo_panel)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            console.print('[yellow]No ELO record found. You have not participated in any competitions yet.[/yellow]')
            console.print('[dim]Your initial ELO will be 800 when you join your first competition.[/dim]')
        else:
            console.print(f'[red]Error: API returned status {e.code}[/red]')
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        console.print(f'[red]Error: Cannot connect to API at {resolved_api_url}[/red]')
        console.print('[dim]Ensure the API is running: cd das-gittensor && npm run start:dev[/dim]')
        return

    # ELO explanation
    console.print('\n[bold]ELO System Info:[/bold]')
    console.print('  - Initial rating: 800')
    console.print('  - Eligibility cutoff: 700 (3-4 consecutive losses)')
    console.print('  - K-factor: 40 (rating changes per match)')
    console.print('  - 30-day rolling EMA (older matches weighted less)')
    console.print('  - Inactivity: ELO decays toward 800 over 30 days')


@view.command('competitions')
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL',
)
@click.option('--limit', default=10, help='Maximum competitions to show')
def view_competitions(api_url: str, limit: int):
    """
    View all competitions from API.

    Shows competition history from the API.

    \b
    Example:
        gitt issue view competitions
        gitt i v competitions --limit 20
    """
    console.print('\n[bold cyan]Competitions[/bold cyan]\n')

    # Resolve API URL from CLI, env, or config
    resolved_api_url = get_api_url(api_url)

    # Query API for actual competitions
    try:
        req = urllib.request.Request(f'{resolved_api_url}/competitions?limit={limit}')
        with urllib.request.urlopen(req, timeout=5) as resp:
            competitions = json.loads(resp.read().decode())

            if not competitions:
                console.print('[dim]No competitions found.[/dim]')
                return

            table = Table(show_header=True, header_style='bold magenta')
            table.add_column('ID', style='cyan', justify='right')
            table.add_column('Issue', style='green')
            table.add_column('Miner 1', style='yellow')
            table.add_column('Miner 2', style='yellow')
            table.add_column('Bounty', style='magenta', justify='right')
            table.add_column('Status', style='blue')

            for comp in competitions:
                comp_id = str(comp.get('id', '?'))
                repo = comp.get('repository_full_name', '?')
                issue_num = comp.get('issue_number', '?')
                issue_ref = f'{repo.split("/")[-1] if "/" in repo else repo}#{issue_num}'
                m1 = comp.get('miner1_hotkey', '?')[:12] + '...'
                m2 = comp.get('miner2_hotkey', '?')[:12] + '...'
                bounty = f'{comp.get("bounty_amount", 0) / 1e9:.1f}'
                status = comp.get('status', 'Unknown')
                table.add_row(comp_id, issue_ref, m1, m2, bounty, status)

            console.print(table)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        console.print(f'[red]Error: Cannot connect to API at {resolved_api_url}[/red]')
        console.print('[dim]Ensure the API is running: cd das-gittensor && npm run start:dev[/dim]')


@view.command('leaderboard')
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL',
)
@click.option('--limit', default=10, help='Number of miners to show')
def view_leaderboard(api_url: str, limit: int):
    """
    View the ELO leaderboard.

    Shows top miners by ELO rating.

    \b
    Example:
        gitt issue view leaderboard
        gitt i v leaderboard --limit 25
    """
    console.print('\n[bold cyan]ELO Leaderboard[/bold cyan]\n')

    # Resolve API URL from CLI, env, or config
    resolved_api_url = get_api_url(api_url)

    # Query API for actual leaderboard
    try:
        req = urllib.request.Request(f'{resolved_api_url}/elo/leaderboard?limit={limit}')
        with urllib.request.urlopen(req, timeout=5) as resp:
            leaderboard = json.loads(resp.read().decode())

            if not leaderboard:
                console.print('[dim]No ELO data found. No competitions have been completed yet.[/dim]')
                return

            table = Table(show_header=True, header_style='bold magenta')
            table.add_column('Rank', style='cyan', justify='right')
            table.add_column('Miner', style='green')
            table.add_column('ELO', style='yellow', justify='right')
            table.add_column('W/L', style='magenta', justify='center')
            table.add_column('Win %', style='blue', justify='right')
            table.add_column('Eligible', style='green', justify='center')

            for i, entry in enumerate(leaderboard, 1):
                hotkey = entry.get('hotkey', '?')
                miner_display = hotkey[:12] + '...' if len(hotkey) > 12 else hotkey
                elo = entry.get('elo', 800)
                wins = entry.get('wins', 0)
                losses = entry.get('losses', 0)
                wl = f'{wins}/{losses}'
                total = wins + losses
                win_pct = f'{(wins / total * 100):.0f}%' if total > 0 else 'N/A'
                eligible = 'Yes' if elo >= 700 else 'No'
                table.add_row(str(i), miner_display, str(elo), wl, win_pct, eligible)

            console.print(table)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        console.print(f'[red]Error: Cannot connect to API at {resolved_api_url}[/red]')
        console.print('[dim]Ensure the API is running: cd das-gittensor && npm run start:dev[/dim]')
