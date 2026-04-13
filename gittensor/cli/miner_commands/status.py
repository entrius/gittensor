# Entrius 2025

"""gitt miner status — Show eligibility gate progress and PR overview."""

from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.table import Table

from gittensor.constants import (
    CREDIBILITY_MULLIGAN_COUNT,
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MIN_CREDIBILITY,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    MIN_VALID_MERGED_PRS,
    PR_LOOKBACK_DAYS,
)

from .post import NETUID_DEFAULT, _error, _load_config_value, _resolve_endpoint

if TYPE_CHECKING:
    from gittensor.classes import MinerEvaluation

console = Console()


def _rich_status(message: str, json_mode: bool):
    """Rich spinner in TTY mode; no-op in JSON mode (avoids duplicate branches)."""
    return nullcontext() if json_mode else console.status(message)


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
@click.option('--detail', is_flag=True, default=False, help='Show per-PR breakdown.')
@click.option('--json-output', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def miner_status(wallet_name, wallet_hotkey, netuid, network, rpc_url, pat, detail, json_mode):
    """Show your eligibility gate progress and PR overview.

    Fetches your PRs from incentivized repositories using your GitHub PAT,
    then calculates credibility locally. Token-based PR scoring is not run
    here (too heavy for CLI); the merge-count gate uses all merged PRs in the
    lookback, while validators require enough merges each meeting a minimum
    token score. Final eligibility is determined by validators.

    \b
    Examples:
        gitt miner status --wallet alice --hotkey default
        gitt miner status --wallet alice --hotkey default --detail
        gitt miner status --network test --json-output
    """
    from gittensor.classes import MinerEvaluation
    from gittensor.utils.github_api_tools import get_github_user, load_miners_prs
    from gittensor.validator.oss_contributions.credibility import calculate_credibility
    from gittensor.validator.utils.load_weights import load_master_repo_weights

    pat = pat or os.environ.get('GITTENSOR_MINER_PAT')
    if not pat:
        if json_mode:
            _error('--pat flag or GITTENSOR_MINER_PAT environment variable is required.', json_mode)
            sys.exit(1)
        pat = click.prompt('Enter your GitHub Personal Access Token', hide_input=True)

    with _rich_status('[bold]Validating PAT...', json_mode):
        user_data = get_github_user(pat)

    if not user_data or not user_data.get('id'):
        _error('GitHub PAT is invalid or expired.', json_mode)
        sys.exit(1)

    github_id = str(user_data['id'])
    github_username = user_data.get('login')

    if not json_mode:
        display = f'@{github_username}' if github_username else github_id
        console.print(f'[green]PAT valid[/green] — {display}')

    wallet_name = wallet_name or _load_config_value('wallet') or 'default'
    wallet_hotkey = wallet_hotkey or _load_config_value('hotkey') or 'default'
    ws_endpoint = _resolve_endpoint(network, rpc_url)

    if not json_mode:
        console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey} | Network: {ws_endpoint} | Netuid: {netuid}[/dim]')

    with _rich_status('[bold]Connecting to network...', json_mode):
        uid, hotkey_ss58 = _resolve_uid(wallet_name, wallet_hotkey, ws_endpoint, netuid, json_mode)

    master_repositories = load_master_repo_weights()
    if not master_repositories:
        _error('Failed to load master repositories.', json_mode)
        sys.exit(1)

    miner_eval = MinerEvaluation(uid=uid, hotkey=hotkey_ss58, github_id=github_id)
    miner_eval.github_pat = pat

    with _rich_status(f'[bold]Fetching PRs across {len(master_repositories)} repos...', json_mode):
        load_miners_prs(miner_eval, master_repositories)

    miner_eval.github_pat = None

    credibility = calculate_credibility(miner_eval.merged_pull_requests, miner_eval.closed_pull_requests)
    merged_count = len(miner_eval.merged_pull_requests)
    credibility_pass = credibility >= MIN_CREDIBILITY
    # Without token scoring, count all merged PRs in lookback (validators use token_score >= threshold per PR).
    merged_pass = merged_count >= MIN_VALID_MERGED_PRS
    unique_repos = frozenset(pr.repository_full_name for pr in miner_eval.merged_pull_requests)

    ctx = _StatusContext(
        uid=uid,
        github_id=github_id,
        github_username=github_username,
        network=ws_endpoint,
        miner_eval=miner_eval,
        credibility=credibility,
        credibility_pass=credibility_pass,
        merged_pass=merged_pass,
        unique_repos=unique_repos,
    )

    if json_mode:
        _output_json(ctx, detail)
    else:
        _output_rich(ctx, detail)


