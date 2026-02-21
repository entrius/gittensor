# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Merge prediction commands for issue CLI

Commands:
    gitt issues submissions --id <N>
    gitt issues predict --id <N>
"""

import json as json_mod
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import click
from rich.panel import Panel
from rich.table import Table

from .helpers import (
    GITHUB_API_TIMEOUT,
    _is_interactive,
    console,
    get_contract_address,
    print_error,
    print_network_header,
    print_success,
    read_issues_from_contract,
    resolve_network,
    validate_issue_id,
)

GITTENSOR_NETUID = 74  # GitTensor subnet netuid

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_github_token() -> Optional[str]:
    """Read GitHub PAT from GITTENSOR_MINER_PAT environment variable."""
    return os.environ.get('GITTENSOR_MINER_PAT')


def _fetch_prs_rest(
    repo: str,
    issue_number: int,
    token: Optional[str] = None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """Fetch open PRs referencing an issue via the GitHub REST search API.

    This is the unauthenticated fallback when no PAT is available (or as a
    secondary data source).  Uses the same ``urllib.request`` pattern as
    ``helpers.py:validate_github_issue``.
    """
    query = f'repo:{repo}+is:pr+is:open+{issue_number}'
    url = f'https://api.github.com/search/issues?q={query}'

    headers: Dict[str, str] = {'User-Agent': 'gittensor-cli'}
    if token:
        headers['Authorization'] = f'token {token}'

    if verbose:
        console.print(f'[dim]REST search: {url}[/dim]')

    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=GITHUB_API_TIMEOUT)
        data = json_mod.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if verbose:
            console.print(f'[dim]REST search failed: HTTP {e.code}[/dim]')
        return []
    except (urllib.error.URLError, OSError) as e:
        if verbose:
            console.print(f'[dim]REST search failed: {e}[/dim]')
        return []

    prs: List[Dict[str, Any]] = []
    for item in data.get('items', []):
        if 'pull_request' not in item:
            continue
        prs.append(
            {
                'number': item.get('number'),
                'title': item.get('title', ''),
                'state': 'OPEN',
                'createdAt': item.get('created_at', ''),
                'url': item.get('html_url', ''),
                'author': (item.get('user') or {}).get('login', 'unknown'),
                'baseRepository': repo,
                'reviewDecision': None,
                'reviewCount': 0,
            }
        )
    return prs


def _fetch_open_prs_for_issue(
    repo: str,
    issue_number: int,
    token: Optional[str] = None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """Fetch open PRs for an issue.  Tries GraphQL first, falls back to REST."""
    if token:
        try:
            from gittensor.utils.github_api_tools import find_open_prs_for_issue

            if verbose:
                console.print('[dim]Fetching PRs via GraphQL...[/dim]')
            prs = find_open_prs_for_issue(repo, issue_number, token)
            if prs:
                return prs
            if verbose:
                console.print('[dim]GraphQL returned no PRs, trying REST...[/dim]')
        except Exception as e:
            if verbose:
                console.print(f'[dim]GraphQL failed ({e}), falling back to REST...[/dim]')

    return _fetch_prs_rest(repo, issue_number, token, verbose)


def _resolve_issue_from_contract(
    issue_id: int,
    network: Optional[str],
    rpc_url: Optional[str],
    contract: str,
    verbose: bool,
    as_json: bool,
) -> Optional[Dict[str, Any]]:
    """Look up an issue on-chain and validate it exists and is Active/Registered.

    Returns the issue dict or None (after printing an error).
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)

    if not contract_addr:
        raise click.ClickException('Contract address not configured. Set via: gitt config set contract_address <ADDR>.')

    if not as_json:
        print_network_header(network_name, contract_addr)

    try:
        validate_issue_id(issue_id)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    with console.status('[bold cyan]Reading issues from contract...', spinner='dots'):
        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

    issue = next((i for i in issues if i['id'] == issue_id), None)
    if not issue:
        print_error(f'Issue {issue_id} not found on contract.')
        return None

    status = issue.get('status', '')
    if isinstance(status, dict):
        status = list(status.keys())[0] if status else 'Unknown'
    if status not in ('Active', 'Registered'):
        console.print(
            f'[yellow]Warning: Issue {issue_id} status is "{status}". '
            f'Predictions are typically for Active or Registered issues.[/yellow]'
        )

    return issue


