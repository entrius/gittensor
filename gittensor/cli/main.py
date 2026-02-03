# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Gittensor CLI - Main entry point

Usage:
    gitt issue ...     - Issue bounty commands
    gitt config        - Show configuration
"""

import click
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table


console = Console()

# Config paths
GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'


@click.group()
@click.version_option(version='3.2.0', prog_name='gittensor')
def cli():
    """Gittensor CLI - Manage issue bounties and validator operations"""
    pass


@cli.command('config')
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


# Import and register issue commands
# Use absolute import from the package-level cli
from .issue_commands import register_issue_commands
register_issue_commands(cli)


def main():
    """Main entry point for the CLI"""
    cli()


if __name__ == '__main__':
    main()