@dataclass
class _StatusContext:
    """Holds computed status data for output rendering."""

    uid: int
    github_id: str
    github_username: str | None
    network: str
    miner_eval: MinerEvaluation
    credibility: float
    credibility_pass: bool
    merged_pass: bool
    unique_repos: frozenset[str]

    @property
    def merged_count(self) -> int:
        return len(self.miner_eval.merged_pull_requests)

    @property
    def closed_count(self) -> int:
        return len(self.miner_eval.closed_pull_requests)

    @property
    def open_count(self) -> int:
        return len(self.miner_eval.open_pull_requests)

    @property
    def effective_closed(self) -> int:
        return max(0, self.closed_count - CREDIBILITY_MULLIGAN_COUNT)


def _resolve_uid(wallet_name, wallet_hotkey, ws_endpoint, netuid, json_mode):
    """Connect to the network and resolve the miner's UID. Returns (uid, hotkey_ss58)."""
    import bittensor as bt

    try:
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        metagraph = subtensor.metagraph(netuid=netuid)
    except Exception as e:
        _error(f'Failed to connect to network: {e}', json_mode)
        sys.exit(1)

    hotkey_ss58 = wallet.hotkey.ss58_address
    if hotkey_ss58 not in metagraph.hotkeys:
        _error(f'Hotkey {hotkey_ss58[:16]}... is not registered on subnet {netuid}.', json_mode)
        sys.exit(1)

    return metagraph.hotkeys.index(hotkey_ss58), hotkey_ss58


def _output_rich(ctx: _StatusContext, detail: bool):
    """Render status output with Rich formatting."""
    github_display = f'@{ctx.github_username}' if ctx.github_username else ctx.github_id

    console.print()
    console.print('[bold]Miner Status[/bold]')
    console.print(f'[dim]UID: {ctx.uid}  |  GitHub: {github_display}  |  Network: {ctx.network}[/dim]')
    console.print()

    merged_icon = '[green]pass[/green]' if ctx.merged_pass else '[red]fail[/red]'
    cred_icon = '[green]pass[/green]' if ctx.credibility_pass else '[red]fail[/red]'
    mulligan_note = f', {CREDIBILITY_MULLIGAN_COUNT} mulligan' if ctx.effective_closed != ctx.closed_count else ''

    console.print('[bold]Eligibility Gate[/bold]')

    if ctx.merged_pass:
        console.print(f'  Merged PRs:    {ctx.merged_count}/{MIN_VALID_MERGED_PRS}  {merged_icon}')
    else:
        remaining = MIN_VALID_MERGED_PRS - ctx.merged_count
        console.print(
            f'  Merged PRs:    {ctx.merged_count}/{MIN_VALID_MERGED_PRS}  {merged_icon}'
            f'  (need {remaining} more; validators require ≥{MIN_TOKEN_SCORE_FOR_BASE_SCORE} token score each)'
        )

    console.print(
        f'  Credibility:   {ctx.credibility:.2f}/{MIN_CREDIBILITY:.2f}  {cred_icon}'
        f'  ({ctx.merged_count}M/{ctx.effective_closed}C{mulligan_note})'
    )

    if ctx.merged_pass and ctx.credibility_pass:
        console.print('  Status:        [green]LIKELY ELIGIBLE[/green]')
    else:
        console.print('  Status:        [red]NOT ELIGIBLE[/red]')

    console.print(
        f'  [dim]Note: Merge counts are unscored; validators apply token scoring. '
        f'Threshold ≥{MIN_TOKEN_SCORE_FOR_BASE_SCORE} per merged PR.[/dim]'
    )
    console.print()

    console.print(f'[bold]Lookback Window ({PR_LOOKBACK_DAYS} days)[/bold]')
    console.print(f'  Merged: {ctx.merged_count}  |  Open: {ctx.open_count}  |  Closed: {ctx.closed_count}')
    if ctx.unique_repos:
        console.print(f'  Unique repos: {len(ctx.unique_repos)}')
    console.print(f'  Open PR threshold: {EXCESSIVE_PR_PENALTY_BASE_THRESHOLD} base (increases with token score)')
    console.print()

    if detail:
        _print_pr_table('Merged PRs', ctx.miner_eval.merged_pull_requests, date_col='Merged')
        _print_pr_table('Open PRs', ctx.miner_eval.open_pull_requests, date_col='Created')
        _print_pr_table('Closed PRs', ctx.miner_eval.closed_pull_requests, date_col='Created')


