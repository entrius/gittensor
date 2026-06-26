# Entrius 2025

"""gitt miner languages — List file extensions ranked by scoring weight and method.

Two things drive how much a changed file is worth: its language weight and *how*
it is scored. Tree-sitter languages are scored on semantic AST tokens (high
ceiling); everything else falls back to line-count scoring, and documentation /
config extensions are additionally capped. This command surfaces both so a miner
can see which file types actually move their score. Reads the same
``programming_languages.json`` the validator scores against — no network required.
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List, Optional, Tuple

import click
from rich.console import Console
from rich.table import Table

from gittensor.constants import MAX_LINES_SCORED_FOR_NON_CODE_EXT, NON_CODE_EXTENSIONS
from gittensor.validator.utils.load_weights import LanguageConfig, load_programming_language_weights

console = Console()

# Scoring-method labels (mirrors how tree_sitter_scoring classifies a file).
METHOD_TOKEN = 'token'  # tree-sitter AST / semantic token scoring
METHOD_LINE = 'line-count'  # line-count scoring (no tree-sitter)
METHOD_LINE_CAPPED = 'line-count-capped'  # non-code extension, capped line-count

_NON_CODE = frozenset(NON_CODE_EXTENSIONS)


def _scoring_method(extension: str, config: LanguageConfig) -> str:
    """Classify how a file extension is scored, matching the validator's logic.

    Tree-sitter (token) scoring requires a language mapping AND a non-doc/config
    extension; documentation/config extensions are capped line-count; the rest
    are plain line-count.
    """
    ext = extension.lstrip('.').lower()
    if ext in _NON_CODE:
        return METHOD_LINE_CAPPED
    if config.language is not None:
        return METHOD_TOKEN
    return METHOD_LINE


def _filter_languages(
    languages: Dict[str, LanguageConfig],
    code_only: bool,
    search: Optional[str],
) -> Dict[str, LanguageConfig]:
    """Keep only token-scored extensions (when code_only) and apply a name filter."""
    needle = search.lower().strip() if search else None
    filtered: Dict[str, LanguageConfig] = {}
    for ext, config in languages.items():
        if code_only and _scoring_method(ext, config) != METHOD_TOKEN:
            continue
        if needle and needle not in ext.lower():
            continue
        filtered[ext] = config
    return filtered


def _rank_languages(languages: Dict[str, LanguageConfig]) -> List[Tuple[str, LanguageConfig]]:
    """Sort by weight descending, breaking ties alphabetically for stable output."""
    return sorted(languages.items(), key=lambda item: (-item[1].weight, item[0]))


def _summarize_languages(ranked: List[Tuple[str, LanguageConfig]]) -> Dict[str, object]:
    """Aggregate counts by scoring method plus tree-sitter coverage."""
    methods = {METHOD_TOKEN: 0, METHOD_LINE: 0, METHOD_LINE_CAPPED: 0}
    for ext, config in ranked:
        methods[_scoring_method(ext, config)] += 1
    total = len(ranked)
    return {
        'total': total,
        'token_scored': methods[METHOD_TOKEN],
        'line_count': methods[METHOD_LINE],
        'line_count_capped': methods[METHOD_LINE_CAPPED],
        'non_code_line_cap': MAX_LINES_SCORED_FOR_NON_CODE_EXT,
    }


def _build_language_rows(
    ranked: List[Tuple[str, LanguageConfig]],
    top: Optional[int],
) -> List[Dict[str, object]]:
    """Turn ranked (ext, config) pairs into display/JSON rows, optionally truncated."""
    rows: List[Dict[str, object]] = []
    for rank, (ext, config) in enumerate(ranked, start=1):
        if top is not None and rank > top:
            break
        method = _scoring_method(ext, config)
        rows.append(
            {
                'rank': rank,
                'extension': ext,
                'weight': round(config.weight, 6),
                'method': method,
                'tree_sitter': config.language if method == METHOD_TOKEN else None,
            }
        )
    return rows


@click.command()
@click.option(
    '--code-only', 'code_only', is_flag=True, default=False, help='Only show tree-sitter (token-scored) extensions.'
)
@click.option('--top', type=int, default=None, help='Show only the top N extensions by weight.')
@click.option('--search', default=None, help='Filter extensions by a case-insensitive substring.')
@click.option('--json-output', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def miner_languages(code_only: bool, top: Optional[int], search: Optional[str], json_mode: bool):
    """List file extensions ranked by scoring weight and method.

    Tree-sitter (token) extensions are scored on semantic AST content and have
    the highest ceiling; line-count extensions score per added line, and
    documentation/config types are capped. Use this to see which file types are
    worth the most per change.

    \b
    Examples:
        gitt miner languages
        gitt miner languages --code-only --top 20
        gitt miner languages --search ts
        gitt miner languages --json-output
    """
    languages = load_programming_language_weights()

    if not languages:
        msg = 'No languages found. The programming_languages.json weights file may be missing or empty.'
        if json_mode:
            click.echo(json.dumps({'success': False, 'error': msg}))
        else:
            console.print(f'[red]Error: {msg}[/red]')
        sys.exit(1)

    filtered = _filter_languages(languages, code_only, search)
    ranked = _rank_languages(filtered)
    rows = _build_language_rows(ranked, top)
    summary = _summarize_languages(_rank_languages(languages))

    if json_mode:
        click.echo(json.dumps({'summary': summary, 'languages': rows}, indent=2))
        return

    if not rows:
        console.print('[yellow]No extensions match the given filters.[/yellow]')
        return

    title = 'Scoring Weights by File Extension'
    if code_only:
        title += ' (tree-sitter only)'
    table = Table(title=title, show_header=True)
    table.add_column('#', style='dim', justify='right')
    table.add_column('Ext', style='cyan')
    table.add_column('Weight', style='green', justify='right')
    table.add_column('Scoring', justify='center')
    table.add_column('Tree-sitter', style='dim')

    method_render = {
        METHOD_TOKEN: '[green]token[/green]',
        METHOD_LINE: '[yellow]line-count[/yellow]',
        METHOD_LINE_CAPPED: f'[dim]line-count (cap {MAX_LINES_SCORED_FOR_NON_CODE_EXT})[/dim]',
    }
    for row in rows:
        table.add_row(
            str(row['rank']),
            row['extension'],
            f'{row["weight"]:.3f}',
            method_render[row['method']],
            str(row['tree_sitter'] or '—'),
        )

    console.print(table)
    shown = len(rows)
    console.print(
        f'\n[bold]{shown}[/bold] of {summary["total"]} extensions shown | '
        f'[green]{summary["token_scored"]} token[/green], '
        f'[yellow]{summary["line_count"]} line-count[/yellow], '
        f'{summary["line_count_capped"]} capped doc/config'
    )
