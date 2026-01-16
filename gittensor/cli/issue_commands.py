# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for managing issue competition preferences.

Miners use these commands to:
- View available issues with bounties
- Set their ranked preferences for competitions
- Check their current competition status
- View their ELO rating and history
"""

import json
from pathlib import Path
from typing import List, Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# Default paths and URLs
GITTENSOR_DIR = Path.home() / '.gittensor'
ISSUE_PREFERENCES_FILE = GITTENSOR_DIR / 'issue_preferences.json'
DEFAULT_API_URL = 'https://api.gittensor.io'

console = Console()


def get_preferences_file() -> Path:
    """Get the path to the issue preferences file, creating directory if needed."""
    GITTENSOR_DIR.mkdir(parents=True, exist_ok=True)
    return ISSUE_PREFERENCES_FILE


def load_preferences() -> List[int]:
    """Load current issue preferences from local file."""
    prefs_file = get_preferences_file()
    if not prefs_file.exists():
        return []
    try:
        with open(prefs_file, 'r') as f:
            data = json.load(f)
            return data.get('preferences', [])[:5]  # Max 5
    except (json.JSONDecodeError, IOError):
        return []


def save_preferences(preferences: List[int]) -> bool:
    """Save issue preferences to local file."""
    prefs_file = get_preferences_file()
    try:
        with open(prefs_file, 'w') as f:
            json.dump({'preferences': preferences[:5]}, f, indent=2)
        return True
    except IOError as e:
        console.print(f'[red]Failed to save preferences: {e}[/red]')
        return False


def clear_preferences() -> bool:
    """Clear issue preferences by deleting the file."""
    prefs_file = get_preferences_file()
    if prefs_file.exists():
        try:
            prefs_file.unlink()
            return True
        except IOError as e:
            console.print(f'[red]Failed to clear preferences: {e}[/red]')
            return False
    return True


@click.group()
def issue():
    """Issue competition commands for miners.

    Manage your participation in head-to-head coding competitions
    on GitHub issues. Winners receive ALPHA token bounties.
    """
    pass


@issue.command('list')
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
def issue_list(rpc_url: str, contract: str, testnet: bool):
    """
    List available issues for competition.

    Shows all issues with status=Active that have funded bounties
    and are available for competition.

    \b
    Example:
        gittensor-cli issue list
        gittensor-cli issue list --testnet
    """
    console.print('\n[bold cyan]Available Issues for Competition[/bold cyan]\n')

    # TODO: Implement actual contract query when deployed
    # For now, show a placeholder message
    if not contract:
        console.print('[yellow]Contract not deployed yet. Showing example data:[/yellow]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='cyan', justify='right')
    table.add_column('Repository', style='green')
    table.add_column('Issue #', style='yellow', justify='right')
    table.add_column('Bounty (ALPHA)', style='magenta', justify='right')
    table.add_column('Status', style='blue')

    # Example data for demonstration
    example_issues = [
        (1, 'opentensor/bittensor', 1234, 50.0, 'Active'),
        (2, 'gittensor/gittensor', 567, 75.5, 'Active'),
        (3, 'anthropic/claude', 890, 100.0, 'Active'),
    ]

    for issue_id, repo, num, bounty, status in example_issues:
        table.add_row(
            str(issue_id),
            repo,
            f'#{num}',
            f'{bounty:.2f}',
            status,
        )

    console.print(table)
    console.print('\n[dim]Use "gittensor-cli issue prefer <id1> <id2> ..." to set preferences[/dim]')


@issue.command('prefer')
@click.argument('issue_ids', nargs=-1, type=int)
@click.option('--clear', is_flag=True, help='Clear existing preferences before adding')
def issue_prefer(issue_ids: tuple, clear: bool):
    """
    Set ranked issue preferences (most preferred first).

    Your preferences determine which issues you'll be matched on.
    Higher ELO miners get priority for their preferred issues.

    \b
    Arguments:
        ISSUE_IDS: Space-separated list of issue IDs in preference order

    \b
    Examples:
        gittensor-cli issue prefer 42 15 8
        gittensor-cli issue prefer 1 2 3 --clear
    """
    if clear:
        clear_preferences()

    if not issue_ids:
        current = load_preferences()
        if current:
            console.print(f'[cyan]Current preferences:[/cyan] {current}')
        else:
            console.print('[yellow]No preferences set. Provide issue IDs to set preferences.[/yellow]')
        console.print('\n[dim]Usage: gittensor-cli issue prefer <id1> <id2> ...[/dim]')
        return

    preferences = list(issue_ids)[:5]  # Max 5

    # Display preferences
    console.print('\n[bold]Your Ranked Preferences:[/bold]')
    for i, issue_id in enumerate(preferences, 1):
        console.print(f'  {i}. Issue #{issue_id}')

    if len(issue_ids) > 5:
        console.print(f'\n[yellow]Note: Only first 5 preferences saved (you provided {len(issue_ids)})[/yellow]')

    # Confirm and save
    if click.confirm('\nSave these preferences?', default=True):
        if save_preferences(preferences):
            console.print(f'[green]Preferences saved to {ISSUE_PREFERENCES_FILE}[/green]')
            console.print('[dim]Your miner will automatically serve these to validators.[/dim]')
            console.print('[dim]You will be assigned based on ELO priority when pairs are formed.[/dim]')
        else:
            console.print('[red]Failed to save preferences.[/red]')


@issue.command('enroll')
@click.argument('issue_id', type=int)
def issue_enroll(issue_id: int):
    """
    Quick enroll for a single issue (shorthand for prefer).

    This is equivalent to running "prefer" with a single issue ID.

    \b
    Arguments:
        ISSUE_ID: The issue ID to enroll for

    \b
    Example:
        gittensor-cli issue enroll 42
    """
    current = load_preferences()
    if issue_id in current:
        console.print(f'[yellow]Already enrolled for issue #{issue_id}[/yellow]')
        console.print(f'Current preferences: {current}')
        return

    # Add to front of preferences
    new_prefs = [issue_id] + [p for p in current if p != issue_id][:4]

    if save_preferences(new_prefs):
        console.print(f'[green]Enrolled for issue #{issue_id}[/green]')
        console.print(f'New preferences: {new_prefs}')
    else:
        console.print('[red]Failed to enroll.[/red]')


@issue.command('status')
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
def issue_status(wallet_name: str, wallet_hotkey: str, api_url: str):
    """
    View your current competition status.

    Shows:
    - Local preferences (what you've enrolled for)
    - Active competition (if you're currently competing)
    - Competition details (opponent, deadline, bounty)

    \b
    Example:
        gittensor-cli issue status
        gittensor-cli issue status --wallet-name mywallet
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
            '[dim]Use "gittensor-cli issue prefer" to join competitions.[/dim]',
            title='Local Preferences',
            border_style='yellow',
        ))

    # TODO: Query API for active competition status
    console.print('\n[dim]Checking for active competitions...[/dim]')
    console.print('[yellow]API not available - contract not deployed yet.[/yellow]')

    # Show example active competition
    console.print('\n[bold]Example Active Competition:[/bold]')
    example_comp = Panel(
        '[green]Competition ID:[/green] 123\n'
        '[green]Issue:[/green] opentensor/bittensor#1234\n'
        '[green]Bounty:[/green] 50.00 ALPHA\n'
        '[green]Opponent:[/green] 5Abc...xyz\n'
        '[green]Deadline:[/green] Block 12345678 (~2 days remaining)\n'
        '[green]Submission Window Ends:[/green] Block 12340000',
        title='Active Competition (Example)',
        border_style='green',
    )
    console.print(example_comp)


