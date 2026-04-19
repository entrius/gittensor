# Entrius 2025

"""gitt miner status — Show miner eligibility gate progress."""

from __future__ import annotations

import json
import os
import sys

import click
import requests
from rich.console import Console
from rich.table import Table

from gittensor.cli.miner_commands.helpers import (
    NETUID_DEFAULT,
    _connect_bittensor,
    _error,
    _load_config_value,
    _print,
    _require_registered,
    _resolve_endpoint,
    _status,
)
from gittensor.constants import (
    BASE_GITHUB_API_URL,
    GITHUB_HTTP_TIMEOUT_SECONDS,
    GRAPHQL_VIEWER_QUERY,
)

console = Console()

MIN_MERGED_PRS = 5
MIN_CREDIBILITY = 0.75


@click.command()
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option(
    '--pat',
    default=None,
    help='GitHub Personal Access Token. Falls back to GITTENSOR_MINER_PAT env var.',
)
@click.option('--json-output', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
@click.option('--detail', is_flag=True, default=False, help='Show per-PR breakdown table.')
def miner_status(wallet_name, wallet_hotkey, netuid, network, rpc_url, pat, json_mode, detail):
    """Show miner eligibility gate progress.

    Validates your GitHub PAT, resolves your UID on the network, fetches
    your PRs across incentivized repositories, and displays your current
    eligibility status (merged PR count, credibility, pass/fail).

    \b
    Note:
        Final eligibility also depends on token scoring by validators.
        This command shows LIKELY ELIGIBLE based on PR count and
        credibility alone.

    \b
    Examples:
        gitt miner status --wallet alice --hotkey default
        gitt miner status --wallet alice --hotkey default --detail
        gitt miner status --wallet alice --hotkey default --json-output
    """
    # 1. Resolve PAT
    pat = pat or os.environ.get('GITTENSOR_MINER_PAT')
    if not pat:
        if json_mode:
            _error('--pat flag or GITTENSOR_MINER_PAT env var is required for JSON mode.', json_mode)
            sys.exit(1)
        pat = click.prompt('Enter your GitHub Personal Access Token', hide_input=True)

    # 2. Validate PAT and get GitHub identity
    with _status('[bold]Validating PAT...', json_mode):
        github_login = _validate_pat_and_get_login(pat)

    if github_login is None:
        _error('GitHub PAT is invalid or expired. Check your GITTENSOR_MINER_PAT.', json_mode)
        sys.exit(1)

    _print(f'[green]PAT valid. GitHub user: @{github_login}[/green]', json_mode)

    # 3. Resolve wallet and network
    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    ws_endpoint = _resolve_endpoint(network, rpc_url)

    _print(
        f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {ws_endpoint} | Netuid: {netuid}[/dim]',
        json_mode,
    )

    # 4. Connect to network and resolve UID
    with _status('[bold]Connecting to network...', json_mode):
        try:
            wallet, subtensor, metagraph, _ = _connect_bittensor(wallet_name, wallet_hotkey, ws_endpoint, netuid)
        except Exception as e:
            _error(f'Failed to initialize bittensor: {e}', json_mode)
            sys.exit(1)

    _require_registered(wallet, metagraph, netuid, json_mode)

    uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
    _print(f'[dim]UID: {uid}[/dim]', json_mode)

    # 5. Fetch PRs using the existing pipeline
    with _status('[bold]Fetching pull requests...', json_mode):
        try:
            from gittensor.classes import MinerEvaluation
            from gittensor.utils.github_api_tools import load_miners_prs
            from gittensor.validator.utils.load_weights import load_master_repo_weights

            master_repositories = load_master_repo_weights()
            miner_eval = MinerEvaluation(
                uid=uid,
                hotkey=wallet.hotkey.ss58_address,
                github_id=github_login,
            )
            miner_eval.github_pat = pat
            load_miners_prs(miner_eval, master_repositories)
        except Exception as e:
            _error(f'Failed to fetch pull requests: {e}', json_mode)
            sys.exit(1)

    # 6. Calculate credibility
    try:
        from gittensor.validator.oss_contributions.credibility import calculate_credibility

        credibility = calculate_credibility(
            miner_eval.merged_pull_requests,
            miner_eval.closed_pull_requests,
        )
    except Exception:
        credibility = 0.0

    merged_count = miner_eval.total_merged_prs
    open_count = miner_eval.total_open_prs
    closed_count = miner_eval.total_closed_prs
    unique_repos = len(miner_eval.unique_repos_contributed_to)

    merged_pass = merged_count >= MIN_MERGED_PRS
    cred_pass = credibility >= MIN_CREDIBILITY
    likely_eligible = merged_pass and cred_pass

    # 7. Display results
    if json_mode:
        result = {
            'uid': uid,
            'github_login': github_login,
            'network': ws_endpoint,
            'eligibility_gate': {
                'merged_prs': merged_count,
                'merged_prs_required': MIN_MERGED_PRS,
                'merged_prs_pass': merged_pass,
                'credibility': round(credibility, 4),
                'credibility_required': MIN_CREDIBILITY,
                'credibility_pass': cred_pass,
                'likely_eligible': likely_eligible,
            },
            'lookback_window': {
                'merged': merged_count,
                'open': open_count,
                'closed': closed_count,
                'unique_repos': unique_repos,
            },
        }
        if detail:
            result['pull_requests'] = (
                [
                    {
                        'number': pr.number,
                        'repo': pr.repository_full_name,
                        'title': pr.title,
                        'state': 'merged',
                    }
                    for pr in miner_eval.merged_pull_requests
                ]
                + [
                    {
                        'number': pr.number,
                        'repo': pr.repository_full_name,
                        'title': pr.title,
                        'state': 'open',
                    }
                    for pr in miner_eval.open_pull_requests
                ]
                + [
                    {
                        'number': pr.number,
                        'repo': pr.repository_full_name,
                        'title': pr.title,
                        'state': 'closed',
                    }
                    for pr in miner_eval.closed_pull_requests
                ]
            )
        click.echo(json.dumps(result, indent=2))
    else:
        console.print('\n[bold]Miner Status[/bold]')
        console.print(f'UID: {uid}  |  GitHub: @{github_login}  |  Network: {ws_endpoint}\n')

        gate_table = Table(title='Eligibility Gate')
        gate_table.add_column('Metric', style='cyan')
        gate_table.add_column('Value', justify='right')
        gate_table.add_column('Required', justify='right', style='dim')
        gate_table.add_column('Status', justify='center')

        gate_table.add_row(
            'Merged PRs',
            str(merged_count),
            f'>= {MIN_MERGED_PRS}',
            '[green]pass[/green]' if merged_pass else '[red]fail[/red]',
        )
        gate_table.add_row(
            'Credibility',
            f'{credibility:.2f}',
            f'>= {MIN_CREDIBILITY}',
            '[green]pass[/green]' if cred_pass else '[red]fail[/red]',
        )
        console.print(gate_table)

        status_str = '[green]LIKELY ELIGIBLE[/green]' if likely_eligible else '[red]INELIGIBLE[/red]'
        console.print(f'\nStatus: {status_str}')
        if likely_eligible:
            console.print('[dim]Note: Final eligibility depends on token scoring by validators.[/dim]')

        console.print('\n[bold]Lookback Window[/bold]')
        console.print(
            f'Merged: {merged_count}  |  Open: {open_count}  |  Closed: {closed_count}  |  Unique repos: {unique_repos}'
        )

        if detail and miner_eval.merged_pull_requests:
            pr_table = Table(title='Merged Pull Requests')
            pr_table.add_column('Repo', style='cyan')
            pr_table.add_column('#', justify='right')
            pr_table.add_column('Title')
            for pr in miner_eval.merged_pull_requests:
                pr_table.add_row(pr.repository_full_name, str(pr.number), pr.title or '')
            console.print(pr_table)


def _validate_pat_and_get_login(pat: str) -> str | None:
    """Validate PAT and return GitHub login, or None if invalid."""
    headers = {'Authorization': f'token {pat}', 'Accept': 'application/vnd.github.v3+json'}
    try:
        user_resp = requests.get(
            f'{BASE_GITHUB_API_URL}/user',
            headers=headers,
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        if user_resp.status_code != 200:
            return None
        gql_headers = {'Authorization': f'Bearer {pat}', 'Accept': 'application/json'}
        gql_resp = requests.post(
            f'{BASE_GITHUB_API_URL}/graphql',
            json={'query': GRAPHQL_VIEWER_QUERY},
            headers=gql_headers,
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        if gql_resp.status_code != 200:
            return None
        return user_resp.json().get('login')
    except requests.RequestException:
        return None
