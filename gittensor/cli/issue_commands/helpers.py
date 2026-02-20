# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Shared helper functions for issue commands
"""

import hashlib
import json
import os
import re
import struct
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.text import Text

from gittensor.constants import CONTRACT_ADDRESS

# Default paths
GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'

console = Console()

# ALPHA token formatting constants
ALPHA_DECIMALS = 9
ALPHA_SCALE = 10**ALPHA_DECIMALS
MIN_BOUNTY_ALPHA = Decimal('10')
MAX_BOUNTY_ALPHA = Decimal('100_000_000')  # 100M ALPHA — prevents u128 encoding overflow
MAX_ISSUE_ID = 1_000_000


def format_alpha(raw_amount: int, decimals: int = 2) -> str:
    """
    Format a raw nanoALPHA integer for display.

    Args:
        raw_amount: Amount in nanoALPHA (raw contract units)
        decimals: Number of decimal places to show (default 2)

    Returns:
        Formatted string like "100.00 ALPHA"
    """
    value = Decimal(raw_amount) / Decimal(ALPHA_SCALE)
    return f'{value:.{decimals}f} ALPHA'


def print_success(message: str) -> None:
    """Print a success message with a green checkmark."""
    console.print(f'\n[green]\u2713[/green] {message}\n')


def print_error(message: str) -> None:
    """Print an error message with a red X."""
    console.print(f'\n[red]\u2717[/red] {message}\n')


def print_network_header(network_name: str, contract_addr: str) -> None:
    """Print a compact network/contract context header."""
    short = f'{contract_addr[:12]}...{contract_addr[-6:]}' if len(contract_addr) > 20 else contract_addr
    console.print(f'[dim]Network: {network_name} \u2022 Contract: {short}[/dim]')


STATUS_COLORS = {
    'Active': 'green',
    'Registered': 'yellow',
    'Completed': 'dim',
    'Cancelled': 'dim',
}


def colorize_status(status: str) -> Text:
    """Return a Rich Text object with the status colored by issue state."""
    color = STATUS_COLORS.get(status, 'white')
    return Text(status, style=color)


def validate_and_convert_bounty(bounty) -> int:
    """
    Validate and convert a bounty amount from ALPHA to nanoALPHA.

    Accepts str, int, or float. When --bounty is typed as str in Click,
    the value arrives as a string and is parsed directly into Decimal,
    completely avoiding IEEE 754 floating-point precision loss.

    Args:
        bounty: Bounty amount in ALPHA tokens (str, int, or float)

    Returns:
        Raw nanoALPHA integer

    Raises:
        click.BadParameter: If bounty is invalid
    """
    import math

    # Reject non-finite floats (inf, -inf, nan) before Decimal conversion
    if isinstance(bounty, float) and not math.isfinite(bounty):
        raise click.BadParameter(f'Invalid bounty amount: {bounty}', param_hint='--bounty')

    try:
        d = Decimal(str(bounty))
    except (InvalidOperation, ValueError):
        raise click.BadParameter(f'Invalid bounty amount: {bounty}', param_hint='--bounty')

    if d < MIN_BOUNTY_ALPHA:
        raise click.BadParameter(
            f'Bounty must be at least {MIN_BOUNTY_ALPHA} ALPHA (got {bounty})', param_hint='--bounty'
        )

    if d > MAX_BOUNTY_ALPHA:
        raise click.BadParameter(
            f'Bounty must be at most {MAX_BOUNTY_ALPHA:,} ALPHA (got {bounty})', param_hint='--bounty'
        )

    # Check for excessive decimal places
    if d.as_tuple().exponent < -ALPHA_DECIMALS:
        raise click.BadParameter(f'Bounty has too many decimal places (max {ALPHA_DECIMALS})', param_hint='--bounty')

    raw = int(d * ALPHA_SCALE)
    if raw <= 0:
        raise click.BadParameter(f'Bounty converts to 0 nanoALPHA: {bounty}', param_hint='--bounty')

    return raw


# Regex for valid GitHub repo name characters
_REPO_NAME_RE = re.compile(r'^[A-Za-z0-9\-._]+$')


def validate_repo_format(repo: str) -> tuple:
    """
    Validate that a repository string is in owner/repo format.

    Args:
        repo: Repository string to validate

    Returns:
        Tuple of (owner, repo_name)

    Raises:
        click.BadParameter: If format is invalid
    """
    parts = repo.split('/')
    if len(parts) != 2:
        raise click.BadParameter(f'Repository must be in owner/repo format (got "{repo}")', param_hint='--repo')

    owner, name = parts
    if not owner or not name:
        raise click.BadParameter(
            f'Repository owner and name must both be non-empty (got "{repo}")', param_hint='--repo'
        )

    if ' ' in repo:
        raise click.BadParameter(f'Repository must not contain spaces (got "{repo}")', param_hint='--repo')

    if not _REPO_NAME_RE.match(owner) or not _REPO_NAME_RE.match(name):
        raise click.BadParameter(
            f'Repository owner/name may only contain alphanumeric characters, hyphens, dots, and underscores (got "{repo}")',
            param_hint='--repo',
        )

    return owner, name


_GITHUB_API_TIMEOUT = 10


def check_github_repo_exists(repo: str) -> bool:
    """
    Check if a GitHub repository exists via the public API.

    Args:
        repo: Repository in owner/repo format

    Returns:
        True if the repo exists or check was inconclusive (network error / rate limit)

    Raises:
        click.BadParameter: If the repo definitively does not exist (404)
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{repo}',
            headers={'User-Agent': 'gittensor-cli'},
        )
        urllib.request.urlopen(req, timeout=_GITHUB_API_TIMEOUT)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise click.BadParameter(f"Repository '{repo}' not found on GitHub", param_hint='--repo')
        if e.code == 403:
            console.print(f'[yellow]Warning: GitHub API rate-limited, skipping repo check for "{repo}"[/yellow]')
            return True
        console.print(f'[yellow]Warning: GitHub API returned {e.code} for "{repo}", skipping check[/yellow]')
        return True
    except Exception as e:
        console.print(f'[yellow]Warning: Could not verify GitHub repo "{repo}": {e}[/yellow]')
        return True