def _display_pr_table(prs: List[Dict[str, Any]]) -> None:
    """Render a Rich table of open PRs."""
    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('PR #', style='cyan', justify='right')
    table.add_column('Title', style='green')
    table.add_column('Author', style='yellow')
    table.add_column('Reviews', justify='right')
    table.add_column('Created', style='dim')

    for pr in prs:
        created = pr.get('createdAt', '')[:10]
        review_decision = pr.get('reviewDecision') or ''
        review_count = pr.get('reviewCount', 0)
        review_display = f'{review_count}'
        if review_decision:
            review_display += f' ({review_decision})'

        table.add_row(
            str(pr['number']),
            pr.get('title', '')[:60],
            pr.get('author', 'unknown'),
            review_display,
            created,
        )

    console.print(table)


def _validate_predictions(
    predictions: Dict[int, float],
    prs: List[Dict[str, Any]],
) -> None:
    """Validate prediction probabilities.

    Raises click.ClickException on invalid input.
    """
    valid_pr_numbers = {pr['number'] for pr in prs}

    for pr_num, prob in predictions.items():
        if pr_num not in valid_pr_numbers:
            raise click.ClickException(f'PR #{pr_num} is not in the list of open PRs for this issue.')
        if prob < 0.0 or prob > 1.0:
            raise click.ClickException(f'Probability for PR #{pr_num} must be between 0.0 and 1.0 (got {prob}).')

    total = sum(predictions.values())
    if total > 1.0:
        raise click.ClickException(
            f'Sum of probabilities ({total:.4f}) exceeds 1.0. '
            f'Total merge probability across all PRs cannot exceed 100%.'
        )


def _verify_miner_registered(
    wallet_name: str,
    wallet_hotkey: str,
    ws_endpoint: str,
    netuid: int,
    verbose: bool,
) -> Any:
    """Load wallet and verify miner is registered on the subnet.

    Returns the wallet object on success.
    Raises click.ClickException if not registered.
    """
    try:
        import bittensor as bt
    except ImportError:
        raise click.ClickException('bittensor is required for predictions. Install with: pip install bittensor')

    wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
    hotkey_addr = wallet.hotkey.ss58_address

    if verbose:
        console.print(f'[dim]Wallet hotkey: {hotkey_addr}[/dim]')

    with console.status('[bold cyan]Verifying miner registration...', spinner='dots'):
        subtensor = bt.Subtensor(network=ws_endpoint)
        metagraph = subtensor.metagraph(netuid)

    if hotkey_addr not in metagraph.hotkeys:
        raise click.ClickException(
            f'Hotkey {hotkey_addr} is not registered on subnet {netuid}. '
            f'Register first with: btcli subnet register --netuid {netuid}'
        )

    uid = metagraph.hotkeys.index(hotkey_addr)
    if verbose:
        console.print(f'[dim]Miner UID: {uid}[/dim]')

    return wallet


# ---------------------------------------------------------------------------
# CLI Commands
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
    help='Contract address (uses default if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON for scripting')
