# Entrius 2025

"""gitt miner repos — List whitelisted repositories ranked by scoring weight.

A repository's weight is a direct multiplier on every merged-PR score, so the
ranked list tells a miner where a contribution is worth the most. Reads the same
``master_repositories.json`` the validator scores against — no network or PAT
required.
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List, Optional, Tuple

import click
from rich.console import Console
from rich.table import Table

from gittensor.validator.utils.load_weights import RepositoryConfig, load_master_repo_weights

console = Console()

# Share-of-total thresholds used to bucket repos for the distribution summary.
HIGH_TIER_SHARE = 0.0075  # >= 0.75% of total weight
MEDIUM_TIER_SHARE = 0.0030  # >= 0.30% of total weight


def _filter_repos(
    repos: Dict[str, RepositoryConfig],
    include_inactive: bool,
    search: Optional[str],
) -> Dict[str, RepositoryConfig]:
    """Drop inactive repos (unless requested) and apply a case-insensitive name filter."""
    needle = search.lower().strip() if search else None
    filtered: Dict[str, RepositoryConfig] = {}
    for name, config in repos.items():
        if not include_inactive and config.inactive_at is not None:
            continue
        if needle and needle not in name.lower():
            continue
        filtered[name] = config
    return filtered


def _rank_repos(repos: Dict[str, RepositoryConfig]) -> List[Tuple[str, RepositoryConfig]]:
    """Sort repos by weight descending, breaking ties alphabetically for stable output."""
    return sorted(repos.items(), key=lambda item: (-item[1].weight, item[0]))


def _weight_tier(share: float) -> str:
    """Bucket a repo by its share of total weight into high / medium / low."""
    if share >= HIGH_TIER_SHARE:
        return 'high'
    if share >= MEDIUM_TIER_SHARE:
        return 'medium'
    return 'low'


def _summarize(ranked: List[Tuple[str, RepositoryConfig]], total_weight: float) -> Dict[str, object]:
    """Build aggregate stats: counts, total weight, and a high/medium/low tier breakdown."""
    tiers = {'high': 0, 'medium': 0, 'low': 0}
    inactive = 0
    for _, config in ranked:
        if config.inactive_at is not None:
            inactive += 1
        share = config.weight / total_weight if total_weight > 0 else 0.0
        tiers[_weight_tier(share)] += 1
    return {
        'total': len(ranked),
        'active': len(ranked) - inactive,
        'inactive': inactive,
        'total_weight': round(total_weight, 6),
        'tiers': tiers,
    }


def _build_rows(
    ranked: List[Tuple[str, RepositoryConfig]],
    total_weight: float,
    top: Optional[int],
) -> List[Dict[str, object]]:
    """Turn ranked (name, config) pairs into display/JSON rows, optionally truncated to top-N."""
    rows: List[Dict[str, object]] = []
    for rank, (name, config) in enumerate(ranked, start=1):
        if top is not None and rank > top:
            break
        share = config.weight / total_weight if total_weight > 0 else 0.0
        rows.append(
            {
                'rank': rank,
                'repository': name,
                'weight': round(config.weight, 6),
                'share': round(share, 6),
                'tier': _weight_tier(share),
                'active': config.inactive_at is None,
            }
        )
    return rows


@click.command()
@click.option('--all', '-a', 'include_inactive', is_flag=True, default=False, help='Include inactive (delisted) repos.')
@click.option('--top', type=int, default=None, help='Show only the top N repositories by weight.')
@click.option('--search', default=None, help='Filter repositories by a case-insensitive name substring.')
@click.option('--json-output', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def miner_repos(include_inactive: bool, top: Optional[int], search: Optional[str], json_mode: bool):
    """List whitelisted repositories ranked by scoring weight.

    Weight is a direct multiplier on every merged-PR score, so higher-weight
    repos pay more per contribution. Active repos only by default.

    \b
    Examples:
        gitt miner repos
        gitt miner repos --top 20
        gitt miner repos --search bittensor
        gitt miner repos --all --json-output
    """
    repos = load_master_repo_weights()

    if not repos:
        msg = 'No repositories found. The master_repositories.json weights file may be missing or empty.'
        if json_mode:
            click.echo(json.dumps({'success': False, 'error': msg}))
        else:
            console.print(f'[red]Error: {msg}[/red]')
        sys.exit(1)

    filtered = _filter_repos(repos, include_inactive, search)
    ranked = _rank_repos(filtered)
    total_weight = sum(config.weight for _, config in ranked)
    rows = _build_rows(ranked, total_weight, top)
    summary = _summarize(ranked, total_weight)

    if json_mode:
        click.echo(json.dumps({'summary': summary, 'repositories': rows}, indent=2))
        return

    if not rows:
        console.print('[yellow]No repositories match the given filters.[/yellow]')
        return

    title = 'Whitelisted Repositories by Weight'
    if search:
        title += f" (search: '{search}')"
    table = Table(title=title, show_header=True)
    table.add_column('#', style='dim', justify='right')
    table.add_column('Repository', style='cyan')
    table.add_column('Weight', style='green', justify='right')
    table.add_column('Share', style='dim', justify='right')
    table.add_column('Status', justify='center')

    tier_color = {'high': 'green', 'medium': 'yellow', 'low': 'dim'}
    for row in rows:
        status = '[green]active[/green]' if row['active'] else '[red]delisted[/red]'
        color = tier_color[row['tier']]
        table.add_row(
            str(row['rank']),
            f'[{color}]{row["repository"]}[/{color}]',
            f'{row["weight"]:.4f}',
            f'{row["share"] * 100:.2f}%',
            status,
        )

    console.print(table)
    shown = len(rows)
    console.print(
        f'\n[bold]{shown}[/bold] of {summary["total"]} repos shown | '
        f'active {summary["active"]} | inactive {summary["inactive"]} | '
        f'tiers: [green]{summary["tiers"]["high"]} high[/green], '
        f'[yellow]{summary["tiers"]["medium"]} medium[/yellow], '
        f'{summary["tiers"]["low"]} low'
    )
