# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Miner-facing prediction commands for issue CLI

Commands:
    gitt issues submissions --id <N>
    gitt issues predict --id <N>
"""

import json as json_mod
import os

import click
from rich.panel import Panel
from rich.table import Table

from .helpers import (
    _is_interactive,
    console,
    fetch_open_prs_for_issue,
    get_contract_address,
    print_error,
    print_network_header,
    print_success,
    read_issues_from_contract,
    resolve_network,
    validate_issue_id,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_issue_and_prs(issue_id, network, rpc_url, contract, verbose):
    """Resolve on-chain issue data and fetch open PRs from GitHub.

    Returns (issue_dict, prs_list, ws_endpoint, network_name, contract_addr)
    or raises click.ClickException on failure.
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException(
            'Contract address not configured. Set via: gitt config set contract_address <ADDR>.'
        )

    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    with console.status('[bold cyan]Reading issue from contract...', spinner='dots'):
        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

    issue = next((i for i in issues if i['id'] == issue_id), None)
    if not issue:
        raise click.ClickException(f'Issue {issue_id} not found on contract.')

    status = issue.get('status', '')
    if isinstance(status, str):
        status = status.capitalize()
    if status not in ('Active', 'Registered'):
        raise click.ClickException(
            f'Issue {issue_id} is not in an active/bountied state (status: {status}).'
        )

    repo_full = issue.get('repository_full_name', '')
    issue_number = issue.get('issue_number', 0)
    if not repo_full or '/' not in repo_full:
        raise click.ClickException(f'Issue {issue_id} has invalid repository: {repo_full}')

    owner, repo = repo_full.split('/', 1)

    with console.status('[bold cyan]Fetching open PRs from GitHub...', spinner='dots'):
        prs = fetch_open_prs_for_issue(owner, repo, issue_number, verbose)

    return issue, prs, ws_endpoint, network_name, contract_addr


def _build_pr_table(prs):
    """Build a Rich Table from a list of PR dicts."""
    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('PR #', style='cyan', justify='right')
    table.add_column('Title', style='green')
    table.add_column('Author', style='yellow')
    table.add_column('Created', style='dim')
    table.add_column('Review', justify='center')
    table.add_column('URL', style='dim')

    for pr in prs:
        review = pr.get('review_status', 'UNKNOWN')
        review_styled = {
            'APPROVED': '[green]APPROVED[/green]',
            'CHANGES_REQUESTED': '[red]CHANGES_REQ[/red]',
            'REVIEW_REQUIRED': '[yellow]REVIEW_REQ[/yellow]',
            'PENDING': '[yellow]PENDING[/yellow]',
        }.get(review, f'[dim]{review}[/dim]')

        created = pr.get('created_at', '')[:10]  # date part only

        table.add_row(
            str(pr['number']),
            pr.get('title', '')[:60],
            pr.get('author', 'unknown'),
            created,
            review_styled,
            pr.get('url', ''),
        )
    return table


# ---------------------------------------------------------------------------
# submissions command
# ---------------------------------------------------------------------------


