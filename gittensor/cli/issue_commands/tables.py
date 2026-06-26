# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Reusable Rich table presets."""

from typing import Any, Dict, List

from rich import box
from rich.table import Table


def build_pr_table(prs: List[Dict[str, Any]]) -> Table:
    """Build a Rich table for issue PR submissions.

    Note: ``review_count`` counts APPROVED reviews only. "Approved" means at
    least one approval review exists; it does not mean the PR is merge-ready.
    """
    table = Table(
        box=box.SQUARE,
        header_style='bold white',
        border_style='grey35',
        show_lines=True,
        pad_edge=True,
        show_header=True,
    )
    table.add_column('PR #', style='white', justify='right')
    table.add_column('Title', style='white', max_width=50)
    table.add_column('Author', style='white')
    table.add_column('Created', style='white')
    table.add_column('Status', style='white')
    table.add_column('URL', style='blue', max_width=60)

    for pr in prs:
        created_at = str(pr.get('created_at') or '')
        created_display = created_at[:10] if created_at else 'N/A'
        is_approved = (pr.get('review_count') or 0) > 0
        review_display = '[green]Approved[/green]' if is_approved else '[yellow]Pending[/yellow]'
        table.add_row(
            str(pr.get('number') or 'N/A'),
            pr.get('title') or 'Untitled',
            pr.get('author') or pr.get('author_login') or 'N/A',
            created_display,
            review_display,
            pr.get('html_url') or pr.get('url') or '',
        )

    return table
