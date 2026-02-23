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
import urllib.parse
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
MIN_BOUNTY_ALPHA = 10
MAX_BOUNTY_ALPHA = 100_000_000
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


def validate_bounty_amount(bounty: str) -> int:
    """Validate bounty and convert to raw ALPHA units without precision loss.

    Accepts a string so Click does not parse as float (avoids IEEE 754 loss at
    the CLI boundary). Caps at 100M ALPHA to avoid u128 overflow. Raises
    click.BadParameter if invalid, below minimum, or above maximum.
    """
    bounty = bounty.strip()
    if not bounty:
        raise click.BadParameter('Bounty cannot be empty', param_hint='--bounty')

    try:
        d = Decimal(bounty)
    except InvalidOperation:
        raise click.BadParameter(f'Invalid number: {bounty}', param_hint='--bounty')

    if not d.is_finite():
        raise click.BadParameter(f'Bounty must be a finite number (got {bounty})', param_hint='--bounty')

    if d < MIN_BOUNTY_ALPHA:
        raise click.BadParameter(
            f'Minimum bounty is {MIN_BOUNTY_ALPHA} ALPHA (got {bounty})',
            param_hint='--bounty',
        )

    if d > MAX_BOUNTY_ALPHA:
        raise click.BadParameter(
            f'Bounty cannot exceed {MAX_BOUNTY_ALPHA:,} ALPHA',
            param_hint='--bounty',
        )

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
        if state == 'closed':
            console.print(f'[yellow]Warning: Issue #{issue_number} is already closed.[/yellow]')
        else:
            console.print(f'[yellow]Warning: Issue #{issue_number} is {state}.[/yellow]')

    return data


def validate_issue_id(value: int, param_name: str = 'issue_id') -> int:
    """Validate an on-chain issue ID (1 to 999999, u32-friendly range)."""
    if value < 1 or value >= MAX_ISSUE_ID:
        raise click.BadParameter(
            f'{param_name} must be between 1 and {MAX_ISSUE_ID - 1} (got {value})',
            param_hint=param_name,
        )
    return value


def validate_ss58_address(address: str, param_name: str = 'address') -> str:
    """Validate an SS58 address.

    Uses substrate-interface's ss58_decode (existing stack) for base58+checksum
    validation. Falls back to a length/prefix regex if not available.
    """
    address = address.strip()
    if not address:
        raise click.BadParameter(f'Empty {param_name}', param_hint=param_name)

    try:
        from substrateinterface.utils.ss58 import ss58_decode

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


# ---------------------------------------------------------------------------
# Submissions / predict helpers
# ---------------------------------------------------------------------------

BOUNTIED_STATUSES = ('Registered', 'Active')


def get_github_pat() -> Optional[str]:
    """Return GITTENSOR_MINER_PAT from environment, or None."""
    return os.environ.get('GITTENSOR_MINER_PAT') or None


def fetch_issue_prs(repository_full_name: str, issue_number: int, token: Optional[str]) -> List[Dict[str, Any]]:
    """Fetch open PRs referencing the issue.

    Delegates to ``find_prs_for_issue`` in ``github_api_tools`` which cascades:
    GraphQL cross-reference timeline → authenticated REST → unauthenticated REST.
    """
    try:
        from gittensor.utils.github_api_tools import find_prs_for_issue

        return find_prs_for_issue(repository_full_name, issue_number, token=token, state_filter='open')
    except Exception as e:
        console.print(f'[yellow]Failed to fetch PRs from GitHub ({e}); showing none.[/yellow]')
        return []


