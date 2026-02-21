# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
PR submission and prediction commands for issue bounties.

Commands:
    gitt issues submissions --id <N>    List open PRs for an issue
    gitt issues predict --id <N>        Submit a prediction on which PR will solve an issue
"""

import json as json_mod
import os
from typing import Any, Dict, List, Optional

import click
from rich.panel import Panel
from rich.table import Table

from .helpers import (
    _is_interactive,
    console,
    get_contract_address,
    print_network_header,
    read_issues_from_contract,
    read_netuid_from_contract,
    resolve_network,
    validate_issue_id,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _find_issue_by_id(issues: List[Dict[str, Any]], issue_id: int) -> Optional[Dict[str, Any]]:
    """Find an issue by its on-chain ID from a list of issues."""
    return next((i for i in issues if i['id'] == issue_id), None)


def _fetch_issue(
    issue_id: int,
    ws_endpoint: str,
    contract_addr: str,
    verbose: bool,
    *,
    require_active: bool = False,
) -> Dict[str, Any]:
    """Read an issue from the contract and validate existence and status.

    Args:
        require_active: If True, raise ClickException when status is not Active.
            If False, warn when status is not Active or Registered.
    """
    with console.status('[bold cyan]Reading issues from contract...', spinner='dots'):
        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

    issue = _find_issue_by_id(issues, issue_id)
    if not issue:
        raise click.ClickException(f'Issue {issue_id} not found on contract.')

    status = issue.get('status', '')
    if require_active and status != 'Active':
        raise click.ClickException(f'Issue {issue_id} has status "{status}" — predictions require Active status.')
    elif not require_active and status not in ('Active', 'Registered'):
        console.print(f'[yellow]Warning: Issue {issue_id} has status "{status}".[/yellow]')

    return issue


def _build_pr_table(prs: List[Dict[str, Any]]) -> Table:
    """Build a Rich table displaying PR information."""
    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('PR #', style='cyan', justify='right')
    table.add_column('Title', style='green', max_width=50)
    table.add_column('Author', style='yellow')
    table.add_column('Created', style='dim')
    table.add_column('Review', justify='center')
    table.add_column('URL', style='dim')

    for pr in prs:
        created = pr.get('created_at', '')[:10]  # YYYY-MM-DD
        review = pr.get('review_status') or '-'
        if review == 'APPROVED':
            review_style = '[green]APPROVED[/green]'
        elif review == 'CHANGES_REQUESTED':
            review_style = '[red]CHANGES[/red]'
        else:
            review_style = f'[dim]{review}[/dim]'

        table.add_row(
            str(pr['number']),
            pr.get('title', ''),
            pr.get('author', 'unknown'),
            created,
            review_style,
            pr.get('url', ''),
        )

    return table


def _format_pred_lines(predictions: Dict[int, float]) -> str:
    """Format predictions as display lines for Rich panels."""
    return '\n'.join(f'  PR #{k}: {v:.2%}' for k, v in predictions.items())


def _load_bittensor():
    """Lazy-import bittensor to avoid heavy import at module level."""
    import bittensor as bt

    return bt


def _load_find_prs():
    """Lazy-import find_prs_for_issue."""
    from gittensor.utils.github_api_tools import find_prs_for_issue

    return find_prs_for_issue


# ---------------------------------------------------------------------------
# Prediction collection and validation
# ---------------------------------------------------------------------------


def _parse_json_predictions(json_input: str) -> Dict[int, float]:
    """Parse predictions from a JSON string mapping PR numbers to probabilities."""
    try:
        raw = json_mod.loads(json_input)
    except json_mod.JSONDecodeError as e:
        raise click.ClickException(f'Invalid JSON input: {e}')

    if not isinstance(raw, dict):
        raise click.ClickException('--json-input must be a JSON object mapping PR numbers to probabilities.')

    predictions: Dict[int, float] = {}
    for k, v in raw.items():
        try:
            pr_num = int(k)
        except ValueError:
            raise click.ClickException(f'Invalid PR number in JSON: {k}')
        try:
            prob = float(v)
        except (ValueError, TypeError):
            raise click.ClickException(f'Invalid probability for PR {k}: {v}')
        predictions[pr_num] = prob
    return predictions


def _collect_interactive_predictions(
    open_prs: List[Dict[str, Any]],
    issue_id: int,
    repo: str,
    issue_number_gh: int,
) -> Dict[int, float]:
    """Collect predictions interactively via TTY prompts."""
    if not _is_interactive():
        raise click.ClickException(
            'Interactive mode requires a TTY. Use --pr/--probability or --json-input in scripts.'
        )

    if not open_prs:
        raise click.ClickException(f'No open PRs found for issue {issue_id} — nothing to predict on.')

    console.print(f'\n[bold cyan]Open PRs for Issue #{issue_id}[/bold cyan] ({repo}#{issue_number_gh})\n')
    console.print(_build_pr_table(open_prs))
    console.print()

    predictions: Dict[int, float] = {}
    running_sum = 0.0
    while True:
        console.print(f'[dim]Probability budget remaining: {1.0 - running_sum:.2f}[/dim]')
        pr_input = click.prompt('PR number (or "done" to finish)', type=str, default='done')
        if pr_input.strip().lower() == 'done':
            break

        try:
            input_pr = int(pr_input)
        except ValueError:
            console.print('[red]Please enter a valid PR number.[/red]')
            continue

        prob_input = click.prompt(f'Probability for PR #{input_pr}', type=float)

        if prob_input < 0.0 or prob_input > 1.0:
            console.print('[red]Probability must be between 0.0 and 1.0.[/red]')
            continue

        if running_sum + prob_input > 1.0:
            console.print(f'[red]Sum would exceed 1.0 ({running_sum + prob_input:.2f}). Try a lower value.[/red]')
            continue

        predictions[input_pr] = prob_input
        running_sum += prob_input

        if running_sum >= 0.9:
            console.print(f'[yellow]Warning: Total probability is now {running_sum:.2f}[/yellow]')

    return predictions


def _collect_predictions(
    pr_number: Optional[int],
    probability: Optional[float],
    json_input: Optional[str],
    open_prs: List[Dict[str, Any]],
    issue_id: int,
    repo: str,
    issue_number_gh: int,
) -> Dict[int, float]:
    """Collect predictions from one of three input modes.

    Modes:
        1. --json-input: parse JSON dict
        2. --pr + --probability: single prediction
        3. Interactive: prompt user for pairs
    """
    if json_input is not None:
        return _parse_json_predictions(json_input)

    if pr_number is not None:
        if probability is None:
            raise click.ClickException('--probability is required when using --pr.')
        return {pr_number: probability}

    if probability is not None:
        raise click.ClickException('--pr is required when using --probability.')

    return _collect_interactive_predictions(open_prs, issue_id, repo, issue_number_gh)


def _validate_predictions(predictions: Dict[int, float], open_pr_numbers: set) -> None:
    """Validate that all predictions have valid probabilities and reference open PRs."""
    for pr_num, prob in predictions.items():
        if prob < 0.0 or prob > 1.0:
            raise click.ClickException(f'Probability for PR #{pr_num} must be between 0.0 and 1.0 (got {prob}).')
        if pr_num not in open_pr_numbers:
            raise click.ClickException(
                f'PR #{pr_num} is not an open PR for this issue. '
                f'Open PRs: {sorted(open_pr_numbers) if open_pr_numbers else "none"}'
            )

    total_prob = sum(predictions.values())
    if total_prob > 1.0:
        raise click.ClickException(f'Sum of probabilities ({total_prob:.4f}) exceeds 1.0.')


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.command('submissions')
@click.option(
    '--id',
    'issue_id',
    required=True,
    type=int,
    help='On-chain issue ID to view submissions for',
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
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON for scripting')
def issues_submissions(issue_id: int, network: str, rpc_url: str, contract: str, verbose: bool, as_json: bool):
    """List open pull requests for an issue bounty.

    Shows PRs that reference the issue, filtered to open PRs only.
    Uses GITTENSOR_MINER_PAT for authenticated GitHub API access (optional).

    \b
    Examples:
        gitt issues submissions --id 1
        gitt i submissions --id 1 --json
        gitt i submissions --id 1 --network test
    """
    find_prs_for_issue = _load_find_prs()

    # Validate issue ID
    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured. Set via: gitt config set contract_address <ADDR>.')

    if not as_json:
        print_network_header(network_name, contract_addr)

    # Read and validate issue
    issue = _fetch_issue(issue_id, ws_endpoint, contract_addr, verbose)
    repo = issue['repository_full_name']
    issue_number = issue['issue_number']

    # Get GitHub PAT (optional for submissions)
    token = os.environ.get('GITTENSOR_MINER_PAT')
    if not token and not as_json:
        console.print(
            '[yellow]Warning: GITTENSOR_MINER_PAT not set — using unauthenticated API (lower rate limits).[/yellow]'
        )

    # Fetch open PRs
    with console.status('[bold cyan]Fetching open PRs...', spinner='dots'):
        prs = find_prs_for_issue(repo, issue_number, token=token, state_filter='open')

    if as_json:
        # Strip internal fields from JSON output
        output = [{k: v for k, v in pr.items() if k != 'author_database_id'} for pr in prs]
        console.print(json_mod.dumps(output, indent=2, default=str))
        return

    console.print(f'[bold cyan]Open PRs for Issue #{issue_id}[/bold cyan] ({repo}#{issue_number})\n')

    if not prs:
        console.print('[yellow]No open PRs found for this issue.[/yellow]')
        console.print(f'[dim]GitHub issue: https://github.com/{repo}/issues/{issue_number}[/dim]')
        return

    table = _build_pr_table(prs)
    console.print(table)
    console.print(f'\n[dim]Showing {len(prs)} open PR(s)[/dim]')


@click.command('predict')
@click.option(
    '--id',
    'issue_id',
    required=True,
    type=int,
    help='On-chain issue ID to predict for',
)
@click.option(
    '--pr',
    'pr_number',
    default=None,
    type=int,
    help='PR number to predict (use with --probability)',
)
@click.option(
    '--probability',
    default=None,
    type=float,
    help='Probability for the PR (0.0 to 1.0, use with --pr)',
)
@click.option(
    '--json-input',
    'json_input',
    default=None,
    type=str,
    help='JSON dict of predictions: \'{"101": 0.85, "102": 0.15}\'',
)
@click.option(
    '--yes',
    '-y',
    is_flag=True,
    help='Skip confirmation prompt',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
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
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON for scripting')
def issues_predict(
    issue_id: int,
    pr_number: Optional[int],
    probability: Optional[float],
    json_input: Optional[str],
    yes: bool,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    verbose: bool,
    as_json: bool,
):
    """Submit a prediction on which PR will solve an issue bounty.

    Predictions assign probabilities (0.0-1.0) to open PRs. The sum of
    all probabilities for an issue must not exceed 1.0.

    Three input modes:
      1. --pr N --probability F  (single prediction)
      2. --json-input '{"101": 0.85}'  (batch predictions)
      3. Interactive prompt (default, requires TTY)

    \b
    Examples:
        gitt issues predict --id 1 --pr 123 --probability 0.7 -y
        gitt issues predict --id 1 --json-input '{"123": 0.5, "456": 0.3}' -y
        gitt issues predict --id 1
    """
    bt = _load_bittensor()
    find_prs_for_issue = _load_find_prs()

    # Validate issue ID
    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    # --- Load miner identity ---
    token = os.environ.get('GITTENSOR_MINER_PAT')
    if not token:
        raise click.ClickException('GITTENSOR_MINER_PAT environment variable is required for predict.')

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured. Set via: gitt config set contract_address <ADDR>.')

    if not as_json:
        print_network_header(network_name, contract_addr)

    # Load wallet and verify registration
    try:
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        hotkey_addr = wallet.hotkey.ss58_address
    except Exception as e:
        raise click.ClickException(f'Failed to load wallet {wallet_name}/{wallet_hotkey}: {e}')

    with console.status('[bold cyan]Connecting to network...', spinner='dots'):
        try:
            subtensor = bt.Subtensor(network=ws_endpoint)
            netuid = read_netuid_from_contract(ws_endpoint, contract_addr, verbose)
            metagraph = subtensor.metagraph(netuid=netuid)
        except Exception as e:
            raise click.ClickException(f'Failed to connect to network: {e}')

    if hotkey_addr not in metagraph.hotkeys:
        raise click.ClickException(
            f'Hotkey {hotkey_addr} is not registered on the metagraph. '
            f'Register your miner before submitting predictions.'
        )

    if not as_json:
        console.print(f'[dim]Miner hotkey: {hotkey_addr}[/dim]')

    # --- Fetch context ---
    issue = _fetch_issue(issue_id, ws_endpoint, contract_addr, verbose, require_active=True)
    repo = issue['repository_full_name']
    issue_number_gh = issue['issue_number']

    with console.status('[bold cyan]Fetching open PRs...', spinner='dots'):
        open_prs = find_prs_for_issue(repo, issue_number_gh, token=token, state_filter='open')

    open_pr_numbers = {pr['number'] for pr in open_prs}

    # --- Collect and validate predictions ---
    predictions = _collect_predictions(pr_number, probability, json_input, open_prs, issue_id, repo, issue_number_gh)

    if not predictions:
        raise click.ClickException('No predictions provided.')

    _validate_predictions(predictions, open_pr_numbers)

    # --- Build payload ---
    total_prob = sum(predictions.values())
    payload = {
        'issue_id': issue_id,
        'repository': repo,
        'issue_number': issue_number_gh,
        'miner_hotkey': hotkey_addr,
        'predictions': {str(k): v for k, v in predictions.items()},
    }

    # --- Confirmation ---
    skip_confirm = yes or not _is_interactive()
    if not skip_confirm:
        pred_lines = _format_pred_lines(predictions)
        console.print(
            Panel(
                f'[cyan]Issue:[/cyan] #{issue_id} ({repo}#{issue_number_gh})\n'
                f'[cyan]Miner:[/cyan] {hotkey_addr}\n'
                f'[cyan]Predictions:[/cyan]\n{pred_lines}\n'
                f'[cyan]Total probability:[/cyan] {total_prob:.2%}',
                title='Prediction Summary',
                border_style='blue',
            )
        )

        if not click.confirm('\nSubmit this prediction?', default=True):
            console.print('[yellow]Prediction cancelled.[/yellow]')
            return

    # --- Stub broadcast ---
    # TODO: Broadcast prediction via synapse when the prediction protocol is implemented.
    # For now, we validate everything locally and display the payload.

    if as_json:
        console.print(json_mod.dumps(payload, indent=2, default=str))
        return

    pred_lines = _format_pred_lines(predictions)
    console.print(
        Panel(
            f'[green]Prediction recorded (local-only)[/green]\n\n'
            f'[cyan]Issue:[/cyan] #{issue_id} ({repo}#{issue_number_gh})\n'
            f'[cyan]Miner:[/cyan] {hotkey_addr}\n'
            f'[cyan]Predictions:[/cyan]\n{pred_lines}\n'
            f'[cyan]Total:[/cyan] {total_prob:.2%}',
            title='Prediction Submitted',
            border_style='green',
        )
    )
    console.print('[dim]Note: Network broadcast is not yet implemented (TODO).[/dim]')
