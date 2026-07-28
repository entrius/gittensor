# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared CLI infrastructure: config/network resolution, option decorators,
console output, confirmation prompts, and input validation. Command groups
(issues, miner, repo, validator) build on these."""

import json
import re
import sys
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, NoReturn, Optional, Tuple, TypeVar

import click
import requests
from bittensor_wallet.utils import is_valid_ss58_address
from click.core import ParameterSource
from rich.console import Console

from gittensor.cli.core.json_output import emit_error_json
from gittensor.constants import BASE_GITHUB_API_URL, NETWORK_MAP

# Default CLI config paths
GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'

# ALPHA token conversion
ALPHA_DECIMALS = 9
ALPHA_RAW_UNIT = 10**ALPHA_DECIMALS
REPO_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$')
GITHUB_API_TIMEOUT = 10


console = Console()
err_console = Console(stderr=True)

CommandFunc = TypeVar('CommandFunc', bound=Callable[..., Any])
NETWORK_CHOICE = click.Choice(['finney', 'test', 'local'], case_sensitive=False)


def apply_click_options(*decorators: Callable[[CommandFunc], CommandFunc]) -> Callable[[CommandFunc], CommandFunc]:
    """Apply Click decorators in the declared display order."""

    def wrapper(func: CommandFunc) -> CommandFunc:
        for decorator in reversed(decorators):
            func = decorator(func)
        return func

    return wrapper


def with_wallet_options(
    wallet_default: str = 'default', hotkey_default: str = 'default'
) -> Callable[[CommandFunc], CommandFunc]:
    """Add the standard wallet name/hotkey options."""
    return apply_click_options(
        click.option(
            '--wallet-name',
            '--wallet.name',
            '--wallet',
            default=wallet_default,
            help='Wallet name',
        ),
        click.option(
            '--wallet-hotkey',
            '--wallet.hotkey',
            '--hotkey',
            default=hotkey_default,
            help='Hotkey name',
        ),
    )


def with_network_contract_options(
    contract_help: str,
) -> Callable[[CommandFunc], CommandFunc]:
    """Add the standard network / rpc / contract option bundle."""
    return apply_click_options(
        click.option(
            '--network',
            '-n',
            default=None,
            type=NETWORK_CHOICE,
            help='Network (finney/test/local)',
        ),
        click.option(
            '--rpc-url',
            default=None,
            help='Subtensor RPC endpoint (overrides --network)',
        ),
        click.option(
            '--contract',
            default='',
            help=contract_help,
        ),
    )


def with_cli_behavior_options(
    *,
    include_verbose: bool = False,
    include_json: bool = False,
    include_yes: bool = False,
    verbose_help: str = 'Show debug output',
    json_help: str = 'Output as JSON for scripting',
    yes_help: str = 'Skip confirmation prompt (non-interactive/CI). Alias: --no-prompt (btcli-compatible).',
) -> Callable[[CommandFunc], CommandFunc]:
    """Add common CLI behavior options such as verbose, JSON, and confirmation controls."""
    decorators: list[Callable[[CommandFunc], CommandFunc]] = []

    if include_verbose:
        decorators.append(
            click.option(
                '--verbose',
                '-v',
                is_flag=True,
                help=verbose_help,
            )
        )
    if include_json:
        decorators.append(
            click.option(
                '--json',
                'as_json',
                is_flag=True,
                help=json_help,
            )
        )
    if include_yes:
        decorators.append(
            click.option(
                '--yes',
                '--no-prompt',
                '-y',
                'yes',
                is_flag=True,
                help=yes_help,
            )
        )

    return apply_click_options(*decorators)


def format_alpha(raw_amount: int, decimals: int = 2) -> str:
    """Format raw token amount (9-decimal) as human-readable ALPHA string.

    Uses Decimal to avoid float rounding in display.
    """
    if raw_amount == 0:
        return f'{0:.{decimals}f}'
    q = Decimal(raw_amount) / Decimal(ALPHA_RAW_UNIT)
    return f'{q:.{decimals}f}'


def print_success(message: str) -> None:
    """Print a success status message to stderr."""
    err_console.print(f'\n[green]✓ {message}[/green]\n', highlight=True)


def print_error(message: str) -> None:
    """Print a standardized error message."""
    err_console.print(f'\n[red]✗ {message}[/red]\n', highlight=True)


def print_warning(message: str) -> None:
    """Print a warning message."""
    err_console.print(f'\n[yellow]{message}[/yellow]\n', highlight=True)


def handle_exception(as_json: bool, message: str, error_type: str = 'cli_error') -> NoReturn:
    """Emit a CLI error in JSON or human format and exit non-zero."""
    if as_json:
        emit_error_json(message, error_type=error_type)
    else:
        print_error(message)
    raise SystemExit(1)


def _handle_command_error(e: Exception) -> None:
    """Print a terminal error message and exit. Gives a tailored message for missing dependencies."""
    if isinstance(e, ImportError):
        print_error(f'Missing dependency — {e}')
    else:
        print_error(str(e))
    raise SystemExit(1)


def loading_context(message: str, as_json: bool, spinner: str = 'dots', color='cyan') -> ContextManager[Any]:
    """Return a spinner context in human mode, or a no-op context in JSON mode."""
    return (
        nullcontext()
        if as_json
        else err_console.status(f'[{color}]{message}[/{color}]', spinner=spinner, spinner_style=color)
    )


def print_network_header(network_name: str, contract_addr: str) -> None:
    """Print a one-line network and contract context header to stderr."""
    short = f'{contract_addr[:12]}...{contract_addr[-6:]}' if len(contract_addr) > 20 else contract_addr
    err_console.print(f'Network: {network_name} • Contract: {short}')


def _is_interactive() -> bool:
    """Return True if stdin is a TTY (interactive session)."""
    return getattr(sys.stdin, 'isatty', lambda: False)()


def confirm_or_abort(prompt: str, yes: bool, default: bool = False) -> bool:
    """Prompt for confirmation before a destructive operation.

    Returns True if the caller should proceed. Returns False (and prints a
    cancellation message) if the user declines. `yes` and non-TTY input both
    skip the prompt and proceed.
    """
    if yes or not _is_interactive():
        return True
    if click.confirm(f'\n{prompt}', default=default):
        return True
    err_console.print('[yellow]Cancelled.[/yellow]')
    return False


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _raise_github_verification_required(
    check: str,
    detail: str,
    *,
    param_hint: str,
) -> None:
    """Raise BadParameter when a GitHub existence probe could not complete.

    Used by the strict variants of validate_repository and validate_github_issue
    so mutation commands never fall through to on-chain writes on transient failures.
    """
    raise click.BadParameter(
        f'Could not verify {check} on GitHub ({detail}). Try again when GitHub is reachable.',
        param_hint=param_hint,
    )


def validate_repository(
    repo: str,
    verify_exists: bool = True,
    *,
    require_verified_exists: bool = False,
) -> Tuple[str, str]:
    """Validate owner/repo format and optionally verify it exists on GitHub.

    Returns (owner, repo_name) on success. Raises click.BadParameter on failure.
    Pass require_verified_exists=True to abort on transient GitHub errors instead
    of warning and continuing; requires verify_exists=True.
    """
    if require_verified_exists and not verify_exists:
        raise ValueError('require_verified_exists requires verify_exists=True')
    repo = repo.strip()

    if not REPO_PATTERN.match(repo):
        raise click.BadParameter(
            f'Repository must be in owner/repo format with alphanumeric characters, '
            f"hyphens, underscores, or dots (got '{repo}')",
            param_hint='--repo',
        )

    owner, repo_name = repo.split('/', 1)

    if verify_exists:
        try:
            resp = requests.get(
                f'{BASE_GITHUB_API_URL}/repos/{owner}/{repo_name}',
                headers={'User-Agent': 'gittensor-cli'},
                timeout=GITHUB_API_TIMEOUT,
            )
            if resp.status_code == 404:
                raise click.BadParameter(
                    f"Repository '{owner}/{repo_name}' not found on GitHub",
                    param_hint='--repo',
                )
            if not resp.ok:
                if require_verified_exists:
                    _raise_github_verification_required(
                        f"repository '{owner}/{repo_name}'",
                        f'status {resp.status_code}',
                        param_hint='--repo',
                    )
                err_console.print(
                    f'[yellow]Warning: GitHub API returned {resp.status_code} — skipping existence check[/yellow]'
                )
        except requests.RequestException as exc:
            if require_verified_exists:
                detail = type(exc).__name__
                _raise_github_verification_required(
                    f"repository '{owner}/{repo_name}'",
                    detail,
                    param_hint='--repo',
                )
            err_console.print('[yellow]Warning: Could not reach GitHub API — skipping existence check[/yellow]')

    return owner, repo_name


def validate_github_issue(
    owner: str,
    repo: str,
    issue_number: int,
    *,
    require_verified_exists: bool = False,
) -> Optional[Dict[str, Any]]:
    """Verify a GitHub issue exists, is open, and is not a pull request.

    Returns the issue JSON data on success, or None if verification was skipped
    due to network issues. Raises click.BadParameter on validation failure.
    Pass require_verified_exists=True to abort on transient errors instead of
    warning and continuing.
    """
    try:
        resp = requests.get(
            f'{BASE_GITHUB_API_URL}/repos/{owner}/{repo}/issues/{issue_number}',
            headers={'User-Agent': 'gittensor-cli'},
            timeout=GITHUB_API_TIMEOUT,
        )
    except requests.RequestException as exc:
        if require_verified_exists:
            detail = type(exc).__name__
            _raise_github_verification_required(
                f'issue #{issue_number} in {owner}/{repo}',
                detail,
                param_hint='--issue',
            )
        err_console.print('[yellow]Warning: Could not reach GitHub API — skipping issue check[/yellow]')
        return None

    if resp.status_code == 404:
        raise click.BadParameter(
            f'Issue #{issue_number} not found in {owner}/{repo}',
            param_hint='--issue',
        )
    if not resp.ok:
        if require_verified_exists:
            _raise_github_verification_required(
                f'issue #{issue_number} in {owner}/{repo}',
                f'status {resp.status_code}',
                param_hint='--issue',
            )
        err_console.print(f'[yellow]Warning: GitHub API returned {resp.status_code} — skipping issue check[/yellow]')
        return None

    data = resp.json()

    if 'pull_request' in data:
        raise click.BadParameter(
            f'#{issue_number} is a pull request, not an issue',
            param_hint='--issue',
        )

    state = data.get('state', 'unknown')
    if state != 'open':
        if state == 'closed':
            err_console.print(f'[yellow]Warning: Issue #{issue_number} is already closed.[/yellow]')
        else:
            err_console.print(f'[yellow]Warning: Issue #{issue_number} is {state}.[/yellow]')

    return data


def validate_ss58_address(address: str, param_name: str = 'address') -> str:
    """Validate an SS58 address via bittensor-wallet's base58+checksum check."""
    address = address.strip()
    if not address:
        raise click.BadParameter(f'Empty {param_name}', param_hint=param_name)
    if not is_valid_ss58_address(address):
        raise click.BadParameter(
            f'Invalid SS58 address for {param_name}: {address}',
            param_hint=param_name,
        )
    return address


