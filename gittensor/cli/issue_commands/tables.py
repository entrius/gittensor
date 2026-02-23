# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Reusable Rich table presets."""

from dataclasses import dataclass
from typing import Any, Dict, List

from rich import box
from rich.table import Table


@dataclass(frozen=True)
class TableTheme:
    box_style: box.Box
    header_style: str
    border_style: str
    show_lines: bool
    pad_edge: bool


TABLE_THEMES = {
    # Full wrapped grid
    'square': TableTheme(
        box_style=box.SQUARE,
        header_style='bold magenta',
        border_style='grey35',
        show_lines=True,
        pad_edge=True,
    ),

    # Minimal separators with a heavier header rule
    'minimal': TableTheme(
        box_style=box.MINIMAL_HEAVY_HEAD,
        header_style='bold white',
        border_style='grey50',
        show_lines=False,
        pad_edge=False,
    ),
}

DEFAULT_TABLE_THEME = 'minimal'


def build_table(theme: str = DEFAULT_TABLE_THEME, **kwargs) -> Table:
    """Create a Rich table using a named visual theme."""
    preset = TABLE_THEMES.get(theme, TABLE_THEMES[DEFAULT_TABLE_THEME])
    params = {
        'box': preset.box_style,
        'header_style': preset.header_style,
        'border_style': preset.border_style,
        'show_lines': preset.show_lines,
        'pad_edge': preset.pad_edge,
    }
    params.update(kwargs)
    return Table(**params)


def build_pr_table(prs: List[Dict[str, Any]]) -> Table:
    """Build a Rich table for issue PR submissions.

    Note: ``review_count`` counts APPROVED reviews only. "Approved" means at
    least one approval review exists; it does not mean the PR is merge-ready.
    """
    table = build_table(theme='square', show_header=True)
    table.add_column('PR #', style='cyan', justify='right')
    table.add_column('Title', style='green', max_width=50)
    table.add_column('Author', style='yellow')
    table.add_column('Created', style='magenta')
    table.add_column('Review', style='white')
    table.add_column('URL', style='blue', max_width=60)

    for pr in prs:
        created_at = str(pr.get('created_at') or '')
        created_display = created_at[:10] if created_at else 'N/A'
        review_display = 'Approved' if (pr.get('review_count') or 0) > 0 else 'Pending'
        table.add_row(
            str(pr.get('number') or 'N/A'),
            pr.get('title') or 'Untitled',
            pr.get('author') or pr.get('author_login') or 'N/A',
            created_display,
            review_display,
            pr.get('html_url') or pr.get('url') or '',
        )

    return table
