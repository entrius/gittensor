# Entrius 2025

"""gitt miner status — Check eligibility-gate progress without waiting for the validator."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

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
    _resolve_endpoint,
    _status,
)
from gittensor.constants import (
    BASE_GITHUB_API_URL,
    GITHUB_HTTP_TIMEOUT_SECONDS,
    MIN_CREDIBILITY,
    MIN_VALID_MERGED_PRS,
    PR_LOOKBACK_DAYS,
)
from gittensor.utils.github_api_tools import make_headers
from gittensor.validator.utils.load_weights import load_master_repo_weights

console = Console()


@dataclass
class StatusReport:
    """Local-only snapshot of a miner's eligibility-gate progress.

    The validator's gate additionally requires each merged PR to clear
    ``MIN_TOKEN_SCORE_FOR_BASE_SCORE`` after tree-sitter scoring, which
    needs per-file content fetches and is too expensive to replay
    client-side. The CLI therefore reports the *raw* merged count plus a
    note that the validator gate is stricter.
    """

    uid: Optional[int]
    github_login: str
    network: str
    netuid: int
    merged_count: int
    closed_count: int
    credibility: float
    eligible_by_count: bool
    eligible_by_credibility: bool
    lookback_start: str
    incentivized_repos_only: bool


def _resolve_pat_and_login(pat: Optional[str], json_mode: bool) -> Tuple[str, str]:
    """Resolve PAT (flag → env var → prompt) and the login it identifies.

    Exits with code 1 if the PAT is missing, invalid, or rejects the
    `/user` probe. Returns ``(pat, login)`` on success.
    """
    pat = pat or os.environ.get('GITTENSOR_MINER_PAT')
    if not pat:
        if json_mode:
            _error('--pat flag or GITTENSOR_MINER_PAT environment variable is required for JSON mode.', json_mode)
            sys.exit(1)
        pat = click.prompt('Enter your GitHub Personal Access Token', hide_input=True)

    try:
        resp = requests.get(
            f'{BASE_GITHUB_API_URL}/user', headers=make_headers(pat), timeout=GITHUB_HTTP_TIMEOUT_SECONDS
        )
    except requests.RequestException as e:
        _error(f'GitHub PAT verification failed: {e}', json_mode)
        sys.exit(1)

    if resp.status_code != 200:
        _error(f'GitHub PAT rejected by /user (HTTP {resp.status_code}). Check token validity.', json_mode)
        sys.exit(1)

    login = resp.json().get('login')
    if not login:
        _error('GitHub /user response missing login field.', json_mode)
        sys.exit(1)

    return pat, login


def _count_user_prs_in_window(
    login: str,
    pat: str,
    since: datetime,
    incentivized_repos: set[str],
) -> Tuple[int, int]:
    """Count merged + closed-not-merged PRs by ``login`` since ``since``.

    Uses the GitHub Search API (one call per state) and filters
    client-side to whitelisted ``incentivized_repos``. The validator
    counts only PRs against incentivized repos in its gate, so this CLI
    matches that scope by default.
    """
    since_iso = since.strftime('%Y-%m-%d')
    merged = _search_count(f'is:pr author:{login} is:merged merged:>={since_iso}', pat, incentivized_repos)
    closed = _search_count(f'is:pr author:{login} is:closed -is:merged closed:>={since_iso}', pat, incentivized_repos)
    return merged, closed


def _search_count(query: str, pat: str, incentivized_repos: set[str]) -> int:
    """Run a Search API query and return the count of items in incentivized repos."""
    total = 0
    page = 1
    while True:
        resp = requests.get(
            f'{BASE_GITHUB_API_URL}/search/issues',
            headers=make_headers(pat),
            params={'q': query, 'per_page': 100, 'page': page},
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return total
        body = resp.json()
        items = body.get('items', [])
        if not items:
            break
        for item in items:
            url = item.get('repository_url', '')
            # repository_url shape: https://api.github.com/repos/<owner>/<name>
            slug = url.removeprefix('https://api.github.com/repos/').lower()
            if slug in incentivized_repos:
                total += 1
        if len(items) < 100:
            break
        page += 1
        if page > 10:  # safety cap; 1000 PRs in 35 days is already an extreme outlier
            break
    return total


def _build_report(
    *,
    pat: str,
    login: str,
    wallet_hotkey_ss58: Optional[str],
    metagraph,
    network_endpoint: str,
    netuid: int,
) -> StatusReport:
    """Pure aggregator: composes a StatusReport from already-resolved inputs."""
    from gittensor.validator.oss_contributions.credibility import calculate_credibility

    repo_weights = load_master_repo_weights()
    incentivized = {name for name in repo_weights}

    since = datetime.now(timezone.utc) - timedelta(days=PR_LOOKBACK_DAYS)
    merged_count, closed_count = _count_user_prs_in_window(login, pat, since, incentivized)

    # `calculate_credibility` works on Sequences with a length, not the raw
    # count, so build minimal placeholders. We don't need the per-PR fields here.
    merged_placeholders = [object()] * merged_count
    closed_placeholders = [object()] * closed_count
    credibility = calculate_credibility(merged_placeholders, closed_placeholders)

    uid: Optional[int] = None
    if metagraph is not None and wallet_hotkey_ss58 is not None:
        if wallet_hotkey_ss58 in metagraph.hotkeys:
            uid = metagraph.hotkeys.index(wallet_hotkey_ss58)

    return StatusReport(
        uid=uid,
        github_login=login,
        network=network_endpoint,
        netuid=netuid,
        merged_count=merged_count,
        closed_count=closed_count,
        credibility=credibility,
        eligible_by_count=merged_count >= MIN_VALID_MERGED_PRS,
        eligible_by_credibility=credibility >= MIN_CREDIBILITY,
        lookback_start=since.strftime('%Y-%m-%d'),
        incentivized_repos_only=True,
    )


def _render_json(report: StatusReport) -> str:
    """Stable JSON envelope mirroring `miner check` / `miner post`."""
    eligible = report.eligible_by_count and report.eligible_by_credibility
    return json.dumps(
        {
            'success': eligible,
            'uid': report.uid,
            'github_login': report.github_login,
            'network': report.network,
            'netuid': report.netuid,
            'merged_pull_requests': report.merged_count,
            'closed_pull_requests': report.closed_count,
            'credibility': round(report.credibility, 4),
            'thresholds': {
                'min_valid_merged_prs': MIN_VALID_MERGED_PRS,
                'min_credibility': MIN_CREDIBILITY,
                'pr_lookback_days': PR_LOOKBACK_DAYS,
            },
            'gates': {
                'merged_count': report.eligible_by_count,
                'credibility': report.eligible_by_credibility,
            },
            'lookback_start': report.lookback_start,
            'note': (
                'Validator additionally requires each merged PR to clear '
                'token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE — not replayed locally.'
            ),
        },
        indent=2,
    )


def _render_table(report: StatusReport) -> Table:
    """Rich table for human-readable output (default mode)."""
    table = Table(title='Miner Eligibility Status')
    table.add_column('Metric', style='cyan')
    table.add_column('Value', justify='right')
    table.add_column('Threshold', justify='right', style='dim')
    table.add_column('Status', justify='center')

    def mark(ok: bool) -> str:
        return '[green]✓[/green]' if ok else '[red]✗[/red]'

    uid_str = str(report.uid) if report.uid is not None else '[yellow]not registered[/yellow]'
    table.add_row('UID', uid_str, '—', '')
    table.add_row('GitHub login', report.github_login, '—', '')
    table.add_row('Network', report.network, '—', '')
    table.add_row(
        'Merged PRs (raw)',
        str(report.merged_count),
        f'≥ {MIN_VALID_MERGED_PRS}',
        mark(report.eligible_by_count),
    )
    table.add_row('Closed (not merged)', str(report.closed_count), '—', '')
    table.add_row(
        'Credibility',
        f'{report.credibility:.2f}',
        f'≥ {MIN_CREDIBILITY:.2f}',
        mark(report.eligible_by_credibility),
    )
    table.add_row('Lookback start', report.lookback_start, f'{PR_LOOKBACK_DAYS}d', '')
    return table


@click.command()
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option(
    '--pat',
    default=None,
    help='GitHub Personal Access Token. If not provided, falls back to GITTENSOR_MINER_PAT env var or interactive prompt.',
)
@click.option('--json-output', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def miner_status(wallet_name, wallet_hotkey, netuid, network, rpc_url, pat, json_mode):
    """Check eligibility-gate progress without waiting for validator scoring.

    Counts your merged + closed-not-merged PRs across incentivized repos in
    the rolling lookback window, derives credibility locally, and reports
    pass/fail against `MIN_VALID_MERGED_PRS` and `MIN_CREDIBILITY`.

    The validator's gate additionally requires each merged PR to clear
    `MIN_TOKEN_SCORE_FOR_BASE_SCORE` after tree-sitter scoring; that step
    needs per-file content fetches and is too expensive to replay locally.
    Raw merged count is shown as a ceiling — actual valid count may be
    lower.

    \b
    Examples:
        gitt miner status --wallet alice --hotkey default
        gitt miner status --pat ghp_xxxx --json-output
        gitt miner status --network test --pat ghp_xxxx
    """
    pat, login = _resolve_pat_and_login(pat, json_mode)

    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    ws_endpoint = _resolve_endpoint(network, rpc_url)

    _print(
        f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {ws_endpoint} | Netuid: {netuid} | GitHub: {login}[/dim]',
        json_mode,
    )

    metagraph = None
    wallet_hotkey_ss58 = None
    with _status('[bold]Connecting to network...', json_mode):
        try:
            wallet, _subtensor, metagraph, _dendrite = _connect_bittensor(
                wallet_name, wallet_hotkey, ws_endpoint, netuid
            )
            wallet_hotkey_ss58 = wallet.hotkey.ss58_address
        except Exception as e:
            # Network unreachable / wallet missing / unregistered — still report
            # GitHub-side progress so the operator can fix one thing at a time.
            _print(f'[yellow]Network connect failed ({e}); reporting GitHub-only metrics[/yellow]', json_mode)

    with _status('[bold]Counting your PRs in incentivized repos...', json_mode):
        report = _build_report(
            pat=pat,
            login=login,
            wallet_hotkey_ss58=wallet_hotkey_ss58,
            metagraph=metagraph,
            network_endpoint=ws_endpoint,
            netuid=netuid,
        )

    if json_mode:
        click.echo(_render_json(report))
    else:
        console.print(_render_table(report))
        eligible = report.eligible_by_count and report.eligible_by_credibility
        if eligible:
            console.print('\n[bold green]Eligible for validator scoring.[/bold green]')
        else:
            console.print(
                '\n[bold red]Not yet eligible.[/bold red] Validator additionally checks token_score ≥ 5 per merged PR.'
            )

    if not (report.eligible_by_count and report.eligible_by_credibility):
        sys.exit(1)
