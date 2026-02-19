# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Gittensor CLI - Main entry point

Usage:
    gitt config              - Show/set CLI configuration
    gitt issues ...          - Issue management (alias: i, is)
    gitt harvest             - Harvest emissions (alias: h)
    gitt vote ...            - Validator vote commands (alias: v)
    gitt admin ...           - Owner commands (alias: a, adm)
"""

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from gittensor.cli.issue_commands import register_commands

console = Console()

# Config paths
GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'


class AliasGroup(click.Group):
    """Click Group that supports command aliases without duplicate help entries."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aliases = {}  # alias -> canonical name

    def add_alias(self, name, alias):
        """Register an alias for an existing command."""
        self._aliases[alias] = name

    def get_command(self, ctx, cmd_name):
        # Resolve alias to canonical name
        canonical = self._aliases.get(cmd_name, cmd_name)
        return super().get_command(ctx, canonical)

    def format_commands(self, ctx, formatter):
        """Write the help text, appending aliases to command descriptions."""
        # Build reverse map: canonical -> list of aliases
        alias_map = {}
        for alias, canonical in self._aliases.items():
            alias_map.setdefault(canonical, []).append(alias)

        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.commands.get(subcommand)
            if cmd is None or cmd.hidden:
                continue
            help_text = cmd.get_short_help_str(limit=150)
            aliases = alias_map.get(subcommand)
            if aliases:
                alias_str = ', '.join(sorted(aliases))
                subcommand = f'{subcommand}, {alias_str}'
            commands.append((subcommand, help_text))

        if commands:
            with formatter.section('Commands'):
                formatter.write_dl(commands)


@click.group(cls=AliasGroup)
@click.version_option(version='3.2.0', prog_name='gittensor')
def cli():
    """Gittensor CLI - Manage issue bounties and validator operations"""
    pass


@click.group(name='config', invoke_without_command=True)
@click.pass_context
def config_group(ctx):
    """CLI configuration management.

    Show current configuration (default) or set config values.

    \b
    Subcommands:
        set <key> <value>    Set a config value
    """
    # If no subcommand, show config
    if ctx.invoked_subcommand is None:
        show_config()


def show_config():
    """Show current CLI configuration"""
    console.print('\n[bold]Gittensor CLI Configuration[/bold]\n')

    if not CONFIG_FILE.exists():
        console.print('[yellow]No config file found at ~/.gittensor/config.json[/yellow]')
        console.print('[dim]Run ./up.sh --issues to create config[/dim]')
        return

    try:
        config = json.loads(CONFIG_FILE.read_text())

        table = Table(show_header=True)
        table.add_column('Setting', style='cyan')
        table.add_column('Value', style='green')

        for key, value in config.items():
            # Truncate long values
            str_val = str(value)
            if len(str_val) > 25:
                str_val = str_val[:12] + '...' + str_val[-10:]
            table.add_row(key, str_val)

        console.print(table)
        console.print(f'\n[dim]Config file: {CONFIG_FILE}[/dim]\n')

    except json.JSONDecodeError:
        console.print('[red]Error: Invalid JSON in config file[/red]')
    except Exception as e:
        console.print(f'[red]Error reading config: {e}[/red]')


@config_group.command('set')
@click.argument('key', type=str)
@click.argument('value', type=str)
def config_set(key: str, value: str):
    """Set a configuration value.

    \b
    Common keys:
        wallet              Wallet name
        hotkey              Hotkey name
        contract_address    Contract address
        ws_endpoint         WebSocket endpoint
        network             Network (local, test, finney)

    \b
    Examples:
        gitt config set wallet alice
        gitt config set contract_address 5Cxxx...
        gitt config set network local
    """
    # Ensure config directory exists
    GITTENSOR_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing config or start fresh
    config = {}
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            console.print('[yellow]Warning: Existing config was invalid, starting fresh[/yellow]')

    # Set the value
    old_value = config.get(key)
    config[key] = value

    # Write config
    CONFIG_FILE.write_text(json.dumps(config, indent=2))

    if old_value is not None:
        console.print(f'[green]Updated {key}:[/green] {old_value} → {value}')
    else:
        console.print(f'[green]Set {key}:[/green] {value}')


# Register config group
cli.add_command(config_group)


# Register issue commands with new flat structure
register_commands(cli)


def main():
    """Main entry point for the CLI"""
    cli()


if __name__ == '__main__':
    main()