def validate_issue_id(value: int, label: str = 'issue ID', param_hint: str = None) -> int:
    """
    Validate that an issue ID is within a reasonable range.

    Args:
        value: Issue ID to validate
        label: Human-readable label for error messages
        param_hint: CLI parameter name for error context

    Returns:
        The validated issue ID

    Raises:
        click.BadParameter: If value is out of range
    """
    if value < 1:
        raise click.BadParameter(f'{label} must be >= 1 (got {value})', param_hint=param_hint)
    if value >= MAX_ISSUE_ID:
        raise click.BadParameter(f'{label} must be < {MAX_ISSUE_ID:,} (got {value})', param_hint=param_hint)
    return value


def check_github_issue_exists(repo: str, issue_number: int) -> bool:
    """
    Check if a GitHub issue exists, is not a pull request, and is open.

    Args:
        repo: Repository in owner/repo format
        issue_number: GitHub issue number

    Returns:
        True if the issue exists (or check was inconclusive)

    Raises:
        click.BadParameter: If issue does not exist or is a pull request
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/issues/{issue_number}',
            headers={'User-Agent': 'gittensor-cli'},
        )
        resp = urllib.request.urlopen(req, timeout=_GITHUB_API_TIMEOUT)
        data = json.loads(resp.read().decode())
        if 'pull_request' in data:
            raise click.BadParameter(f'#{issue_number} in {repo} is a pull request, not an issue', param_hint='--issue')
        if data.get('state') == 'closed':
            console.print(f'[yellow]Warning: Issue #{issue_number} is already closed.[/yellow]')
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise click.BadParameter(f'GitHub issue #{issue_number} not found in {repo}', param_hint='--issue')
        if e.code == 403:
            console.print('[yellow]Warning: GitHub API rate-limited, skipping issue check[/yellow]')
            return True
        console.print(f'[yellow]Warning: GitHub API returned {e.code}, skipping issue check[/yellow]')
        return True
    except click.BadParameter:
        raise
    except Exception as e:
        console.print(f'[yellow]Warning: Could not verify GitHub issue #{issue_number}: {e}[/yellow]')
        return True


_SS58_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{46,48}$')


def validate_ss58_address(address: str, label: str = 'address', param_hint: str = None) -> str:
    """
    Validate an SS58 address using substrateinterface with regex fallback.

    Args:
        address: SS58 address string to validate
        label: Human-readable label for error messages
        param_hint: CLI parameter name for error context

    Returns:
        The cleaned (stripped) address string

    Raises:
        click.BadParameter: If the address is invalid
    """
    address = address.strip()
    if not address:
        raise click.BadParameter(f'{label} must not be empty', param_hint=param_hint)

    try:
        from substrateinterface import Keypair

        Keypair(ss58_address=address)
    except ImportError:
        # Fallback: regex check for base58 format (no checksum validation)
        if not _SS58_RE.match(address):
            raise click.BadParameter(f'Invalid SS58 address: {address}', param_hint=param_hint)
    except Exception:
        raise click.BadParameter(f'Invalid SS58 address: {address}', param_hint=param_hint)

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


def get_contract_address(cli_value: str = '') -> str:
    """
    Get contract address. CLI arg > env var > constants.py default.

    Args:
        cli_value: Value passed via --contract CLI option

    Returns:
        Contract address string
    """
    if cli_value:
        return cli_value
    return os.environ.get('CONTRACT_ADDRESS') or CONTRACT_ADDRESS


NETWORK_MAP = {
    'finney': 'wss://entrypoint-finney.opentensor.ai:443',
    'test': 'wss://test.finney.opentensor.ai:443',
    'local': 'ws://127.0.0.1:9944',
}

# Reverse lookup: URL -> network name
_URL_TO_NETWORK = {url: name for name, url in NETWORK_MAP.items()}


def resolve_network(network: Optional[str] = None, rpc_url: Optional[str] = None) -> tuple:
    """
    Resolve --network and --rpc-url into (endpoint, network_name).

    Priority:
        1. --rpc-url (explicit URL always wins)
        2. --network (mapped to known endpoint)
        3. Config file ws_endpoint / network
        4. Default: finney (mainnet)

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

    # Fall back to config file
    config = load_config()
    if config.get('ws_endpoint'):
        endpoint = config['ws_endpoint']
        name = _URL_TO_NETWORK.get(endpoint, config.get('network', 'custom'))
        return endpoint, name

    # Default: finney (mainnet)
    return NETWORK_MAP['finney'], 'finney'


