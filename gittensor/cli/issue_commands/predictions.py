# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Submissions and predict commands for issue PRs (miner-facing prediction feature).

Commands:
    gitt issues submissions --id <N>
    gitt issues predict --id <N>
"""

import json as json_mod

import click
from rich.panel import Panel

from .helpers import (
    _is_interactive,
    build_pr_table,
    build_prediction_payload,
    collect_predictions,
    console,
    fetch_issue_from_contract,
    fetch_issue_prs,
    format_prediction_lines,
    get_contract_address,
    get_github_pat,
    load_config,
    print_network_header,
    resolve_network,
    validate_issue_id,
    verify_miner_registration,
)


@click.command('submissions')
@click.option('--id', 'issue_id', required=True, type=int, help='On-chain issue ID to query')
@click.option(
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint (overrides --network)')
@click.option('--contract', default='', help='Contract address (uses default if empty)')
@click.option('--verbose', '-v', is_flag=True, help='Show debug output for contract reads')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON for scripting')
def issues_submissions(issue_id: int, network: str, rpc_url: str, contract: str, verbose: bool, as_json: bool):
    """List open pull requests for an issue bounty.

    Shows PRs that reference the given on-chain issue, filtered to open
    PRs only. The table includes author, creation date, review status,
    and whether the PR explicitly closes the issue.

    \b
    Note:
        Set GITTENSOR_MINER_PAT to a GitHub personal access token for
        higher API rate limits and access to private repositories.
        Without it, unauthenticated GitHub API limits apply.

    \b
    Examples:
        gitt issues submissions --id 42
        gitt i submissions --id 42 --json
        gitt i submissions --id 42 --network test -v
    """
    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)
    if not contract_addr:
        raise click.ClickException('Contract address not configured.')

    if not as_json:
        print_network_header(network_name, contract_addr)

    with console.status('[bold cyan]Reading issues from contract...', spinner='dots'):
        issue = fetch_issue_from_contract(ws_endpoint, contract_addr, issue_id, require_active=False, verbose=verbose)

    repo = issue['repository_full_name']
    issue_number = issue['issue_number']

    pat = get_github_pat()
    if not pat and not as_json:
        console.print('[dim]Using unauthenticated GitHub API; rate limits may apply.[/dim]')
    with console.status('[bold cyan]Fetching open PRs from GitHub...', spinner='dots'):
        prs = fetch_issue_prs(repo, issue_number, pat)

    if as_json:
        console.print(
            json_mod.dumps(
                [
                    {
                        'number': p.get('number'),
                        'title': p.get('title'),
                        'author': p.get('author_login'),
                        'state': p.get('state', 'OPEN'),
                        'created_at': p.get('created_at'),
                        'merged_at': p.get('merged_at'),
                        'url': p.get('url'),
                        'review_count': p.get('review_count', 0),
                        'closes_issue': issue_number in (p.get('closing_numbers') or []),
                    }
                    for p in prs
                ],
                indent=2,
                default=str,
            )
        )
        return

    console.print(f'[bold cyan]Open PRs for Issue #{issue_id} ({repo}#{issue_number})[/bold cyan]\n')
    if prs:
        console.print(build_pr_table(prs, issue_number=issue_number))
        console.print(f'\n[dim]Showing {len(prs)} open PR(s)[/dim]')
    else:
        console.print('[yellow]No open PRs found for this issue.[/yellow]')
    console.print(f'[dim]GitHub issue: https://github.com/{repo}/issues/{issue_number}[/dim]')
    console.print(f'[dim]Tip: Use "gitt i predict --id {issue_id}" to submit merge-probability predictions.[/dim]')


@click.command('predict')
@click.option('--id', 'issue_id', required=True, type=int, help='On-chain issue ID to predict for')
@click.option('--pr', 'pr_number', default=None, type=int, help='PR number to predict (pair with --probability)')
@click.option('--probability', default=None, type=float, help='Merge probability 0.0–1.0 (pair with --pr)')
@click.option('--json-input', default=None, type=str, help='Batch predictions as JSON: \'{"PR": prob, ...}\'')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation prompt (non-interactive/CI)')
@click.option('--wallet-name', '--wallet.name', '--wallet', default='default', help='Wallet name')
@click.option('--wallet-hotkey', '--wallet.hotkey', '--hotkey', default='default', help='Hotkey name')
@click.option(
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
    help='Network (finney/test/local)',
)
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint (overrides --network)')
@click.option('--contract', default='', help='Contract address (uses default if empty)')
@click.option('--verbose', '-v', is_flag=True, help='Show debug output for contract reads')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON for scripting')
def issues_predict(
    issue_id: int,
    pr_number: int,
    probability: float,
    json_input: str,
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

    Predictions assign merge probabilities (0.0–1.0) to open PRs for a
    bountied issue. The sum of all probabilities must not exceed 1.0.
    Only registered miners on the subnet may submit predictions.

    \b
    Input modes (mutually exclusive):
      1. --pr N --probability F    Single PR prediction
      2. --json-input '{...}'      Batch: '{"101": 0.85, "103": 0.15}'
      3. (no flags)                Interactive prompt (requires TTY)

    \b
    Note:
        Requires GITTENSOR_MINER_PAT (GitHub PAT) in the environment
        and a registered miner wallet on the subnet.

    \b
    Examples:
        gitt i predict --id 42 --pr 101 --probability 0.85 -y
        gitt i predict --id 42 --json-input '{"101": 0.5, "103": 0.3}' -y
        gitt i predict --id 42                       # interactive mode
        gitt i predict --id 42 --pr 101 --probability 0.7 -y --json
    """
    # --- Phase 1: cheap local validation (no network) ---
    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)
    if not contract_addr:
        raise click.ClickException('Contract address not configured.')

    # Cheap flag conflict checks (fail before any network I/O)
    if pr_number is not None and json_input is not None:
        raise click.ClickException('Use either --pr/--probability or --json-input, not both.')
    if probability is not None and json_input is not None:
        raise click.ClickException('Use either --pr/--probability or --json-input, not both.')
    if pr_number is None and probability is not None and json_input is None:
        raise click.ClickException('--probability requires --pr.')
    if pr_number is not None and probability is None and json_input is None:
        raise click.ClickException('--probability is required when --pr is set.')
    if probability is not None and not (0.0 <= probability <= 1.0):
        raise click.ClickException(f'Probability must be between 0.0 and 1.0 (got {probability})')

    pat = get_github_pat()
    if not pat:
        raise click.ClickException('GITTENSOR_MINER_PAT environment variable is required for predict.')

    # --- Phase 2: on-chain + GitHub reads (before wallet) ---
    if not as_json:
        print_network_header(network_name, contract_addr)

    with console.status('[bold cyan]Reading issues from contract...', spinner='dots'):
        issue = fetch_issue_from_contract(ws_endpoint, contract_addr, issue_id, require_active=True, verbose=verbose)
    repo = issue['repository_full_name']
    gh_issue_number = issue['issue_number']

    with console.status('[bold cyan]Fetching open PRs...', spinner='dots'):
        prs = fetch_issue_prs(repo, gh_issue_number, pat)
    pr_numbers = {p.get('number') for p in prs if p.get('number') is not None}

    # --- Phase 3: collect predictions (network-validated PR set) ---
    predictions = collect_predictions(pr_number, probability, json_input, prs, pr_numbers, issue_id)

    # --- Phase 4: wallet + miner registration (expensive, last) ---
    config = load_config()
    effective_wallet = wallet_name if wallet_name != 'default' else config.get('wallet', wallet_name)
    effective_hotkey = wallet_hotkey if wallet_hotkey != 'default' else config.get('hotkey', wallet_hotkey)

    try:
        import bittensor as bt

        wallet = bt.Wallet(name=effective_wallet, hotkey=effective_hotkey)
        hotkey_ss58 = wallet.hotkey.ss58_address
    except ImportError as e:
        raise click.ClickException(f'Missing dependency \u2014 {e}')
    except Exception as e:
        raise click.ClickException(f'Failed to load wallet: {e}')

    if not as_json:
        console.print(f'[dim]Miner hotkey: {hotkey_ss58}[/dim]')

    if not verify_miner_registration(ws_endpoint, contract_addr, hotkey_ss58, verbose):
        raise click.ClickException(f'Hotkey {hotkey_ss58[:16]}... is not a registered miner on this subnet.')

    # --- Phase 5: build payload, confirm, output ---
    payload = build_prediction_payload(issue_id, repo, gh_issue_number, hotkey_ss58, predictions)

    if as_json:
        console.print(json_mod.dumps(payload, indent=2, default=str))
        return

    skip_confirm = yes or not _is_interactive()
    if not skip_confirm and not click.confirm('\nSubmit these predictions?', default=True):
        console.print('[yellow]Cancelled.[/yellow]')
        return

    pred_lines = format_prediction_lines(predictions)
    console.print(
        Panel(
            f'Prediction recorded (local-only)\n\n'
            f'[cyan]Issue:[/cyan] #{issue_id} ({repo}#{gh_issue_number})\n'
            f'[cyan]Miner:[/cyan] {hotkey_ss58}\n'
            f'[cyan]Predictions:[/cyan]\n{pred_lines}',
            title='Prediction Submitted',
            border_style='green',
        )
    )
    # TODO: Replace with actual synapse broadcast when prediction protocol is implemented
    console.print('[dim]Note: On-chain broadcast will be available when the prediction synapse protocol is live.[/dim]')