def _print_pr_table(title, prs, date_col='Date'):
    """Print a Rich table for a list of PRs."""
    if not prs:
        console.print(f'[dim]{title}: none[/dim]')
        console.print()
        return

    table = Table(title=title, show_header=True, header_style='bold')
    table.add_column('PR #', style='cyan', justify='right')
    table.add_column('Repository', style='white')
    table.add_column(date_col, style='dim')

    for pr in prs:
        date_val = pr.merged_at if date_col == 'Merged' else pr.created_at
        date_str = date_val.strftime('%b %d') if date_val else 'N/A'
        table.add_row(str(pr.number), pr.repository_full_name, date_str)

    console.print(table)
    console.print()


def _output_json(ctx: _StatusContext, detail: bool):
    """Output status as JSON."""
    note = (
        f'Merge counts omit token scoring; validators require ≥{MIN_TOKEN_SCORE_FOR_BASE_SCORE} '
        'token score per merged PR toward the merge gate. Final eligibility depends on validators.'
    )
    data = {
        'uid': ctx.uid,
        'github_id': ctx.github_id,
        'github_username': ctx.github_username,
        'network': ctx.network,
        'eligibility': {
            'merged_prs': ctx.merged_count,
            'required_merged_prs': MIN_VALID_MERGED_PRS,
            'merged_pass': ctx.merged_pass,
            'credibility': round(ctx.credibility, 4),
            'required_credibility': MIN_CREDIBILITY,
            'credibility_pass': ctx.credibility_pass,
            'likely_eligible': ctx.merged_pass and ctx.credibility_pass,
            'min_token_score_per_merged_pr': MIN_TOKEN_SCORE_FOR_BASE_SCORE,
            'merge_gate_uses_unscored_merges': True,
            'note': note,
        },
        'lookback': {
            'days': PR_LOOKBACK_DAYS,
            'merged': ctx.merged_count,
            'open': ctx.open_count,
            'closed': ctx.closed_count,
            'unique_repos': sorted(ctx.unique_repos),
            'open_pr_threshold_base': EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
        },
    }

    if detail:
        data['merged_prs'] = [_pr_to_dict(pr) for pr in ctx.miner_eval.merged_pull_requests]
        data['open_prs'] = [_pr_to_dict(pr) for pr in ctx.miner_eval.open_pull_requests]
        data['closed_prs'] = [_pr_to_dict(pr) for pr in ctx.miner_eval.closed_pull_requests]

    click.echo(json.dumps(data, indent=2, default=str))


def _pr_to_dict(pr):
    """Convert a PullRequest to a JSON-serializable dict."""
    return {
        'number': pr.number,
        'repository': pr.repository_full_name,
        'title': pr.title,
        'author': pr.author_login,
        'state': pr.pr_state.value,
        'created_at': pr.created_at.isoformat() if pr.created_at else None,
        'merged_at': pr.merged_at.isoformat() if pr.merged_at else None,
    }