# ============================================================================
# Contract storage reading helpers (shared by view and admin commands)
# ============================================================================


def _get_contract_child_storage_key(substrate, contract_addr: str, verbose: bool = False) -> Optional[str]:
    """
    Get the child storage key for a contract's trie.

    Args:
        substrate: SubstrateInterface instance
        contract_addr: Contract address
        verbose: If True, print debug output

    Returns:
        Hex-encoded child storage key or None if contract doesn't exist
    """
    try:
        contract_info = substrate.query('Contracts', 'ContractInfoOf', [contract_addr])
        if not contract_info or not contract_info.value:
            if verbose:
                console.print(f'[dim]Debug: Contract not found at {contract_addr}[/dim]')
            return None

        trie_id_hex = contract_info.value['trie_id'].replace('0x', '')
        prefix = b':child_storage:default:'
        trie_id_bytes = bytes.fromhex(trie_id_hex)
        return '0x' + (prefix + trie_id_bytes).hex()
    except Exception as e:
        if verbose:
            console.print(f'[dim]Debug: Contract info query failed: {e}[/dim]')
        return None


def _read_contract_packed_storage(substrate, contract_addr: str, verbose: bool = False) -> Optional[Dict[str, Any]]:
    """
    Read the packed root storage from a contract using childstate RPC

    This bypasses the broken state_call/ContractsApi_call method and reads
    storage directly. Works around substrate-interface Ink! 5 compatibility issues.

    Args:
        substrate: SubstrateInterface instance
        contract_addr: Contract address
        verbose: If True, print debug output

    Returns:
        Dict with owner, netuid, next_issue_id, etc. or None on error
    """
    child_key = _get_contract_child_storage_key(substrate, contract_addr, verbose)
    if not child_key:
        if verbose:
            console.print('[dim]Debug: Failed to get contract child storage key[/dim]')
        return None

    # Get all storage keys for this contract
    keys_result = substrate.rpc_request('childstate_getKeysPaged', [child_key, '0x', 100, None, None])
    keys = keys_result.get('result', [])

    if verbose:
        console.print(f'[dim]Debug: Found {len(keys)} storage keys in contract[/dim]')

    # Find the packed storage key (ends with 00000000)
    packed_key = None
    for k in keys:
        if k.endswith('00000000'):
            packed_key = k
            break

    if not packed_key:
        if verbose:
            console.print('[dim]Debug: No packed storage key (ending in 00000000) found[/dim]')
        return None

    # Read the packed storage value
    val_result = substrate.rpc_request('childstate_getStorage', [child_key, packed_key, None])
    if not val_result.get('result'):
        if verbose:
            console.print('[dim]Debug: Failed to read packed storage value[/dim]')
        return None

    data = bytes.fromhex(val_result['result'].replace('0x', ''))
    if verbose:
        console.print(f'[dim]Debug: Packed storage data length = {len(data)} bytes[/dim]')

    # Decode packed struct (matches IssueBountyManager in lib.rs):
    # owner: AccountId (32 bytes)
    # treasury_hotkey: AccountId (32 bytes)
    # netuid: u16 (2 bytes)
    # next_issue_id: u64 (8 bytes)
    # alpha_pool: u128 (16 bytes)
    # Total: 74 bytes minimum

    if len(data) < 74:
        if verbose:
            console.print(f'[dim]Debug: Packed storage too small ({len(data)} < 74 bytes)[/dim]')
        return None

    offset = 0
    owner = data[offset : offset + 32]
    offset += 32
    treasury = data[offset : offset + 32]
    offset += 32
    netuid = struct.unpack_from('<H', data, offset)[0]
    offset += 2
    next_issue_id = struct.unpack_from('<Q', data, offset)[0]
    offset += 8
    alpha_pool_lo, alpha_pool_hi = struct.unpack_from('<QQ', data, offset)
    alpha_pool = alpha_pool_lo + (alpha_pool_hi << 64)

    return {
        'owner': substrate.ss58_encode(owner.hex()),
        'treasury_hotkey': substrate.ss58_encode(treasury.hex()),
        'netuid': netuid,
        'next_issue_id': next_issue_id,
        'alpha_pool': alpha_pool,
    }


