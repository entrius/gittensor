# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for managing Gittensor CLI configuration.

All settings stored in ~/.gittensor/config.json
"""

import json
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

# Config file location
GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'

console = Console()


def load_config() -> dict:
    """Load configuration from file."""
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

    All settings are stored in ~/.gittensor/config.json

    \b
    Available keys:
        wallet            Default wallet name
        hotkey            Default hotkey name
        network           Network (local, testnet, mainnet)
        ws_endpoint       WebSocket RPC endpoint
        api_url           Gittensor API URL
        contract_address  Smart contract address (use with caution)

    \b
    Examples:
        gitt config                       # Show current config
        gitt config wallet mywallet       # Set default wallet
        gitt config ws_endpoint ws://...  # Set RPC endpoint
    """
    if ctx.invoked_subcommand is None:
        show_config()


def show_config():
    """Display current configuration."""
    cfg = load_config()

    if not cfg:
        console.print('\n[yellow]No configuration set.[/yellow]')
        console.print('[dim]Use "gitt config <key> <value>" to set values.[/dim]')
        console.print('\n[dim]Keys: wallet, hotkey, network, ws_endpoint, api_url, contract_address[/dim]')
        return

    console.print('\n[bold cyan]Gittensor CLI Configuration[/bold cyan]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('Setting', style='cyan')
    table.add_column('Value', style='green')

    # Display order
    key_order = ['network', 'wallet', 'hotkey', 'ws_endpoint', 'api_url', 'contract_address']
    for key in key_order:
        if key in cfg:
            value = cfg[key]
            # Truncate long addresses
            if key == 'contract_address' and len(str(value)) > 20:
                value = f'{value[:12]}...{value[-8:]}'
            table.add_row(key, str(value))

    # Any other keys
    for key, value in sorted(cfg.items()):
        if key not in key_order:
            table.add_row(key, str(value))

    console.print(table)
    console.print(f'\n[dim]Config file: {CONFIG_FILE}[/dim]')


@config.command('wallet')
@click.argument('name', required=False)
def config_wallet(name: Optional[str]):
    """Set or show the default wallet name."""
    cfg = load_config()
    if name is None:
        console.print(f'[cyan]wallet:[/cyan] {cfg.get("wallet", "(not set)")}')
        return
    cfg['wallet'] = name
    if save_config(cfg):
        console.print(f'[green]wallet = {name}[/green]')


@config.command('hotkey')
@click.argument('name', required=False)
def config_hotkey(name: Optional[str]):
    """Set or show the default hotkey name."""
    cfg = load_config()
    if name is None:
        console.print(f'[cyan]hotkey:[/cyan] {cfg.get("hotkey", "(not set)")}')
        return
    cfg['hotkey'] = name
    if save_config(cfg):
        console.print(f'[green]hotkey = {name}[/green]')


@config.command('network')
@click.argument('name', required=False, type=click.Choice(['local', 'testnet', 'mainnet']))
def config_network(name: Optional[str]):
    """Set or show the network (local, testnet, mainnet)."""
    cfg = load_config()
    if name is None:
        console.print(f'[cyan]network:[/cyan] {cfg.get("network", "(not set)")}')
        return
    cfg['network'] = name
    if save_config(cfg):
        console.print(f'[green]network = {name}[/green]')
        endpoints = {
            'local': 'ws://127.0.0.1:9944',
            'testnet': 'wss://test.finney.opentensor.ai:443',
            'mainnet': 'wss://entrypoint-finney.opentensor.ai:443',
        }
        console.print(f'[dim]Hint: ws_endpoint for {name} is typically {endpoints.get(name)}[/dim]')


@config.command('ws_endpoint')
@click.argument('url', required=False)
def config_ws_endpoint(url: Optional[str]):
    """Set or show the WebSocket RPC endpoint."""
    cfg = load_config()
    if url is None:
        console.print(f'[cyan]ws_endpoint:[/cyan] {cfg.get("ws_endpoint", "(not set)")}')
        return
    cfg['ws_endpoint'] = url
    if save_config(cfg):
        console.print(f'[green]ws_endpoint = {url}[/green]')


@config.command('api_url')
@click.argument('url', required=False)
def config_api_url(url: Optional[str]):
    """Set or show the Gittensor API URL."""
    cfg = load_config()
    if url is None:
        console.print(f'[cyan]api_url:[/cyan] {cfg.get("api_url", "(not set)")}')
        return
    cfg['api_url'] = url
    if save_config(cfg):
        console.print(f'[green]api_url = {url}[/green]')


@config.command('contract_address')
@click.argument('address', required=False)
@click.option('--force', '-f', is_flag=True, help='Skip safety warning')
def config_contract_address(address: Optional[str], force: bool):
    """Set or show the smart contract address.

    WARNING: Changing this affects which contract your CLI interacts with.
    Only change if you know what you're doing (e.g., deploying a new contract).

    This is typically set automatically by deployment scripts.
    """
    cfg = load_config()
    if address is None:
        current = cfg.get('contract_address', '(not set)')
        console.print(f'[cyan]contract_address:[/cyan] {current}')
        return

    # Safety warning
    if not force:
        console.print('\n[bold yellow]WARNING: Changing contract_address is dangerous![/bold yellow]')
        console.print('[yellow]This determines which smart contract the CLI interacts with.[/yellow]')
        console.print('[yellow]Only proceed if you are deploying a new contract or know what you are doing.[/yellow]\n')

        if cfg.get('contract_address'):
            console.print(f'[dim]Current: {cfg["contract_address"]}[/dim]')
            console.print(f'[dim]New:     {address}[/dim]\n')

        if not click.confirm('Are you sure you want to change contract_address?', default=False):
            console.print('[yellow]Cancelled.[/yellow]')
            return

    cfg['contract_address'] = address
    if save_config(cfg):
        console.print(f'[green]contract_address = {address}[/green]')


@config.command('set')
@click.option('--wallet', help='Wallet name')
@click.option('--hotkey', help='Hotkey name')
@click.option('--network', type=click.Choice(['local', 'testnet', 'mainnet']), help='Network')
@click.option('--ws-endpoint', help='WebSocket RPC endpoint')
@click.option('--api-url', help='Gittensor API URL')
def config_set(
    wallet: Optional[str],
    hotkey: Optional[str],
    network: Optional[str],
    ws_endpoint: Optional[str],
    api_url: Optional[str],
):
    """Set multiple config values at once.

    Note: contract_address cannot be set via this command for safety.
    Use 'gitt config contract_address <addr>' with confirmation.
    """
    cfg = load_config()
    changed = []

    if wallet:
        cfg['wallet'] = wallet
        changed.append(f'wallet={wallet}')
    if hotkey:
        cfg['hotkey'] = hotkey
        changed.append(f'hotkey={hotkey}')
    if network:
        cfg['network'] = network
        changed.append(f'network={network}')
    if ws_endpoint:
        cfg['ws_endpoint'] = ws_endpoint
        changed.append(f'ws_endpoint={ws_endpoint}')
    if api_url:
        cfg['api_url'] = api_url
        changed.append(f'api_url={api_url}')

    if changed and save_config(cfg):
        console.print(f'[green]Config updated: {", ".join(changed)}[/green]')
    elif not changed:
        console.print('[yellow]No options provided.[/yellow]')
        console.print('[dim]Use --wallet, --hotkey, --network, --ws-endpoint, --api-url[/dim]')


@config.command('clear')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def config_clear(force: bool):
    """Clear all configuration."""
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