def load_config() -> Dict[str, Any]:
    """
    Load configuration from ~/.gittensor/config.json.

    Priority:
    1. CLI arguments (highest - handled by callers)
    2. ~/.gittensor/config.json
    3. Defaults

    Config file format:
        {
            "contract_address": "5Cxxx...",
            "ws_endpoint": "wss://entrypoint-finney.opentensor.ai:443",
            "network": "finney",
            "wallet": "default",
            "hotkey": "default"
        }

    Manage via: gitt config <key> <value>

    Returns:
        Dict with all config keys
    """
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def resolve_wallet_config(
    wallet_name: str,
    wallet_hotkey: str,
    *,
    wallet_default: str = 'default',
    hotkey_default: str = 'default',
) -> Tuple[str, str]:
    """Resolve wallet values against the CLI config file."""
    ctx = click.get_current_context(silent=True)
    config = load_config()

    if ctx is None:
        wallet_explicit = wallet_name != wallet_default
        hotkey_explicit = wallet_hotkey != hotkey_default
    else:
        wallet_explicit = ctx.get_parameter_source('wallet_name') == ParameterSource.COMMANDLINE
        hotkey_explicit = ctx.get_parameter_source('wallet_hotkey') == ParameterSource.COMMANDLINE

    effective_wallet = wallet_name if wallet_explicit else config.get('wallet', wallet_default)
    effective_hotkey = wallet_hotkey if hotkey_explicit else config.get('hotkey', hotkey_default)
    return effective_wallet, effective_hotkey


