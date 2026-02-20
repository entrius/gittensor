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
import sys
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click
from rich.console import Console

from gittensor.constants import CONTRACT_ADDRESS

# ALPHA token conversion
ALPHA_DECIMALS = 9
ALPHA_RAW_UNIT = 10**ALPHA_DECIMALS
MIN_BOUNTY_ALPHA = 10.0
MAX_BOUNTY_ALPHA = 100_000_000.0
MAX_ISSUE_ID = 1_000_000
MAX_ISSUE_NUMBER = 2**32 - 1
REPO_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$')
GITHUB_API_TIMEOUT = 10

# Status display colors
STATUS_COLORS: Dict[str, str] = {
    'Active': 'green',
    'Registered': 'yellow',
    'Completed': 'dim',
    'Cancelled': 'dim',
}

# Default paths
GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'

console = Console()


def format_alpha(raw_amount: int, decimals: int = 2) -> str:
    """Format raw token amount (9-decimal) as human-readable ALPHA string.

    Uses Decimal to avoid float rounding in display.
    """
    if raw_amount == 0:
        return f'{0:.{decimals}f}'
    q = Decimal(raw_amount) / Decimal(ALPHA_RAW_UNIT)
    return f'{q:.{decimals}f}'


def colorize_status(status: str) -> str:
    """Wrap status text with the appropriate Rich color tag."""
    color = STATUS_COLORS.get(status, 'white')
    return f'[{color}]{status}[/{color}]'


def print_success(message: str) -> None:
    """Print a standardized success message."""
    console.print(f'\n  [green]\u2713[/green] {message}\n')


def print_error(message: str) -> None:
    """Print a standardized error message."""
    console.print(f'\n  [red]\u2717[/red] {message}\n')


def print_network_header(network_name: str, contract_addr: str) -> None:
    """Print a one-line network and contract context header."""
    short = f'{contract_addr[:12]}...{contract_addr[-6:]}' if len(contract_addr) > 20 else contract_addr
    console.print(f'[dim]Network: {network_name} \u2022 Contract: {short}[/dim]\n')


def _is_interactive() -> bool:
    """Return True if stdin is a TTY (interactive session)."""
    return getattr(sys.stdin, 'isatty', lambda: False)()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def validate_bounty_amount(bounty: float) -> int:
    """Validate bounty and convert to raw ALPHA units without precision loss.

    Raises click.BadParameter if the value has too many decimals or is below
    the contract minimum.
    """
    if bounty < MIN_BOUNTY_ALPHA:
        raise click.BadParameter(
            f'Minimum bounty is {MIN_BOUNTY_ALPHA} ALPHA (got {bounty})',
            param_hint='--bounty',
        )

    if bounty > MAX_BOUNTY_ALPHA:
        raise click.BadParameter(
            f'Bounty cannot exceed {MAX_BOUNTY_ALPHA:,.0f} ALPHA',
            param_hint='--bounty',
        )

    try:
        d = Decimal(str(bounty))
    except InvalidOperation:
        raise click.BadParameter(f'Invalid number: {bounty}', param_hint='--bounty')

    if not d.is_finite():
        raise click.BadParameter(f'Bounty must be a finite number (got {bounty})', param_hint='--bounty')

    sign, digits, exponent = d.as_tuple()
    decimal_places = max(0, -exponent)
    if decimal_places > ALPHA_DECIMALS:
        raise click.BadParameter(
            f'Maximum {ALPHA_DECIMALS} decimal places allowed (got {decimal_places})',
            param_hint='--bounty',
        )

    raw = int(d * ALPHA_RAW_UNIT)
    if raw <= 0:
        raise click.BadParameter('Bounty must result in a positive amount', param_hint='--bounty')

    return raw


def validate_repository(repo: str, verify_exists: bool = True) -> Tuple[str, str]:
    """Validate owner/repo format and optionally verify it exists on GitHub.

    Returns (owner, repo_name) on success.
    Raises click.BadParameter on failure.
    """
    repo = repo.strip()

    if not REPO_PATTERN.match(repo):
        raise click.BadParameter(
            f'Repository must be in owner/repo format with alphanumeric characters, '
            f"hyphens, underscores, or dots (got '{repo}')",
            param_hint='--repo',
        )

    owner, repo_name = repo.split('/', 1)

    if verify_exists:
        url = f'https://api.github.com/repos/{owner}/{repo_name}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'gittensor-cli'})
            urllib.request.urlopen(req, timeout=GITHUB_API_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise click.BadParameter(
                    f"Repository '{owner}/{repo_name}' not found on GitHub",
                    param_hint='--repo',
                )
            # Non-404 HTTP errors: warn but don't block
            console.print(f'[yellow]Warning: GitHub API returned {e.code} — skipping existence check[/yellow]')
        except (urllib.error.URLError, OSError):
            console.print('[yellow]Warning: Could not reach GitHub API — skipping existence check[/yellow]')

    return owner, repo_name


def validate_github_issue(owner: str, repo: str, issue_number: int) -> Optional[Dict[str, Any]]:
    """Verify a GitHub issue exists, is open, and is not a pull request.

    Returns the issue JSON data on success, or None if verification was skipped
    due to network issues.  Raises click.BadParameter on validation failure.
    """
    url = f'https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'gittensor-cli'})
        resp = urllib.request.urlopen(req, timeout=GITHUB_API_TIMEOUT)
        data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise click.BadParameter(
                f'Issue #{issue_number} not found in {owner}/{repo}',
                param_hint='--issue',
            )
        console.print(f'[yellow]Warning: GitHub API returned {e.code} — skipping issue check[/yellow]')
        return None
    except (urllib.error.URLError, OSError):
        console.print('[yellow]Warning: Could not reach GitHub API — skipping issue check[/yellow]')
        return None

    if 'pull_request' in data:
        raise click.BadParameter(
            f'#{issue_number} is a pull request, not an issue',
            param_hint='--issue',
        )

    state = data.get('state', 'unknown')
    if state != 'open':
        console.print(f'[yellow]Warning: Issue #{issue_number} is {state}[/yellow]')

    return data


def validate_issue_id(value: int, param_name: str = 'issue_id') -> int:
    """Validate an on-chain issue ID is in a reasonable range (1 to 999999)."""
    if value < 1 or value >= MAX_ISSUE_ID:
        raise click.BadParameter(
            f'{param_name} must be between 1 and {MAX_ISSUE_ID - 1} (got {value})',
            param_hint=param_name,
        )
    return value


def validate_ss58_address(address: str, param_name: str = 'address') -> str:
    """Validate an SS58 address.

    Uses scalecodec's ss58_decode for proper base58+checksum validation.
    Falls back to a length/prefix heuristic if scalecodec is not installed.
    """
    address = address.strip()
    if not address:
        raise click.BadParameter(f'Empty {param_name}', param_hint=param_name)

    try:
        from scalecodec.utils.ss58 import ss58_decode

        ss58_decode(address)
        return address
    except ImportError:
        pass
    except Exception:
        raise click.BadParameter(
            f'Invalid SS58 address for {param_name}: {address}',
            param_hint=param_name,
        )

    # Fallback: basic structure check (starts with 1-9/A-H/J-N/P-Z, 46-48 chars)
    if not re.match(r'^[1-9A-HJ-NP-Za-km-z]{46,48}$', address):
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
    MAX_REASONABLE_ISSUE_ID = 1_000_000
    if next_issue_id > MAX_REASONABLE_ISSUE_ID:
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