@issue.command('withdraw')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation prompt')
def issue_withdraw(force: bool):
    """
    Clear issue preferences (stop competing for new issues).

    This removes your local preferences file. You will no longer
    be matched for new competitions.

    NOTE: You cannot withdraw from an active competition once started.

    \b
    Example:
        gittensor-cli issue withdraw
        gittensor-cli issue withdraw --force
    """
    preferences = load_preferences()

    if not preferences:
        console.print('[yellow]No preferences to clear.[/yellow]')
        return

    console.print(f'[cyan]Current preferences:[/cyan] {preferences}')

    if force or click.confirm('\nClear all issue preferences?', default=False):
        if clear_preferences():
            console.print('[green]Preferences cleared.[/green]')
            console.print('[dim]You will not be matched for new competitions.[/dim]')
        else:
            console.print('[red]Failed to clear preferences.[/red]')


@issue.command('elo')
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
def issue_elo(wallet_name: str, wallet_hotkey: str, api_url: str):
    """
    View your ELO rating and competition history.

    Shows your current ELO rating, win/loss record, and eligibility
    status for competitions.

    ELO System:
    - Initial rating: 800
    - Cutoff for eligibility: 700
    - Uses 30-day rolling EMA

    \b
    Example:
        gittensor-cli issue elo
        gittensor-cli issue elo --wallet-name mywallet
    """
    console.print('\n[bold cyan]ELO Rating[/bold cyan]\n')

    # TODO: Query API for actual ELO rating
    console.print('[yellow]API not available - showing example data.[/yellow]\n')

    # Show example ELO data
    elo_panel = Panel(
        '[bold green]Current ELO:[/bold green] 850\n'
        '[green]Wins:[/green] 5\n'
        '[green]Losses:[/green] 2\n'
        '[green]Win Rate:[/green] 71.4%\n'
        '[green]Last Competition:[/green] 2 days ago\n'
        '[green]Eligible:[/green] Yes (ELO >= 700)',
        title='Your ELO Rating (Example)',
        border_style='green',
    )
    console.print(elo_panel)

    # ELO explanation
    console.print('\n[bold]ELO System Info:[/bold]')
    console.print('  - Initial rating: 800')
    console.print('  - Eligibility cutoff: 700 (3-4 consecutive losses)')
    console.print('  - K-factor: 40 (rating changes per match)')
    console.print('  - 30-day rolling EMA (older matches weighted less)')
    console.print('  - Inactivity: ELO decays toward 800 over 30 days')


