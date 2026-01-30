# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Shared helper functions for issue commands.
"""

import hashlib
import json
import os
import struct
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Optional, Dict, Any

from rich.console import Console

# CLI config file location (same as config_commands.py)
CLI_CONFIG_FILE = Path.home() / '.gittensor' / 'cli_config.json'

# Default paths and URLs
GITTENSOR_DIR = Path.home() / '.gittensor'
ISSUE_PREFERENCES_FILE = GITTENSOR_DIR / 'issue_preferences.json'
CONTRACT_CONFIG_FILE = GITTENSOR_DIR / 'contract_config.json'
DEFAULT_API_URL = 'http://localhost:3000'

console = Console()


def load_cli_config() -> dict:
    """Load CLI configuration from file."""
    if not CLI_CONFIG_FILE.exists():
        return {}
    try:
        with open(CLI_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def get_preferences_file() -> Path:
    """Get the path to the issue preferences file, creating directory if needed."""
    GITTENSOR_DIR.mkdir(parents=True, exist_ok=True)
    return ISSUE_PREFERENCES_FILE


def load_contract_config() -> Dict[str, Any]:
    """
    Load contract configuration from environment or config file.

    Priority:
    1. CONTRACT_ADDRESS environment variable
    2. ~/.gittensor/contract_config.json (written by dev-environment up.sh)
    3. Empty dict (defaults will be used)

    Returns:
        Dict with contract_address, ws_endpoint, netuid, network keys
    """
    config: Dict[str, Any] = {}

    # 1. Check environment variable first
    env_addr = os.environ.get('CONTRACT_ADDRESS')
    if env_addr:
        config['contract_address'] = env_addr

    env_ws = os.environ.get('WS_ENDPOINT')
    if env_ws:
        config['ws_endpoint'] = env_ws

    # 2. Load from config file (fills in missing values)
    if CONTRACT_CONFIG_FILE.exists():
        try:
            with open(CONTRACT_CONFIG_FILE, 'r') as f:
                file_config = json.load(f)
                # Only use file values if not already set from env
                for key in ['contract_address', 'ws_endpoint', 'netuid', 'network']:
                    if key not in config and key in file_config:
                        config[key] = file_config[key]
        except (json.JSONDecodeError, IOError):
            pass

    return config


def get_contract_address(cli_value: str = '', testnet: bool = False) -> str:
    """
    Get contract address from CLI arg, env, or config file.

    Args:
        cli_value: Value passed via --contract CLI option
        testnet: If True and no address found, use testnet default

    Returns:
        Contract address string (may be empty if not configured)
    """
    if cli_value:
        return cli_value

    config = load_contract_config()
    if config.get('contract_address'):
        return config['contract_address']

    # Fall back to testnet default if requested
    if testnet:
        # TODO: Add testnet contract address constant
        return ''

    return ''


def get_ws_endpoint(cli_value: str = '') -> str:
    """
    Get WebSocket endpoint from CLI arg, env, or config file.

    Args:
        cli_value: Value passed via --rpc-url CLI option

    Returns:
        WebSocket endpoint string
    """
    if cli_value and cli_value != 'wss://entrypoint-finney.opentensor.ai:443':
        return cli_value

    config = load_contract_config()
    if config.get('ws_endpoint'):
        return config['ws_endpoint']

    return cli_value  # Return CLI default


def get_api_url(cli_value: str = '') -> str:
    """
    Get API URL from CLI arg, env, or config file.

    Priority:
    1. CLI argument (if not default)
    2. GITTENSOR_API_URL environment variable
    3. Config file (~/.gittensor/contract_config.json)
    4. Default (localhost:3000)

    Args:
        cli_value: Value passed via --api-url CLI option

    Returns:
        API URL string
    """
    # 1. CLI argument (if explicitly provided and not default)
    if cli_value and cli_value != DEFAULT_API_URL:
        return cli_value

    # 2. Environment variable
    env_url = os.environ.get('GITTENSOR_API_URL')
    if env_url:
        return env_url

    # 3. Config file
    config = load_contract_config()
    if config.get('api_url'):
        return config['api_url']

    # 4. Default
    return DEFAULT_API_URL


def load_preferences() -> List[int]:
    """Load current issue preferences from local file."""
    prefs_file = get_preferences_file()
    if not prefs_file.exists():
        return []
    try:
        with open(prefs_file, 'r') as f:
            data = json.load(f)
            return data.get('preferences', [])[:5]  # Max 5
    except (json.JSONDecodeError, IOError):
        return []


def save_preferences(preferences: List[int]) -> bool:
    """Save issue preferences to local file."""
    prefs_file = get_preferences_file()
    try:
        with open(prefs_file, 'w') as f:
            json.dump({'preferences': preferences[:5]}, f, indent=2)
        return True
    except IOError as e:
        console.print(f'[red]Failed to save preferences: {e}[/red]')
        return False


def clear_preferences() -> bool:
    """Clear issue preferences by deleting the file."""
    prefs_file = get_preferences_file()
    if prefs_file.exists():
        try:
            prefs_file.unlink()
            return True
        except IOError as e:
            console.print(f'[red]Failed to clear preferences: {e}[/red]')
            return False
    return True


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
    Read the packed root storage from a contract using childstate RPC.

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
    # validator_hotkey: AccountId (32 bytes)
    # netuid: u16 (2 bytes)
    # next_issue_id: u64 (8 bytes)
    # next_competition_id: u64 (8 bytes)
    # alpha_pool: u128 (16 bytes)
    # submission_window_blocks: u32 (4 bytes)
    # competition_deadline_blocks: u32 (4 bytes)
    # proposal_expiry_blocks: u32 (4 bytes)
    # Total: 142 bytes minimum

    if len(data) < 142:  # Minimum expected size
        if verbose:
            console.print(f'[dim]Debug: Packed storage too small ({len(data)} < 142 bytes)[/dim]')
        return None

    offset = 0
    owner = data[offset:offset + 32]
    offset += 32
    treasury = data[offset:offset + 32]
    offset += 32
    validator_hotkey = data[offset:offset + 32]
    offset += 32
    netuid = struct.unpack_from('<H', data, offset)[0]
    offset += 2
    next_issue_id = struct.unpack_from('<Q', data, offset)[0]
    offset += 8
    next_competition_id = struct.unpack_from('<Q', data, offset)[0]
    offset += 8
    alpha_pool_lo, alpha_pool_hi = struct.unpack_from('<QQ', data, offset)
    alpha_pool = alpha_pool_lo + (alpha_pool_hi << 64)
    offset += 16
    submission_window_blocks = struct.unpack_from('<I', data, offset)[0]
    offset += 4
    competition_deadline_blocks = struct.unpack_from('<I', data, offset)[0]
    offset += 4
    proposal_expiry_blocks = struct.unpack_from('<I', data, offset)[0]

    return {
        'owner': substrate.ss58_encode(owner.hex()),
        'treasury_hotkey': substrate.ss58_encode(treasury.hex()),
        'validator_hotkey': substrate.ss58_encode(validator_hotkey.hex()),
        'netuid': netuid,
        'next_issue_id': next_issue_id,
        'next_competition_id': next_competition_id,
        'alpha_pool': alpha_pool,
        'submission_window_blocks': submission_window_blocks,
        'competition_deadline_blocks': competition_deadline_blocks,
        'proposal_expiry_blocks': proposal_expiry_blocks,
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
    # A corrupted value indicates storage decoder mismatch
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
    status_names = ['Registered', 'Active', 'InCompetition', 'Completed', 'Cancelled']

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

            repo_name = data[offset:offset + str_len].decode('utf-8', errors='replace')
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

            issues.append({
                'id': stored_issue_id,
                'repository_full_name': repo_name,
                'issue_number': issue_number,
                'bounty_amount': bounty_amount,
                'target_bounty': target_bounty,
                'status': status,
            })
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