def _compute_ink5_lazy_key(root_key_hex: str, encoded_key: bytes) -> str:
    """
    Compute Ink! 5 lazy mapping storage key using blake2_128concat.

    Args:
        root_key_hex: Hex string of the mapping root key (e.g., '52789899')
        encoded_key: SCALE-encoded key bytes

    Returns:
        Hex-encoded storage key
    """
    root_key = bytes.fromhex(root_key_hex.replace('0x', ''))
    # Blake2_128Concat: blake2_128(root_key || encoded_key) || root_key || encoded_key
    data = root_key + encoded_key
    h = hashlib.blake2b(data, digest_size=16).digest()
    return '0x' + (h + data).hex()


def _read_issues_from_child_storage(substrate, contract_addr: str, verbose: bool = False) -> List[Dict[str, Any]]:
    """
    Read all issues from contract child storage.

    Uses Ink! 5 lazy mapping key computation to directly read issue storage.

    Args:
        substrate: SubstrateInterface instance
        contract_addr: Contract address
        verbose: If True, print debug output

    Returns:
        List of issue dictionaries
    """
    child_key = _get_contract_child_storage_key(substrate, contract_addr, verbose)
    if not child_key:
        if verbose:
            console.print('[dim]Debug: Cannot read issues - no child storage key[/dim]')
        return []

    # First, read packed storage to get next_issue_id
    packed_storage = _read_contract_packed_storage(substrate, contract_addr, verbose)
    if not packed_storage:
        if verbose:
            console.print('[dim]Debug: Cannot read issues - packed storage read failed[/dim]')
        return []

    next_issue_id = packed_storage.get('next_issue_id', 1)
    if verbose:
        console.print(f'[dim]Debug: next_issue_id from contract = {next_issue_id}[/dim]')

    # Sanity check: next_issue_id should be reasonable (< 1 million for any real deployment)
    if next_issue_id > MAX_ISSUE_ID:
        console.print(f'[yellow]Warning: next_issue_id ({next_issue_id}) is unreasonably large.[/yellow]')
        console.print('[yellow]This may indicate a storage format mismatch. Check contract version.[/yellow]')
        return []

    # If next_issue_id is 1, no issues have been registered yet
    if next_issue_id <= 1:
        if verbose:
            console.print('[dim]Debug: No issues registered (next_issue_id <= 1)[/dim]')
        return []

    issues = []
    status_names = ['Registered', 'Active', 'Completed', 'Cancelled']

    # Iterate through all issue IDs (1 to next_issue_id - 1)
    # Issues mapping root key is '52789899'
    if verbose:
        console.print(f'[dim]Debug: Reading issues 1 to {next_issue_id - 1} using mapping key 52789899[/dim]')

    for issue_id in range(1, next_issue_id):
        # SCALE encode u64 as little-endian 8 bytes
        encoded_id = struct.pack('<Q', issue_id)
        lazy_key = _compute_ink5_lazy_key('52789899', encoded_id)

        val_result = substrate.rpc_request('childstate_getStorage', [child_key, lazy_key, None])
        if not val_result.get('result'):
            if verbose:
                console.print(f'[dim]Debug: No storage found for issue_id={issue_id} (key={lazy_key[:20]}...)[/dim]')
            continue

        data = bytes.fromhex(val_result['result'].replace('0x', ''))

        try:
            # Decode Issue struct:
            # id: u64 (8 bytes)
            # github_url_hash: [u8; 32] (32 bytes)
            # repository_full_name: String (compact len + bytes)
            # issue_number: u32 (4 bytes)
            # bounty_amount: u128 (16 bytes)
            # target_bounty: u128 (16 bytes)
            # status: IssueStatus enum (1 byte)
            # registered_at_block: u32 (4 bytes)

            offset = 0
            stored_issue_id = struct.unpack_from('<Q', data, offset)[0]
            offset += 8
            offset += 32  # Skip url_hash

            # String: compact-encoded length then bytes
            len_byte = data[offset]
            if len_byte & 0x03 == 0:
                str_len = len_byte >> 2
                offset += 1
            elif len_byte & 0x03 == 1:
                # Two-byte length
                str_len = (data[offset] | (data[offset + 1] << 8)) >> 2
                offset += 2
            else:
                str_len = 0
                offset += 1

            repo_name = data[offset : offset + str_len].decode('utf-8', errors='replace')
            offset += str_len

            issue_number = struct.unpack_from('<I', data, offset)[0]
            offset += 4

            bounty_lo, bounty_hi = struct.unpack_from('<QQ', data, offset)
            bounty_amount = bounty_lo + (bounty_hi << 64)
            offset += 16

            target_lo, target_hi = struct.unpack_from('<QQ', data, offset)
            target_bounty = target_lo + (target_hi << 64)
            offset += 16

            status_byte = data[offset]
            status = status_names[status_byte] if status_byte < len(status_names) else 'Unknown'

            issues.append(
                {
                    'id': stored_issue_id,
                    'repository_full_name': repo_name,
                    'issue_number': issue_number,
                    'bounty_amount': bounty_amount,
                    'target_bounty': target_bounty,
                    'status': status,
                }
            )
            if verbose:
                console.print(f'[dim]Debug: Decoded issue {stored_issue_id}: {repo_name}#{issue_number}[/dim]')
        except Exception as e:
            if verbose:
                console.print(f'[dim]Debug: Failed to decode issue {issue_id}: {e}[/dim]')
            continue

    # Sort by ID
    issues.sort(key=lambda x: x['id'])
    return issues