@issue.command('competitions')
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL',
)
@click.option('--limit', default=10, help='Maximum competitions to show')
def issue_competitions(api_url: str, limit: int):
    """
    View all active competitions.

    Shows current head-to-head competitions across the network.

    \b
    Example:
        gittensor-cli issue competitions
        gittensor-cli issue competitions --limit 20
    """
    console.print('\n[bold cyan]Active Competitions[/bold cyan]\n')

    # TODO: Query API for actual competitions
    console.print('[yellow]API not available - showing example data.[/yellow]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='cyan', justify='right')
    table.add_column('Issue', style='green')
    table.add_column('Miner 1', style='yellow')
    table.add_column('Miner 2', style='yellow')
    table.add_column('Bounty', style='magenta', justify='right')
    table.add_column('Deadline', style='blue')

    # Example data
    example_comps = [
        (1, 'bittensor#1234', '5Abc...def', '5Xyz...uvw', '50.0', '~2d'),
        (2, 'gittensor#567', '5Ghi...jkl', '5Mno...pqr', '75.5', '~5d'),
    ]

    for comp_id, issue_ref, m1, m2, bounty, deadline in example_comps:
        table.add_row(str(comp_id), issue_ref, m1, m2, bounty, deadline)

    console.print(table)


@issue.command('leaderboard')
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL',
)
@click.option('--limit', default=10, help='Number of miners to show')
def issue_leaderboard(api_url: str, limit: int):
    """
    View the ELO leaderboard.

    Shows top miners by ELO rating.

    \b
    Example:
        gittensor-cli issue leaderboard
        gittensor-cli issue leaderboard --limit 25
    """
    console.print('\n[bold cyan]ELO Leaderboard[/bold cyan]\n')

    # TODO: Query API for actual leaderboard
    console.print('[yellow]API not available - showing example data.[/yellow]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('Rank', style='cyan', justify='right')
    table.add_column('Miner', style='green')
    table.add_column('ELO', style='yellow', justify='right')
    table.add_column('W/L', style='magenta', justify='center')
    table.add_column('Win %', style='blue', justify='right')
    table.add_column('Eligible', style='green', justify='center')

    # Example data
    example_leaders = [
        (1, '5Abc...def', 1250, '15/3', '83%', 'Yes'),
        (2, '5Ghi...jkl', 1180, '12/4', '75%', 'Yes'),
        (3, '5Mno...pqr', 1050, '8/5', '62%', 'Yes'),
        (4, '5Stu...vwx', 920, '6/6', '50%', 'Yes'),
        (5, '5Yza...bcd', 850, '5/7', '42%', 'Yes'),
    ]

    for rank, miner, elo, wl, win_pct, eligible in example_leaders[:limit]:
        table.add_row(str(rank), miner, str(elo), wl, win_pct, eligible)

    console.print(table)


@issue.command('register')
@click.option(
    '--repo',
    required=True,
    help='Repository in owner/repo format (e.g., opentensor/btcli)',
)
@click.option(
    '--issue',
    'issue_number',
    required=True,
    type=int,
    help='GitHub issue number',
)
@click.option(
    '--bounty',
    required=True,
    type=float,
    help='Bounty amount in ALPHA tokens',
)
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
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name (must be contract owner)',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
def issue_register(
    repo: str,
    issue_number: int,
    bounty: float,
    rpc_url: str,
    contract: str,
    testnet: bool,
    wallet_name: str,
    wallet_hotkey: str,
):
    """
    Register a new issue with a bounty (OWNER ONLY).

    This command registers a GitHub issue on the smart contract
    with a target bounty amount. Only the contract owner can
    register new issues.

    \b
    Arguments:
        --repo: Repository in owner/repo format
        --issue: GitHub issue number
        --bounty: Target bounty amount in ALPHA

    \b
    Examples:
        gittensor-cli issue register --repo opentensor/btcli --issue 144 --bounty 100
        gittensor-cli issue register --repo tensorflow/tensorflow --issue 12345 --bounty 50 --testnet
    """
    console.print('\n[bold cyan]Register Issue for Competition[/bold cyan]\n')

    # Validate repo format
    if '/' not in repo:
        console.print('[red]Error: Repository must be in owner/repo format[/red]')
        return

    # Construct GitHub URL
    github_url = f'https://github.com/{repo}/issues/{issue_number}'

    # Display registration details
    console.print(Panel(
        f'[cyan]Repository:[/cyan] {repo}\n'
        f'[cyan]Issue Number:[/cyan] #{issue_number}\n'
        f'[cyan]GitHub URL:[/cyan] {github_url}\n'
        f'[cyan]Target Bounty:[/cyan] {bounty:.2f} ALPHA\n'
        f'[cyan]Network:[/cyan] {"Testnet" if testnet else "Mainnet"}',
        title='Issue Registration',
        border_style='blue',
    ))

    # TODO: Implement actual contract call when deployed
    if not contract:
        console.print('\n[yellow]Contract not deployed yet.[/yellow]')
        console.print('[dim]In production, this would call register_issue() on the contract.[/dim]')
        console.print('\n[bold]Mock Registration:[/bold]')
        console.print(f'  Issue ID: 1 (would be assigned by contract)')
        console.print(f'  Status: Registered (awaiting bounty fill)')
        console.print('\n[green]Issue would be registered successfully![/green]')
        return

    # When contract is deployed, this would do:
    # 1. Load wallet
    # 2. Connect to contract via substrate-interface
    # 3. Call register_issue(github_url, repo, issue_number, bounty_amount)
    # 4. Display transaction result

    if click.confirm('\nProceed with registration?', default=True):
        console.print('\n[yellow]Connecting to contract...[/yellow]')
        console.print('[red]Contract interaction not implemented yet.[/red]')
        console.print('[dim]This will be enabled when the contract is deployed.[/dim]')


def register_issue_commands(cli):
    """Register issue commands with a parent CLI group."""
    cli.add_command(issue)
