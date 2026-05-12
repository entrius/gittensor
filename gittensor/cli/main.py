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
import os
import sys

# Stub heavy imports during shell completion and --help so tab-completion stays
# fast and bittensor's argparse doesn't hijack click's help output.
if os.environ.get('_GITT_COMPLETE') or any(arg in ('-h', '--help') for arg in sys.argv[1:]):
    import types as _types

    class _Stub(_types.ModuleType):
        def __getattr__(self, _name):
            return self

        def __call__(self, *_a, **_kw):
            return self

    _stub = _Stub('_gitt_cli_stub')
    for _pkg in ('bittensor', 'requests'):
        sys.modules[_pkg] = _stub

import click
from click.shell_completion import get_completion_class
from rich.table import Table

from gittensor import __version__
from gittensor.cli.issue_commands import register_commands
from gittensor.cli.issue_commands.help import StyledAliasGroup, StyledGroup
from gittensor.cli.issue_commands.helpers import CONFIG_FILE, GITTENSOR_DIR, console, emit_error_json, err_console


def _argv_requests_machine_json(argv: list[str]) -> bool:
    """True if the user asked for JSON on stdout (matches issue/miner flags)."""
    return '--json' in argv or '--json-output' in argv


def _click_exception_error_type(exc: click.ClickException) -> str:
    """Map Click exceptions to the same ``error.type`` strings issue commands use."""
    if isinstance(exc, click.BadParameter):
        return 'bad_parameter'
    if isinstance(exc, click.UsageError):
        return 'usage_error'
    return 'click_exception'


class GittensorRootCli(StyledAliasGroup):
    """Root group: in machine-json mode, Click parse/usage errors emit the same JSON envelope as commands."""

    def main(
        self,
        args=None,
        prog_name=None,
        complete_var=None,
        standalone_mode=True,
        windows_expand_args=True,
        **extra,
    ):
        json_argv = list(sys.argv[1:] if args is None else args)
        if standalone_mode and _argv_requests_machine_json(json_argv):
            try:
                return super().main(
                    args=args,
                    prog_name=prog_name,
                    complete_var=complete_var,
                    standalone_mode=False,
                    windows_expand_args=windows_expand_args,
                    **extra,
                )
            except click.ClickException as e:
                emit_error_json(str(e), error_type=_click_exception_error_type(e))
                sys.exit(e.exit_code)
        return super().main(
            args=args,
            prog_name=prog_name,
            complete_var=complete_var,
            standalone_mode=standalone_mode,
            windows_expand_args=windows_expand_args,
            **extra,
        )


@click.group(cls=GittensorRootCli)
@click.version_option(version=__version__, prog_name='gittensor')
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
    err_console.print('\n[bold]Gittensor CLI Configuration[/bold]\n')

    if not CONFIG_FILE.exists():
        err_console.print('[yellow]No config file found at ~/.gittensor/config.json[/yellow]')
        err_console.print('[dim]Run ./up.sh --issues to create config[/dim]')
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
        err_console.print(f'\n[dim]Config file: {CONFIG_FILE}[/dim]\n')

    except json.JSONDecodeError:
        err_console.print('[red]Error: Invalid JSON in config file[/red]')
    except Exception as e:
        err_console.print(f'[red]Error reading config: {e}[/red]')


CONFIG_KEYS = ('wallet', 'hotkey', 'network', 'contract_address', 'ws_endpoint')


@config_group.command('set')
@click.argument('key', type=click.Choice(CONFIG_KEYS, case_sensitive=False))
@click.argument('value', type=str)
def config_set(key: str, value: str):
    """Set a configuration value.

    [dim]Use this command to override values stored in `~/.gittensor/config.json`.
    KEY must be one of the recognised settings — unknown keys are rejected so a
    typo (for example `wallet_name`) cannot silently write a dead entry that
    downstream commands will ignore.[/dim]

    [dim]Recognised keys:
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
    key = key.lower()
    # Ensure config directory exists
    GITTENSOR_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing config or start fresh
    config = {}
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            err_console.print('[yellow]Warning: Existing config was invalid, starting fresh[/yellow]')

    # Set the value
    old_value = config.get(key)
    config[key] = value

    # Write config
    CONFIG_FILE.write_text(json.dumps(config, indent=2))

    if old_value is not None:
        err_console.print(f'[green]Updated {key}:[/green] {old_value} → {value}')
    else:
        err_console.print(f'[green]Set {key}:[/green] {value}')


def _detect_shell():
    """Detect the current shell from the SHELL environment variable"""
    shell_path = os.environ.get('SHELL', '')
    shell_name = os.path.basename(shell_path)
    if shell_name in ('bash', 'zsh', 'fish'):
        return shell_name
    return None


@cli.command('completion')
@click.argument('shell', type=click.Choice(['bash', 'zsh', 'fish']), default=None, required=False)
def completion(shell):
    """Generate shell completion script

    Install completions:
        bash:  eval "$(gitt completion bash)"
        zsh:   eval "$(gitt completion zsh)"
        fish:  gitt completion fish | source

    If shell is omitted, auto-detects from the SHELL environment variable.
    """
    if shell is None:
        shell = _detect_shell()
        if shell is None:
            raise click.UsageError('Cannot detect shell. Please specify one of: bash, zsh, fish')
    cls = get_completion_class(shell)
    if cls is None:
        raise click.UsageError(f'Unsupported shell: {shell}')
    comp = cls(cli, ctx_args={}, prog_name='gitt', complete_var='_GITT_COMPLETE')
    click.echo(comp.source())


# Register config group
cli.add_command(config_group)

# Register miner commands
from gittensor.cli.miner_commands import register_miner_commands  # noqa: E402

register_miner_commands(cli)


# Register issue commands with new flat structure
register_commands(cli)


def main():
    """Main entry point for the CLI."""
    cli()


if __name__ == '__main__':
    main()