def fetch_issue_from_contract(
    ws_endpoint: str,
    contract_addr: str,
    issue_id: int,
    require_active: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Resolve on-chain issue by ID, validate status. Raises ClickException on failure."""
    issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)
    issue = next((i for i in issues if i.get('id') == issue_id), None)
    if not issue:
        raise click.ClickException(f'Issue {issue_id} not found on-chain.')
    status = issue.get('status') or ''
    if status not in BOUNTIED_STATUSES:
        raise click.ClickException(f'Issue {issue_id} is not in a bountied state (status: {status}).')
    if require_active and status != 'Active':
        raise click.ClickException(
            f'Issue {issue_id} is not active (status: {status}). Predictions require Active status.'
        )
    repo = issue.get('repository_full_name', '')
    issue_number = issue.get('issue_number', 0)
    if not repo or not issue_number:
        raise click.ClickException('Issue missing repository or issue number.')
    return issue


def validate_probability(value: float, param_hint: str = 'probability') -> float:
    """Ensure probability is in [0.0, 1.0]. Raises click.BadParameter if not."""
    if not (0.0 <= value <= 1.0):
        raise click.BadParameter(
            f'Probability must be between 0.0 and 1.0 (got {value})',
            param_hint=param_hint,
        )
    return value


def validate_predictions(
    predictions: Dict[int, float],
    valid_pr_numbers: set,
    param_hint: str = 'predictions',
) -> None:
    """Validate PR existence and sum <= 1.0. Raises on failure with available PRs listed."""
    for num in predictions:
        if num not in valid_pr_numbers:
            available = sorted(valid_pr_numbers)
            raise click.BadParameter(
                f'PR #{num} is not an open PR for this issue. Open PRs: {available}',
                param_hint=param_hint,
            )
    total = sum(predictions.values())
    if total > 1.0:
        raise click.BadParameter(
            f'Sum of probabilities must be <= 1.0 (got {total:.4f})',
            param_hint=param_hint,
        )


def build_pr_table(prs: List[Dict[str, Any]], issue_number: Optional[int] = None):
    """Build a Rich Table of open PRs (reusable by submissions and predict).

    Args:
        prs: List of PR dicts from GitHub API.
        issue_number: If provided, adds a "Closes?" column indicating
            whether the PR references closing keywords for this issue.
    """
    from rich.table import Table

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('PR #', style='cyan', justify='right')
    table.add_column('Title', style='green', max_width=50)
    table.add_column('Author', style='yellow')
    table.add_column('Created', style='dim')
    table.add_column('Review', justify='right')
    if issue_number is not None:
        table.add_column('Closes?', justify='center')
    table.add_column('URL', style='dim')
    for pr in prs:
        created = (pr.get('created_at') or '')[:10]
        review = 'Approved' if (pr.get('review_count') or 0) > 0 else '\u2014'
        row = [
            str(pr.get('number', '')),
            (pr.get('title') or '')[:50],
            pr.get('author_login', ''),
            created,
            review,
        ]
        if issue_number is not None:
            closes = issue_number in (pr.get('closing_numbers') or [])
            row.append('[green]\u2713[/green]' if closes else '\u2014')
        row.append((pr.get('url') or '')[:60])
        table.add_row(*row)
    return table


def format_prediction_lines(predictions: Dict[int, float]) -> str:
    """Format predictions as indented lines for Rich panels."""
    lines = [f'  PR #{num}: {prob * 100:.2f}%' for num, prob in sorted(predictions.items())]
    total = sum(predictions.values())
    lines.append(f'Total: {total * 100:.2f}%')
    return '\n'.join(lines)


def collect_predictions(
    pr_number: Optional[int],
    probability: Optional[float],
    json_input: Optional[str],
    prs: List[Dict[str, Any]],
    pr_numbers: set,
    issue_id: int,
) -> Dict[int, float]:
    """Collect and validate predictions from one of three input modes.

    Modes:
      1. ``--json-input`` — batch predictions from JSON string
      2. ``--pr N --probability F`` — single prediction
      3. Interactive TTY prompt (default when neither is set)

    Handles flag conflict validation, JSON parsing, probability validation,
    and PR-set validation. Returns dict mapping PR number → probability.
    """
    # --- Flag conflict checks ---
    if pr_number is not None and json_input is not None:
        raise click.ClickException('Use either --pr/--probability or --json-input, not both.')
    if probability is not None and json_input is not None:
        raise click.ClickException('Use either --pr/--probability or --json-input, not both.')
    if pr_number is None and probability is not None and json_input is None:
        raise click.ClickException('--probability requires --pr.')

    # --- Mode 1: JSON batch ---
    if json_input is not None:
        predictions: Dict[int, float] = {}
        try:
            raw = json.loads(json_input)
            if not isinstance(raw, dict):
                raise click.BadParameter(
                    'JSON input must be an object: {"pr_number": probability, ...}',
                    param_hint='--json-input',
                )
            for k, v in raw.items():
                try:
                    pr_num = int(k)
                except (TypeError, ValueError):
                    raise click.BadParameter(f'Invalid PR number in JSON: {k}', param_hint='--json-input')
                try:
                    predictions[pr_num] = validate_probability(float(v), '--json-input')
                except (TypeError, ValueError):
                    raise click.BadParameter(
                        f'Invalid probability value for PR #{k} in JSON: {v}', param_hint='--json-input'
                    )
        except json.JSONDecodeError as e:
            raise click.BadParameter(f'Invalid JSON: {e}', param_hint='--json-input')
        validate_predictions(predictions, pr_numbers, '--json-input')
        return predictions

    # --- Mode 2: single --pr + --probability ---
    if pr_number is not None:
        if probability is None:
            raise click.ClickException('--probability is required when --pr is set.')
        predictions = {pr_number: validate_probability(probability, '--probability')}
        validate_predictions(predictions, pr_numbers, '--probability')
        return predictions

    # --- Mode 3: interactive TTY ---
    if not prs:
        raise click.ClickException('No open PRs for this issue.')
    console.print(f'[bold cyan]Predict merge probability for issue #{issue_id}[/bold cyan]\n')
    console.print(build_pr_table(prs))
    console.print(
        '\n[dim]Assign a probability (0.0\u20131.0) to each PR. Press Enter to skip. Total must be \u2264 1.0.[/dim]\n'
    )
    predictions = {}
    total = 0.0
    for pr in prs:
        num = pr.get('number')
        if num is None:
            continue
        while True:
            raw_input = click.prompt(
                f'  Probability for PR #{num} (0\u20131, blank to skip)', default='', show_default=False
            )
            if not raw_input.strip():
                break
            try:
                val = float(raw_input.strip())
                validate_probability(val, f'PR #{num}')
                if total + val > 1.0:
                    print_error(f'Sum would exceed 1.0 ({total:.2f} + {val:.2f}). Enter a lower value or skip.')
                    continue
                predictions[num] = val
                total += val
                if total >= 0.99:
                    console.print(f'[yellow]Running total: {total:.2f} \u2014 approaching 1.0[/yellow]')
                else:
                    console.print(f'[dim]Running total: {total:.2f}[/dim]')
                break
            except (click.BadParameter, ValueError):
                console.print('[yellow]Enter a number between 0.0 and 1.0[/yellow]')
    if not predictions:
        raise click.ClickException('No predictions entered.')
    validate_predictions(predictions, pr_numbers, 'predictions')
    return predictions


def build_prediction_payload(
    issue_id: int,
    repository: str,
    issue_number: int,
    miner_hotkey: str,
    predictions: Dict[int, float],
) -> Dict[str, Any]:
    """Build the validated payload structured for future synapse broadcast.

    PR numbers are stringified in the predictions dict for JSON serialization
    compatibility (JSON keys must be strings).
    """
    return {
        'issue_id': issue_id,
        'repository': repository,
        'issue_number': issue_number,
        'miner_hotkey': miner_hotkey,
        'predictions': {str(k): v for k, v in predictions.items()},
    }


def read_netuid_from_contract(ws_endpoint: str, contract_addr: str, verbose: bool = False) -> Optional[int]:
    """Read the subnet netuid from contract packed storage."""
    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        packed = _read_contract_packed_storage(substrate, contract_addr, verbose)
        if packed and packed.get('netuid') is not None:
            return int(packed['netuid'])
    except Exception as e:
        if verbose:
            console.print(f'[dim]Debug: Failed to read netuid: {e}[/dim]')
    return None


def verify_miner_registration(
    ws_endpoint: str,
    contract_addr: str,
    hotkey_ss58: str,
    verbose: bool = False,
) -> bool:
    """Return True if hotkey is registered on the subnet (netuid from contract)."""
    try:
        import bittensor as bt

        netuid = read_netuid_from_contract(ws_endpoint, contract_addr, verbose)
        if netuid is None:
            return False
        subtensor = bt.Subtensor(network=ws_endpoint)
        return bool(subtensor.is_hotkey_registered(netuid=netuid, hotkey_ss58=hotkey_ss58))
    except Exception:
        return False
