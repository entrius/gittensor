# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Gittensor CLI - Main entry point

Usage:
    gitt config              - Show/set CLI configuration
    gitt issues ...          - Issue management (alias: i)
    gitt harvest             - Harvest emissions
    gitt vote ...            - Validator vote commands
    gitt admin ...           - Owner commands (alias: a)
"""

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from gittensor.cli.issue_commands import register_commands
from gittensor.cli.issue_commands.help import StyledAliasGroup, StyledGroup

console = Console()

# Config paths
GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'


@click.group(cls=StyledAliasGroup)
@click.version_option(version='3.2.0', prog_name='gittensor')
def cli():
    """Gittensor CLI - Manage issue bounties and validator operations"""
    pass


@click.group(name='config', cls=StyledGroup, invoke_without_command=True)
@click.pass_context
def config_group(ctx):
    """Show current configuration (default) or set configuration values."""
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

    [dim]Use this command to override values stored in `~/.gittensor/config.json`.[/dim]

    [dim]Common keys:
        wallet              Wallet name
        hotkey              Hotkey name
        contract_address    Contract address
        ws_endpoint         WebSocket endpoint
        network             Network (local, test, finney)
    [/dim]

    [dim]Examples:
        $ gitt config set wallet alice
        $ gitt config set contract_address 5Cxxx...
        $ gitt config set network local
    [/dim]
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

# Register miner commands
from gittensor.cli.miner_commands import register_miner_commands  # noqa: E402

register_miner_commands(cli)


# Register issue commands with new flat structure
register_commands(cli)


def main():
    """Main entry point for the CLI"""
    cli()


if __name__ == '__main__':
    main()
