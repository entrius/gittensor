# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared Click group classes with Rich-powered help output."""

from __future__ import annotations

from inspect import cleandoc

import click
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table


def _single_paragraph(text: str) -> str:
    """Collapse multiline help text into a single paragraph."""
    return ' '.join(text.split())


def _collect_help_rows(params: list[click.Parameter], ctx: click.Context) -> list[tuple[str, str]]:
    """Collect Click help records for parameters."""
    rows: list[tuple[str, str]] = []
    for param in params:
        record = param.get_help_record(ctx)
        if record is None:
            continue
        rows.append((record[0], record[1] or ''))
    return rows


def _render_usage(console: Console, usage: str) -> None:
    """Render the usage line with styling."""
    usage = usage.strip()
    if usage.startswith('Usage:'):
        usage_template = usage[len('Usage:') :].strip()
        console.print(
            Padding(
                f'[bold yellow]Usage:[/bold yellow] [bold white]{escape(usage_template)}[/bold white]',
                (0, 0, 0, 1),
            )
        )
        console.print()
        return

    console.print(Padding(f'[bold white]{escape(usage)}[/bold white]', (0, 0, 0, 1)))
    console.print()


def _section_panel(
    title: str,
    rows: list[tuple[str, str]],
    left_style: str = 'bold cyan',
    right_style: str = 'bright_white',
) -> Panel:
    table = Table.grid(expand=True)
    table.add_column(style=left_style, no_wrap=True, ratio=1, justify='left')
    table.add_column(style=right_style, ratio=4, justify='left')

    if rows:
        for left, right in rows:
            table.add_row(left, right)
    else:
        table.add_row('-', 'No entries')

    return Panel(
        table,
        title=f' {title} ',
        title_align='left',
        border_style='grey66',
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _parse_option_decl(option_decl: str) -> tuple[str, str, str]:
    """Split Click option declaration into long names, short alias, and type."""
    names_part = option_decl.strip()
    option_type = ''

    if ' ' in names_part:
        maybe_names, maybe_type = names_part.rsplit(' ', 1)
        if maybe_type.isupper() or (maybe_type.startswith('[') and maybe_type.endswith(']')):
            names_part = maybe_names
            option_type = maybe_type

    tokens = [token.strip() for token in names_part.split(',') if token.strip()]
    short_tokens = [token for token in tokens if token.startswith('-') and not token.startswith('--')]
    long_tokens = [token for token in tokens if token.startswith('--')]

    long_names = ','.join(long_tokens) if long_tokens else ','.join(tokens)
    short_alias = ','.join(short_tokens)
    return long_names, short_alias, option_type


def _options_panel(rows: list[tuple[str, str]]) -> Panel:
    """Render btcli-style options panel with explicit type column."""
    table = Table.grid(expand=True)
    table.add_column(style='bold cyan', no_wrap=True, ratio=4, justify='left')
    table.add_column(style='bold green', no_wrap=True, ratio=1, justify='left')
    table.add_column(style='bold yellow', no_wrap=True, ratio=1, justify='left')
    table.add_column(style='bright_white', ratio=7, justify='left')

    if rows:
        for decl, description in rows:
            long_names, short_alias, option_type = _parse_option_decl(decl)
            table.add_row(long_names, short_alias, option_type, description or '')
    else:
        table.add_row('-', '-', '-', 'No entries')

    return Panel(
        table,
        title=' Options ',
        title_align='left',
        border_style='grey66',
        box=box.ROUNDED,
        padding=(0, 1),
    )


class StyledCommand(click.Command):
    """Click command with styled help output."""

    def _help_options_rows(self, ctx: click.Context) -> list[tuple[str, str]]:
        return _collect_help_rows(self.get_params(ctx), ctx)

    def get_help(self, ctx: click.Context) -> str:
        console = Console(width=ctx.terminal_width or 120)

        with console.capture() as capture:
            _render_usage(console, self.get_usage(ctx))

            help_text = cleandoc(self.help or '').replace('\x08', '')
            if help_text:
                console.print(Padding(help_text, (0, 0, 0, 1)))

            console.print(_options_panel(self._help_options_rows(ctx)))

            footer = getattr(self, 'help_footer', None)
            if footer:
                console.print(f'\n{footer}')

        return capture.get()


class StyledGroup(click.Group):
    """Click group with Rich help rendering."""

    command_class = StyledCommand

    def _alias_map(self) -> dict[str, list[str]]:
        """Return canonical-command -> aliases mapping."""
        return {}

    def _help_options_rows(self, ctx: click.Context) -> list[tuple[str, str]]:
        return _collect_help_rows(self.get_params(ctx), ctx)

    def _help_commands_rows(self, ctx: click.Context) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        alias_map = self._alias_map()

        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue

            desc = cmd.get_short_help_str(limit=150)
            aliases = alias_map.get(name, [])
            if aliases:
                quoted = ', '.join(f'`{alias}`' for alias in sorted(aliases))
                alias_label = 'alias' if len(aliases) == 1 else 'aliases'
                alias_text = f'{alias_label}: {quoted}'
                if desc:
                    desc = f'{desc.rstrip(".")}, {alias_text}'
                else:
                    desc = alias_text

            rows.append((name, desc))

        return rows

    def get_help(self, ctx: click.Context) -> str:
        console = Console(width=ctx.terminal_width or 120)

        with console.capture() as capture:
            _render_usage(console, self.get_usage(ctx))

            help_text = cleandoc(self.help or '')
            if help_text:
                console.print(
                    Padding(
                        f'[bright_white]{escape(_single_paragraph(help_text))}[/bright_white]',
                        (0, 0, 0, 1),
                    )
                )
                console.print()

            console.print(_options_panel(self._help_options_rows(ctx)))
            console.print()
            console.print(_section_panel('Commands', self._help_commands_rows(ctx)))

            footer = getattr(self, 'help_footer', None)
            if footer:
                console.print(f'\n{footer}')

        return capture.get()


class StyledAliasGroup(StyledGroup):
    """Styled group with command alias support."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aliases: dict[str, str] = {}

    def add_alias(self, name: str, alias: str) -> None:
        """Register an alias for an existing command."""
        self._aliases[alias] = name

    def get_command(self, ctx: click.Context, cmd_name: str):
        canonical = self._aliases.get(cmd_name, cmd_name)
        return super().get_command(ctx, canonical)

    def _alias_map(self) -> dict[str, list[str]]:
        reverse: dict[str, list[str]] = {}
        for alias, canonical in self._aliases.items():
            reverse.setdefault(canonical, []).append(alias)
        return reverse