@click.command('submissions')
@click.option(
    '--id',
    'issue_id',
    required=True,
    type=int,
    help='On-chain issue ID',
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
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON for scripting')
def issues_submissions(issue_id: int, network: str, rpc_url: str, contract: str, verbose: bool, as_json: bool):
    """List open PRs (submissions) for a bountied issue.

    Displays all open pull requests that reference a given on-chain
    bountied issue. Uses GITTENSOR_MINER_PAT for authenticated GitHub
    API calls when set, otherwise falls back to unauthenticated endpoints.

    \b
    Examples:
        gitt issues submissions --id 42
        gitt i submissions --id 42
        gitt i submissions --id 42 --json
    """
    issue, prs, ws_endpoint, network_name, contract_addr = _resolve_issue_and_prs(
        issue_id, network, rpc_url, contract, verbose,
    )

    repo_full = issue.get('repository_full_name', '')
    issue_number = issue.get('issue_number', 0)

    if as_json:
        console.print(json_mod.dumps({
            'issue_id': issue_id,
            'repository': repo_full,
            'issue_number': issue_number,
            'submissions': prs,
        }, indent=2, default=str))
        return

    print_network_header(network_name, contract_addr)
    console.print(
        f'[bold cyan]Submissions for Issue #{issue_id}[/bold cyan] '
        f'({repo_full}#{issue_number})\n'
    )

    if prs:
        console.print(_build_pr_table(prs))
        console.print(f'\n[dim]{len(prs)} open PR(s) found[/dim]')
    else:
        console.print('[yellow]No open PRs found for this issue.[/yellow]')


# ---------------------------------------------------------------------------
# predict command
# ---------------------------------------------------------------------------


def _validate_probability(value):
    """Validate a probability value is in [0.0, 1.0]. Returns float."""
    try:
        p = float(value)
    except (TypeError, ValueError):
        raise click.BadParameter(f'Invalid probability: {value} (must be a float in [0.0, 1.0])')
    if p < 0.0 or p > 1.0:
        raise click.BadParameter(f'Probability must be in [0.0, 1.0] (got {p})')
    return p


def _validate_pr_belongs_to_issue(pr_number, prs):
    """Validate that pr_number exists in the list of open PRs."""
    pr_numbers = {pr['number'] for pr in prs}
    if pr_number not in pr_numbers:
        raise click.ClickException(
            f'PR #{pr_number} is not an open PR for this issue. '
            f'Valid PRs: {sorted(pr_numbers)}'
        )


@click.command('predict')
@click.option(
    '--id',
    'issue_id',
    required=True,
    type=int,
    help='On-chain issue ID',
)
@click.option(
    '--pr',
    'pr_number',
    default=None,
    type=int,
    help='PR number (non-interactive single prediction)',
)
@click.option(
    '--probability',
    default=None,
    type=float,
    help='Merge probability for --pr (0.0 to 1.0)',
)
@click.option(
    '--json-input',
    'json_input',
    default=None,
    type=str,
    help='Batch predictions as JSON: \'{"101": 0.85, "103": 0.10}\'',
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
    help='Contract address (uses config if empty)',
)
@click.option(
    '--yes',
    '-y',
    is_flag=True,
    help='Skip confirmation prompt (non-interactive/CI)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'as_json', is_flag=True, help='Output result as JSON')
def issues_predict(
    issue_id: int,
    pr_number: int,
    probability: float,
    json_input: str,
    wallet_name: str,
    wallet_hotkey: str,
    network: str,
    rpc_url: str,
    contract: str,
    yes: bool,
    verbose: bool,
    as_json: bool,
):
    """Predict merge probabilities for PRs on a bountied issue.

    Interactive mode (default): prompts you to assign probabilities to
    each open PR. Non-interactive modes available via --pr/--probability
    or --json-input for scripting.

    \b
    Examples:
        gitt i predict --id 42
        gitt i predict --id 42 --pr 101 --probability 0.85
        gitt i predict --id 42 --json-input '{"101": 0.85, "103": 0.10}'
        gitt i predict --id 42 --json-input '{"101": 0.85}' -y
    """
    # --- Resolve issue and PRs ---
    issue, prs, ws_endpoint, network_name, contract_addr = _resolve_issue_and_prs(
        issue_id, network, rpc_url, contract, verbose,
    )

    repo_full = issue.get('repository_full_name', '')

    if not prs:
        raise click.ClickException('No open PRs found for this issue. Nothing to predict.')

    # --- Load miner identity ---
    pat = os.environ.get('GITTENSOR_MINER_PAT', '').strip()
    if not pat and not as_json:
        console.print('[yellow]Warning: GITTENSOR_MINER_PAT not set. Wallet verification only.[/yellow]')

    miner_hotkey_addr = None
    try:
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        miner_hotkey_addr = wallet.hotkey.ss58_address
        if verbose and not as_json:
            console.print(f'[dim]Miner hotkey: {miner_hotkey_addr}[/dim]')

        # Verify the hotkey is registered on the subnet
        subtensor = bt.Subtensor(network=ws_endpoint)
        netuid = 47  # gittensor subnet
        metagraph = subtensor.metagraph(netuid)
        if miner_hotkey_addr not in metagraph.hotkeys:
            raise click.ClickException(
                f'Hotkey {miner_hotkey_addr} is not a registered miner on subnet {netuid}.'
            )
        if verbose and not as_json:
            console.print('[dim]Miner registration verified.[/dim]')
    except ImportError:
        if not as_json:
            console.print('[yellow]Warning: bittensor not installed — skipping wallet verification.[/yellow]')
    except click.ClickException:
        raise
    except Exception as e:
        if not as_json:
            console.print(f'[yellow]Warning: Could not verify miner registration: {e}[/yellow]')

    # --- Build predictions dict ---
    predictions = {}  # {pr_number: probability}

    if json_input is not None:
        # Batch mode via --json-input
        try:
            raw = json_mod.loads(json_input)
        except json_mod.JSONDecodeError as e:
            raise click.ClickException(f'Invalid JSON input: {e}')
        if not isinstance(raw, dict):
            raise click.ClickException('--json-input must be a JSON object: {"pr_number": probability, ...}')
        for k, v in raw.items():
            try:
                pr_num = int(k)
            except ValueError:
                raise click.ClickException(f'Invalid PR number in JSON: {k}')
            prob = _validate_probability(v)
            _validate_pr_belongs_to_issue(pr_num, prs)
            predictions[pr_num] = prob

    elif pr_number is not None:
        # Single non-interactive mode
        if probability is None:
            raise click.ClickException('--probability is required when using --pr.')
        prob = _validate_probability(probability)
        _validate_pr_belongs_to_issue(pr_number, prs)
        predictions[pr_number] = prob

    else:
        # Interactive mode
        if not as_json:
            print_network_header(network_name, contract_addr)
            console.print(
                f'[bold cyan]Predict Merge Probabilities[/bold cyan] — '
                f'Issue #{issue_id} ({repo_full})\n'
            )
            console.print(_build_pr_table(prs))
            console.print()

        running_total = 0.0
        for pr in prs:
            pr_num = pr['number']
            while True:
                raw_input = click.prompt(
                    f'  PR #{pr_num} ({pr["title"][:40]}) probability [0.0-1.0, Enter to skip]',
                    default='',
                    show_default=False,
                )
                if raw_input.strip() == '':
                    break
                try:
                    prob = _validate_probability(raw_input)
                except click.BadParameter as e:
                    console.print(f'  [red]{e}[/red]')
                    continue

                new_total = running_total + prob
                if new_total > 1.0:
                    console.print(
                        f'  [red]Sum would exceed 1.0 ({new_total:.4f}). '
                        f'Current total: {running_total:.4f}. Try again.[/red]'
                    )
                    continue

                predictions[pr_num] = prob
                running_total = new_total
                if running_total > 0.9:
                    console.print(f'  [yellow]Running total: {running_total:.4f} (approaching 1.0)[/yellow]')
                else:
                    console.print(f'  [dim]Running total: {running_total:.4f}[/dim]')
                break

        if not predictions:
            console.print('[yellow]No predictions entered. Aborting.[/yellow]')
            return

    # --- Validate sum <= 1.0 ---
    total = sum(predictions.values())
    if total > 1.0:
        raise click.ClickException(
            f'Sum of probabilities ({total:.4f}) exceeds 1.0. Predictions rejected.'
        )

    # --- Build payload ---
    payload = {
        'issue_id': issue_id,
        'repository': repo_full,
        'predictions': {int(k): float(v) for k, v in predictions.items()},
    }

    # --- Confirmation ---
    if as_json:
        console.print(json_mod.dumps(payload, indent=2, default=str))
        return

    pred_lines = '\n'.join(
        f'  PR #{pr_num}: {prob:.4f}' for pr_num, prob in sorted(predictions.items())
    )
    console.print(
        Panel(
            f'[cyan]Issue ID:[/cyan] {issue_id}\n'
            f'[cyan]Repository:[/cyan] {repo_full}\n'
            f'[cyan]Total probability:[/cyan] {total:.4f}\n'
            f'[cyan]Predictions:[/cyan]\n{pred_lines}',
            title='Prediction Summary',
            border_style='blue',
        )
    )

    skip_confirm = yes or not _is_interactive()
    if not skip_confirm and not click.confirm('\nSubmit prediction?', default=True):
        console.print('[yellow]Prediction cancelled.[/yellow]')
        return

    # TODO: Broadcast prediction via synapse to validators.
    # The validated payload is structured and ready for a future synapse
    # to consume. Replace the stub below with the actual network call.
    print_success('Prediction validated and ready to broadcast!')
    console.print(
        Panel(
            json_mod.dumps(payload, indent=2, default=str),
            title='[bold green]Validated Payload (broadcast stub)[/bold green]',
            border_style='green',
        )
    )
    console.print(
        '[dim]Network broadcast is not yet implemented. '
        'The payload above shows exactly what would be sent.[/dim]'
    )
