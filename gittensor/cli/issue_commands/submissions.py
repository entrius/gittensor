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
from typing import Optional

import click
from rich.panel import Panel

from gittensor.utils.github_api_tools import find_prs_for_issue

from .helpers import (
    _is_interactive,
    build_pr_table,
    collect_predictions,
    console,
    fetch_issue_from_contract,
    format_pred_lines,
    get_contract_address,
    print_network_header,
    read_netuid_from_contract,
    resolve_network,
    validate_issue_id,
    validate_predictions,
)


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
    issue = fetch_issue_from_contract(issue_id, ws_endpoint, contract_addr, verbose)
    repo = issue['repository_full_name']
    issue_number = issue['issue_number']

    # Get GitHub PAT (optional for submissions)
    token = os.environ.get('GITTENSOR_MINER_PAT') or None
    if not token and not as_json:
        console.print(
            '[yellow]Warning: GITTENSOR_MINER_PAT not set — using unauthenticated API (lower rate limits).[/yellow]'
        )

    # Fetch open PRs
    try:
        with console.status('[bold cyan]Fetching open PRs...', spinner='dots'):
            prs = find_prs_for_issue(repo, issue_number, token=token, state_filter='open')
    except Exception as e:
        if not as_json:
            console.print(f'[yellow]Warning: Could not fetch PRs from GitHub: {e}[/yellow]')
        prs = []

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

    table = build_pr_table(prs)
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
    # --- Phase 1: Cheap local validation (no network I/O) ---
    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    token = os.environ.get('GITTENSOR_MINER_PAT') or None
    if not token:
        raise click.ClickException('GITTENSOR_MINER_PAT environment variable is required for predict.')

    # Validate flag combinations before any network calls
    if pr_number is not None and json_input is not None:
        raise click.ClickException('Use either --pr/--probability or --json-input, not both.')
    if probability is not None and json_input is not None:
        raise click.ClickException('Use either --pr/--probability or --json-input, not both.')
    if pr_number is not None and probability is None and json_input is None:
        raise click.ClickException('--probability is required when using --pr.')
    if pr_number is None and probability is not None and json_input is None:
        raise click.ClickException('--pr is required when using --probability.')
    if probability is not None and not (0.0 <= probability <= 1.0):
        raise click.ClickException(f'Probability must be between 0.0 and 1.0 (got {probability}).')

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured. Set via: gitt config set contract_address <ADDR>.')

    if not as_json:
        print_network_header(network_name, contract_addr)

    # --- Phase 2: On-chain and GitHub reads (fail fast before wallet) ---
    issue = fetch_issue_from_contract(issue_id, ws_endpoint, contract_addr, verbose, require_active=True)
    repo = issue['repository_full_name']
    issue_number_gh = issue['issue_number']

    with console.status('[bold cyan]Fetching open PRs...', spinner='dots'):
        open_prs = find_prs_for_issue(repo, issue_number_gh, token=token, state_filter='open')

    open_pr_numbers = {pr['number'] for pr in open_prs}

    # --- Phase 3: Collect and validate predictions ---
    predictions = collect_predictions(
        pr_number=pr_number,
        probability=probability,
        json_input=json_input,
        open_prs=open_prs,
        issue_id=issue_id,
        repo=repo,
        issue_number_gh=issue_number_gh,
    )

    validate_predictions(predictions, open_pr_numbers)

    # --- Phase 4: Load wallet and verify registration (expensive, last) ---
    try:
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        hotkey_addr = wallet.hotkey.ss58_address

        with console.status('[bold cyan]Verifying miner registration...', spinner='dots'):
            subtensor = bt.Subtensor(network=ws_endpoint)
            netuid = read_netuid_from_contract(ws_endpoint, contract_addr, verbose)
            metagraph = subtensor.metagraph(netuid=netuid)

        if hotkey_addr not in metagraph.hotkeys:
            raise click.ClickException(
                f'Hotkey {hotkey_addr} is not registered on the metagraph. '
                f'Register your miner before submitting predictions.'
            )

    except ImportError as e:
        raise click.ClickException(f'Missing dependency — {e}. Install with: pip install bittensor')
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f'Failed to load wallet or connect to network: {e}')

    if not as_json:
        console.print(f'[dim]Miner hotkey: {hotkey_addr}[/dim]')

    # --- Phase 5: Build payload, confirm, output ---
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
        pred_lines = format_pred_lines(predictions)
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

    pred_lines = format_pred_lines(predictions)
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