def issues_submissions(
    issue_id: int,
    network: str,
    rpc_url: str,
    contract: str,
    verbose: bool,
    as_json: bool,
):
    """List open PR submissions for an issue.

    Shows pull requests that reference the given on-chain issue,
    which are candidates for merge predictions.

    \b
    Examples:
        gitt issues submissions --id 1
        gitt i submissions --id 1 --json
        gitt i submissions --id 1 --verbose
    """
    issue = _resolve_issue_from_contract(issue_id, network, rpc_url, contract, verbose, as_json)
    if not issue:
        return

    repo = issue.get('repository_full_name', '')
    issue_number = issue.get('issue_number', 0)

    token = _get_github_token()
    if not token and not as_json:
        console.print(
            '[yellow]Warning: GITTENSOR_MINER_PAT not set. Using unauthenticated API (lower rate limits).[/yellow]\n'
        )

    with console.status('[bold cyan]Fetching open PRs...', spinner='dots'):
        prs = _fetch_open_prs_for_issue(repo, issue_number, token, verbose)

    if as_json:
        console.print(
            json_mod.dumps(
                {
                    'issue_id': issue_id,
                    'repository': repo,
                    'issue_number': issue_number,
                    'open_prs': prs,
                },
                indent=2,
                default=str,
            )
        )
        return

    console.print(f'[bold cyan]Open PRs for Issue #{issue_id}[/bold cyan]')
    console.print(f'[dim]Repository: {repo} | Issue: #{issue_number}[/dim]\n')

    if prs:
        _display_pr_table(prs)
        console.print(f'\n[dim]{len(prs)} open PR(s) found[/dim]')
    else:
        console.print('[yellow]No open PRs found for this issue.[/yellow]')


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
    help='PR number to predict on (use with --probability)',
)
@click.option(
    '--probability',
    default=None,
    type=float,
    help='Merge probability for --pr (0.0 to 1.0)',
)
@click.option(
    '--json-input',
    default=None,
    type=str,
    help='JSON predictions: \'{"123": 0.8, "456": 0.2}\'',
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
    '--yes',
    '-y',
    is_flag=True,
    help='Skip confirmation prompt (non-interactive/CI)',
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
    wallet_name: str,
    wallet_hotkey: str,
    yes: bool,
    network: str,
    rpc_url: str,
    contract: str,
    verbose: bool,
    as_json: bool,
):
    """Submit merge predictions for an issue's open PRs.

    Predict which PR(s) will be merged to solve the issue. Probabilities must
    be between 0.0 and 1.0, and the sum across all PRs must not exceed 1.0.

    \b
    Input modes (mutually exclusive):
        --pr + --probability   Single PR prediction
        --json-input           JSON dict of {pr_number: probability}
        (neither)              Interactive mode - prompt per PR

    \b
    Examples:
        gitt issues predict --id 1 --pr 42 --probability 0.8 -y
        gitt issues predict --id 1 --json-input '{"42": 0.8, "99": 0.15}'
        gitt issues predict --id 1
    """
    # --- Validate flag mutual exclusivity ---
    has_single = pr_number is not None or probability is not None
    has_json = json_input is not None
    if has_single and has_json:
        raise click.ClickException('Cannot use --pr/--probability together with --json-input. Choose one input mode.')
    if (pr_number is not None) != (probability is not None):
        raise click.ClickException('--pr and --probability must be used together.')

    # --- Require PAT ---
    token = _get_github_token()
    if not token:
        raise click.ClickException(
            'GITTENSOR_MINER_PAT environment variable is required for predictions. '
            'Set it with: export GITTENSOR_MINER_PAT=ghp_...'
        )

    # --- Load wallet + verify registration ---
    ws_endpoint, network_name = resolve_network(network, rpc_url)
    wallet = _verify_miner_registered(wallet_name, wallet_hotkey, ws_endpoint, GITTENSOR_NETUID, verbose)

    # --- Resolve issue from contract ---
    issue = _resolve_issue_from_contract(issue_id, network, rpc_url, contract, verbose, as_json)
    if not issue:
        return

    repo = issue.get('repository_full_name', '')
    issue_number = issue.get('issue_number', 0)

    # --- Fetch open PRs ---
    with console.status('[bold cyan]Fetching open PRs...', spinner='dots'):
        prs = _fetch_open_prs_for_issue(repo, issue_number, token, verbose)

    if not prs:
        print_error(f'No open PRs found for issue #{issue_id}. Nothing to predict on.')
        return

    # --- Build predictions dict ---
    predictions: Dict[int, float] = {}

    if json_input:
        # JSON input mode
        try:
            raw = json_mod.loads(json_input)
        except json_mod.JSONDecodeError as e:
            raise click.ClickException(f'Invalid JSON input: {e}')

        if not isinstance(raw, dict):
            raise click.ClickException('JSON input must be a dict of {{pr_number: probability}}.')

        for k, v in raw.items():
            try:
                predictions[int(k)] = float(v)
            except (ValueError, TypeError):
                raise click.ClickException(f'Invalid entry in JSON: {k}={v}')

    elif pr_number is not None and probability is not None:
        # Single PR mode
        predictions[pr_number] = probability

    else:
        # Interactive mode
        if not _is_interactive():
            raise click.ClickException(
                'Interactive mode requires a TTY. Use --pr/--probability or --json-input instead.'
            )

        console.print('\n[bold cyan]Open PRs for prediction:[/bold cyan]\n')
        _display_pr_table(prs)
        console.print()

        running_total = 0.0
        for pr in prs:
            remaining = 1.0 - running_total
            prompt_text = (
                f'PR #{pr["number"]} ({pr.get("title", "")[:40]}) - probability [0.0-{remaining:.2f}] (Enter to skip)'
            )
            value = click.prompt(prompt_text, default='', show_default=False)
            if not value.strip():
                continue

            try:
                prob = float(value)
            except ValueError:
                console.print(f'[yellow]Skipping PR #{pr["number"]}: invalid number[/yellow]')
                continue

            if prob < 0.0 or prob > 1.0:
                console.print(f'[yellow]Skipping PR #{pr["number"]}: must be 0.0-1.0[/yellow]')
                continue

            if running_total + prob > 1.0:
                console.print(
                    f'[red]Cannot add {prob:.4f} - total would be '
                    f'{running_total + prob:.4f} (exceeds 1.0). Skipped.[/red]'
                )
                continue

            predictions[pr['number']] = prob
            running_total += prob

            if running_total >= 0.9:
                console.print(
                    f'[yellow]Warning: Running total is {running_total:.4f} '
                    f'(only {1.0 - running_total:.4f} remaining)[/yellow]'
                )

    if not predictions:
        console.print('[yellow]No predictions entered. Aborting.[/yellow]')
        return

    # --- Validate ---
    _validate_predictions(predictions, prs)

    # --- Build payload ---
    payload = {
        'issue_id': issue_id,
        'repository': repo,
        'miner_hotkey': wallet.hotkey.ss58_address,
        'predictions': {str(k): v for k, v in predictions.items()},
    }

    # --- Confirmation ---
    skip_confirm = yes or not _is_interactive()
    if not skip_confirm:
        pred_lines = '\n'.join(f'  PR #{pr_num}: {prob:.4f}' for pr_num, prob in predictions.items())
        total = sum(predictions.values())
        console.print(
            Panel(
                f'[cyan]Issue ID:[/cyan] {issue_id}\n'
                f'[cyan]Repository:[/cyan] {repo}\n'
                f'[cyan]Predictions:[/cyan]\n{pred_lines}\n'
                f'[cyan]Total probability:[/cyan] {total:.4f}\n'
                f'[cyan]Network:[/cyan] {network_name}',
                title='Prediction Summary',
                border_style='blue',
            )
        )
        if not click.confirm('\nSubmit predictions?', default=True):
            console.print('[yellow]Prediction cancelled.[/yellow]')
            return

    # --- Stub broadcast ---
    if as_json:
        console.print(json_mod.dumps(payload, indent=2))
    else:
        print_success('Prediction prepared successfully!')
        console.print(f'[cyan]Payload:[/cyan] {json_mod.dumps(payload, indent=2)}')
        console.print(
            '\n[dim]Note: Network broadcast is not yet implemented. '
            'This payload will be submitted via synapse in a future release.[/dim]'
        )

    # TODO: broadcast prediction synapse