def read_issues_from_contract(ws_endpoint: str, contract_addr: str, verbose: bool = False) -> List[Dict[str, Any]]:
    """
    Read issues directly from the smart contract (no API dependency).

    Uses childstate_getStorage RPC to read contract storage directly,
    bypassing the broken ContractsApi_call method in substrate-interface.

    Args:
        ws_endpoint: WebSocket endpoint for Subtensor
        contract_addr: Contract address
        verbose: If True, print debug output

    Returns:
        List of issue dictionaries
    """
    try:
        from substrateinterface import SubstrateInterface

        if verbose:
            console.print(f'[dim]Debug: Connecting to {ws_endpoint}...[/dim]')

        # Connect to subtensor
        substrate = SubstrateInterface(url=ws_endpoint)

        if verbose:
            console.print('[dim]Debug: Connected successfully[/dim]')

        # Read issues directly from child storage
        return _read_issues_from_child_storage(substrate, contract_addr, verbose)

    except ImportError as e:
        console.print(f'[yellow]Cannot read from contract: {e}[/yellow]')
        console.print('[dim]Install with: pip install substrate-interface[/dim]')
        return []
    except Exception as e:
        if verbose:
            console.print(f'[dim]Debug: Connection/read error: {e}[/dim]')
        console.print(f'[yellow]Error reading from contract: {e}[/yellow]')
        return []
