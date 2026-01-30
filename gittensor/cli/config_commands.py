# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for managing Gittensor CLI configuration.

Users can configure:
- Wallet name (default coldkey)
- Hotkey name (default hotkey)
- Network (local, testnet, mainnet)
"""

import json
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Config file location
GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'

console = Console()


def load_config() -> dict:
    """Load unified configuration from file."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_config(config: dict) -> bool:
    """Save configuration to file."""
    GITTENSOR_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except IOError as e:
        console.print(f'[red]Failed to save config: {e}[/red]')
        return False


def get_config_value(key: str, default: str = '') -> str:
    """Get a config value with optional default."""
    config = load_config()
    return config.get(key, default)


@click.group(invoke_without_command=True)
@click.pass_context
def config(ctx):
    """Manage CLI configuration.

    Configure default wallet, hotkey, and network settings.
    These defaults are used when not specified via CLI options.

    \b
    Examples:
        gitt config                   # Show current config
        gitt config wallet mywallet   # Set default wallet
        gitt config hotkey myhotkey   # Set default hotkey
        gitt config network local     # Set network to local
    """
    # If no subcommand, show current config
    if ctx.invoked_subcommand is None:
        show_config()


def show_config():
    """Display current configuration."""
    config = load_config()

    if not config:
        console.print('\n[yellow]No configuration set.[/yellow]')
        console.print('[dim]Use "gitt config <key> <value>" to set values.[/dim]')
        console.print('\n[dim]Available keys: wallet, hotkey, network[/dim]')
        return

    console.print('\n[bold cyan]Gittensor CLI Configuration[/bold cyan]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('Setting', style='cyan')
    table.add_column('Value', style='green')

    for key, value in sorted(config.items()):
        table.add_row(key, str(value))

    console.print(table)
    console.print(f'\n[dim]Config file: {CONFIG_FILE}[/dim]')


@config.command('wallet')
@click.argument('name', required=False)
def config_wallet(name: Optional[str]):
    """Set or show the default wallet name.

    \b
    Arguments:
        NAME: Wallet name to set (optional, shows current if omitted)

    \b
    Examples:
        gitt config wallet              # Show current wallet
        gitt config wallet owner-wallet # Set wallet to owner-wallet
    """
    config = load_config()

    if name is None:
        current = config.get('wallet', '(not set)')
        console.print(f'[cyan]Current wallet:[/cyan] {current}')
        return

    config['wallet'] = name
    if save_config(config):
        console.print(f'[green]Wallet set to:[/green] {name}')


@config.command('hotkey')
@click.argument('name', required=False)
def config_hotkey(name: Optional[str]):
    """Set or show the default hotkey name.

    \b
    Arguments:
        NAME: Hotkey name to set (optional, shows current if omitted)

    \b
    Examples:
        gitt config hotkey          # Show current hotkey
        gitt config hotkey default  # Set hotkey to default
    """
    config = load_config()

    if name is None:
        current = config.get('hotkey', '(not set)')
        console.print(f'[cyan]Current hotkey:[/cyan] {current}')
        return

    config['hotkey'] = name
    if save_config(config):
        console.print(f'[green]Hotkey set to:[/green] {name}')


@config.command('network')
@click.argument('name', required=False, type=click.Choice(['local', 'testnet', 'mainnet']))
def config_network(name: Optional[str]):
    """Set or show the default network.

    \b
    Arguments:
        NAME: Network name (local, testnet, or mainnet)

    \b
    Examples:
        gitt config network         # Show current network
        gitt config network local   # Set network to local
    """
    config = load_config()

    if name is None:
        current = config.get('network', '(not set)')
        console.print(f'[cyan]Current network:[/cyan] {current}')
        return

    config['network'] = name
    if save_config(config):
        console.print(f'[green]Network set to:[/green] {name}')

        # Show endpoint hint based on network
        endpoints = {
            'local': 'ws://127.0.0.1:9944',
            'testnet': 'wss://test.finney.opentensor.ai:443',
            'mainnet': 'wss://entrypoint-finney.opentensor.ai:443',
        }
        console.print(f'[dim]Default endpoint: {endpoints.get(name, "unknown")}[/dim]')


@config.command('set')
@click.option('--wallet', help='Wallet name')
@click.option('--hotkey', help='Hotkey name')
@click.option('--network', type=click.Choice(['local', 'testnet', 'mainnet']), help='Network')
def config_set(wallet: Optional[str], hotkey: Optional[str], network: Optional[str]):
    """Set multiple config values at once.

    \b
    Examples:
        gitt config set --wallet contract-treasury --hotkey contract-treasury
        gitt config set --wallet mywallet --hotkey myhotkey --network local
    """
    config = load_config()
    changed = []
    if wallet:
        config['wallet'] = wallet
        changed.append(f'wallet={wallet}')
    if hotkey:
        config['hotkey'] = hotkey
        changed.append(f'hotkey={hotkey}')
    if network:
        config['network'] = network
        changed.append(f'network={network}')

    if changed and save_config(config):
        console.print(f'[green]Config updated: {", ".join(changed)}[/green]')
    elif not changed:
        console.print('[yellow]No options provided. Use --wallet, --hotkey, or --network[/yellow]')


@config.command('clear')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def config_clear(force: bool):
    """Clear all configuration.

    \b
    Example:
        gitt config clear
        gitt config clear --force
    """
    if not CONFIG_FILE.exists():
        console.print('[yellow]No configuration to clear.[/yellow]')
        return

    if not force and not click.confirm('Clear all configuration?', default=False):
        console.print('[yellow]Cancelled.[/yellow]')
        return

    try:
        CONFIG_FILE.unlink()
        console.print('[green]Configuration cleared.[/green]')
    except IOError as e:
        console.print(f'[red]Failed to clear config: {e}[/red]')


def register_config_commands(cli):
    """Register config commands with a parent CLI group."""
    cli.add_command(config)