def get_contract_address(cli_value: str = '') -> str:
    """
    Get contract address.

    Priority:
        1. --contract CLI option
        2. ~/.gittensor/config.json `contract_address`
        3. CONTRACT_ADDRESS env var
        4. constants.py default

    Mirrors the resolution order documented on ``load_config`` and already
    honored by ``resolve_network`` in this module.

    Args:
        cli_value: Value passed via --contract CLI option

    Returns:
        Contract address string
    """
    from gittensor.utils.utils import get_contract_address as _get_contract_address

    if cli_value:
        return cli_value
    config_value = load_config().get('contract_address')
    if config_value:
        return config_value
    return _get_contract_address()


# Reverse lookup: URL -> network name
_URL_TO_NETWORK = {url: name for name, url in NETWORK_MAP.items()}


def resolve_network(network: Optional[str] = None, rpc_url: Optional[str] = None) -> tuple:
    """
    Resolve --network and --rpc-url into (endpoint, network_name).

    Priority:
        1. --rpc-url (explicit URL always wins)
        2. --network (mapped to known endpoint)
        3. Config file `network` (when set to a known network)
        4. Config file `ws_endpoint` (custom URL fallback)
        5. Default: finney (mainnet)

    Args:
        network: Network name from --network option (test/finney/local)
        rpc_url: Explicit RPC URL from --rpc-url option

    Returns:
        Tuple of (ws_endpoint, network_name)
    """
    # --rpc-url takes highest priority
    if rpc_url:
        name = _URL_TO_NETWORK.get(rpc_url, 'custom')
        return rpc_url, name

    # --network maps to a known endpoint
    if network:
        key = network.lower()
        if key in NETWORK_MAP:
            return NETWORK_MAP[key], key
        # Treat unknown network value as a custom URL
        return network, 'custom'

    # Fall back to config file. Prefer an explicit, recognized `network` over
    # `ws_endpoint`: a stale dev `ws_endpoint` (e.g. localhost) shouldn't silently
    # override a user who set `network: finney` to point at mainnet.
    config = load_config()

    config_network = config.get('network', '').lower()
    if config_network and config_network in NETWORK_MAP:
        return NETWORK_MAP[config_network], config_network

    if config.get('ws_endpoint'):
        endpoint = config['ws_endpoint']
        name = _URL_TO_NETWORK.get(endpoint, config.get('network', 'custom'))
        return endpoint, name

    # Default: finney (mainnet)
    return NETWORK_MAP['finney'], 'finney'


def _resolve_contract_and_network(
    contract: str,
    network: Optional[str] = None,
    rpc_url: Optional[str] = None,
    *,
    missing_contract_message: str = 'Contract address not configured.',
) -> Tuple[str, str, str]:
    """Resolve contract address, WS endpoint, and network name from CLI options.

    Combines get_contract_address and resolve_network into one call, raising
    click.ClickException when the contract address is empty.

    Returns (contract_addr, ws_endpoint, network_name).
    """
    contract_addr = get_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)
    if not contract_addr:
        raise click.ClickException(missing_contract_message)
    return contract_addr, ws_endpoint, network_name
