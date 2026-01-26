# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for managing issue competition preferences.

Miners use these commands to:
- View available issues with bounties
- Set their ranked preferences for competitions
- Check their current competition status
- View their ELO rating and history
"""

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Optional, Dict, Any

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# CLI config file location (same as config_commands.py)
CLI_CONFIG_FILE = Path.home() / '.gittensor' / 'cli_config.json'


def load_cli_config() -> dict:
    """Load CLI configuration from file."""
    if not CLI_CONFIG_FILE.exists():
        return {}
    try:
        with open(CLI_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

# Default paths and URLs
GITTENSOR_DIR = Path.home() / '.gittensor'
ISSUE_PREFERENCES_FILE = GITTENSOR_DIR / 'issue_preferences.json'
CONTRACT_CONFIG_FILE = GITTENSOR_DIR / 'contract_config.json'
DEFAULT_API_URL = 'http://localhost:3000'

console = Console()


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


@click.group()
def issue():
    """Issue competition commands for miners.

    Manage your participation in head-to-head coding competitions
    on GitHub issues. Winners receive ALPHA token bounties.
    """
    pass


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
    import struct

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
    # next_competition_id: u64 (8 bytes)
    # alpha_pool: u128 (16 bytes)
    # submission_window_blocks: u32 (4 bytes)
    # competition_deadline_blocks: u32 (4 bytes)
    # proposal_expiry_blocks: u32 (4 bytes)
    # Total: 110 bytes minimum

    if len(data) < 110:  # Minimum expected size
        if verbose:
            console.print(f'[dim]Debug: Packed storage too small ({len(data)} < 110 bytes)[/dim]')
        return None

    offset = 0
    owner = data[offset:offset + 32]
    offset += 32
    treasury = data[offset:offset + 32]
    offset += 32
    netuid = struct.unpack_from('<H', data, offset)[0]
    offset += 2
    next_issue_id = struct.unpack_from('<Q', data, offset)[0]
    offset += 8
    next_competition_id = struct.unpack_from('<Q', data, offset)[0]
    offset += 8
    alpha_pool_lo, alpha_pool_hi = struct.unpack_from('<QQ', data, offset)
    alpha_pool = alpha_pool_lo + (alpha_pool_hi << 64)

    return {
        'owner': substrate.ss58_encode(owner.hex()),
        'treasury_hotkey': substrate.ss58_encode(treasury.hex()),
        'netuid': netuid,
        'next_issue_id': next_issue_id,
        'next_competition_id': next_competition_id,
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
    import hashlib

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
    import struct

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


@issue.command('list')
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses default if empty)',
)
@click.option('--testnet', is_flag=True, help='Use testnet contract address')
@click.option('--from-api', is_flag=True, help='Force reading from API instead of contract')
@click.option('--verbose', '-v', is_flag=True, help='Show debug output for contract reads')
def issue_list(rpc_url: str, contract: str, testnet: bool, from_api: bool, verbose: bool):
    """
    List available issues for competition.

    Shows all issues with status=Active that have funded bounties
    and are available for competition.

    By default, reads directly from the smart contract (no API dependency).
    Use --from-api to read from the API instead.

    \b
    Example:
        gitt issue list
        gitt issue list --testnet
        gitt issue list --from-api
    """
    console.print('\n[bold cyan]Available Issues for Competition[/bold cyan]\n')

    # Load configuration
    config = load_contract_config()
    contract_addr = get_contract_address(contract, testnet)
    ws_endpoint = get_ws_endpoint(rpc_url)

    issues = []

    # Default: read from contract directly (no API dependency)
    if not from_api and contract_addr:
        console.print(f'[dim]Data source: Contract at {contract_addr[:20]}...[/dim]')
        console.print(f'[dim]Endpoint: {ws_endpoint}[/dim]\n')

        issues = read_issues_from_contract(ws_endpoint, contract_addr, verbose)

        if not issues:
            console.print('[yellow]No issues found in contract or contract read failed.[/yellow]')
            if verbose:
                console.print('[dim]Debug: Contract read returned empty list[/dim]')
            console.print('[dim]Falling back to API...[/dim]\n')
            from_api = True

    # Fallback or explicit API mode
    if from_api or not contract_addr:
        api_url = config.get('api_url', 'http://localhost:3000')
        console.print(f'[dim]Data source: API at {api_url}[/dim]')
        if contract_addr:
            console.print(f'[dim]Contract: {contract_addr[:20]}... @ {ws_endpoint}[/dim]\n')

        issues_endpoint = f'{api_url}/issues'

        try:
            req = urllib.request.Request(issues_endpoint, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                # Handle both direct array and wrapped response
                if isinstance(data, list):
                    issues = data
                elif isinstance(data, dict) and 'issues' in data:
                    issues = data['issues']
                elif isinstance(data, dict) and 'data' in data:
                    issues = data['data']
        except urllib.error.URLError as e:
            console.print(f'[yellow]Could not reach API ({e.reason}).[/yellow]')
            console.print('[dim]Ensure API is running or use direct contract reads.[/dim]\n')
        except Exception as e:
            console.print(f'[yellow]Error fetching issues: {e}[/yellow]\n')

    table = Table(show_header=True, header_style='bold magenta')
    table.add_column('ID', style='cyan', justify='right')
    table.add_column('Repository', style='green')
    table.add_column('Issue #', style='yellow', justify='right')
    table.add_column('Bounty Pool', style='magenta', justify='right')
    table.add_column('Status', style='blue')

    if issues:
        for issue in issues:
            # Handle different field naming conventions (camelCase from API, snake_case from contract)
            issue_id = issue.get('id', issue.get('issue_id', '?'))
            repo = issue.get('repositoryFullName', issue.get('repository_full_name', issue.get('repo', '?')))
            num = issue.get('issueNumber', issue.get('issue_number', issue.get('number', '?')))
            # Get bounty - prefer bounty_amount if funded, otherwise show target_bounty
            bounty_raw = issue.get('bountyAmount', issue.get('bounty_amount', 0))
            target_raw = issue.get('targetBounty', issue.get('target_bounty', 0))
            status = issue.get('status', 'unknown')

            # Parse bounty - might be string with decimals or numeric
            try:
                bounty = float(bounty_raw) if bounty_raw else 0.0
                target = float(target_raw) if target_raw else 0.0
                # Always convert from smallest units (9 decimals) to ALPHA
                bounty = bounty / 1_000_000_000
                target = target / 1_000_000_000
            except (ValueError, TypeError):
                bounty = 0.0
                target = 0.0

            # Format bounty pool display with fill percentage
            if target > 0:
                fill_pct = (bounty / target) * 100 if target > 0 else 0
                if fill_pct >= 100:
                    bounty_display = f'{bounty:.1f} (100%)'
                elif bounty > 0:
                    bounty_display = f'{bounty:.1f}/{target:.1f} ({fill_pct:.0f}%)'
                else:
                    bounty_display = f'0/{target:.1f} (0%)'
            else:
                bounty_display = f'{bounty:.2f}' if bounty > 0 else '0.00'

            # Format status (handle enum values)
            if isinstance(status, dict):
                status = list(status.keys())[0] if status else 'Unknown'
            elif isinstance(status, str):
                status = status.capitalize()
            else:
                status = str(status)

            table.add_row(
                str(issue_id),
                repo,
                f'#{num}',
                bounty_display,
                status,
            )
        console.print(table)
        console.print(f'\n[dim]Showing {len(issues)} issue(s)[/dim]')
        console.print('[dim]Bounty Pool shows: filled/target (percentage)[/dim]')
    else:
        console.print('[yellow]No issues found. Register an issue with:[/yellow]')
        console.print('[dim]  gitt issue register --repo owner/repo --issue 1 --bounty 100[/dim]')

    console.print('\n[dim]Use "gitt issue prefer <id1> <id2> ..." to set preferences[/dim]')


@issue.command('prefer')
@click.argument('issue_ids', nargs=-1, type=int)
@click.option('--clear', is_flag=True, help='Clear existing preferences before adding')
def issue_prefer(issue_ids: tuple, clear: bool):
    """
    Set ranked issue preferences (most preferred first).

    Your preferences determine which issues you'll be matched on.
    Higher ELO miners get priority for their preferred issues.

    \b
    Arguments:
        ISSUE_IDS: Space-separated list of issue IDs in preference order

    \b
    Examples:
        gitt issue prefer 42 15 8
        gitt issue prefer 1 2 3 --clear
    """
    if clear:
        clear_preferences()

    if not issue_ids:
        current = load_preferences()
        if current:
            console.print(f'[cyan]Current preferences:[/cyan] {current}')
        else:
            console.print('[yellow]No preferences set. Provide issue IDs to set preferences.[/yellow]')
        console.print('\n[dim]Usage: gitt issue prefer <id1> <id2> ...[/dim]')
        return

    preferences = list(issue_ids)[:5]  # Max 5

    # Display preferences
    console.print('\n[bold]Your Ranked Preferences:[/bold]')
    for i, issue_id in enumerate(preferences, 1):
        console.print(f'  {i}. Issue #{issue_id}')

    if len(issue_ids) > 5:
        console.print(f'\n[yellow]Note: Only first 5 preferences saved (you provided {len(issue_ids)})[/yellow]')

    # Confirm and save
    if click.confirm('\nSave these preferences?', default=True):
        if save_preferences(preferences):
            console.print(f'[green]Preferences saved to {ISSUE_PREFERENCES_FILE}[/green]')
            console.print('[dim]Your miner will automatically serve these to validators.[/dim]')
            console.print('[dim]You will be assigned based on ELO priority when pairs are formed.[/dim]')
        else:
            console.print('[red]Failed to save preferences.[/red]')


@issue.command('enroll')
@click.argument('issue_id', type=int)
def issue_enroll(issue_id: int):
    """
    Quick enroll for a single issue (shorthand for prefer).

    This is equivalent to running "prefer" with a single issue ID.

    \b
    Arguments:
        ISSUE_ID: The issue ID to enroll for

    \b
    Example:
        gitt issue enroll 42
    """
    current = load_preferences()
    if issue_id in current:
        console.print(f'[yellow]Already enrolled for issue #{issue_id}[/yellow]')
        console.print(f'Current preferences: {current}')
        return

    # Add to front of preferences
    new_prefs = [issue_id] + [p for p in current if p != issue_id][:4]

    if save_preferences(new_prefs):
        console.print(f'[green]Enrolled for issue #{issue_id}[/green]')
        console.print(f'New preferences: {new_prefs}')
    else:
        console.print('[red]Failed to enroll.[/red]')


@issue.command('status')
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL',
)
def issue_status(wallet_name: str, wallet_hotkey: str, api_url: str):
    """
    View your current competition status.

    Shows:
    - Local preferences (what you've enrolled for)
    - Active competition (if you're currently competing)
    - Competition details (opponent, deadline, bounty)

    \b
    Example:
        gitt issue status
        gitt issue status --wallet-name mywallet
    """
    console.print('\n[bold cyan]Issue Competition Status[/bold cyan]\n')

    # Show local preferences
    preferences = load_preferences()
    if preferences:
        console.print(Panel(
            f'[cyan]Preferred Issues:[/cyan] {preferences}\n'
            '[dim]Status: Waiting for validator pairing...[/dim]',
            title='Local Preferences',
            border_style='blue',
        ))
    else:
        console.print(Panel(
            '[yellow]No preferences set.[/yellow]\n'
            '[dim]Use "gitt issue prefer" to join competitions.[/dim]',
            title='Local Preferences',
            border_style='yellow',
        ))

    # Query API for active competition status
    console.print('\n[dim]Checking for active competitions...[/dim]')

    resolved_api_url = get_api_url(api_url)
    try:
        req = urllib.request.Request(f'{resolved_api_url}/competitions/active')
        with urllib.request.urlopen(req, timeout=5) as resp:
            competitions = json.loads(resp.read().decode())
            if competitions:
                for comp in competitions[:3]:  # Show up to 3 active competitions
                    comp_panel = Panel(
                        f'[green]Competition ID:[/green] {comp.get("id", "?")}\n'
                        f'[green]Issue:[/green] {comp.get("repository_full_name", "?")}#{comp.get("issue_number", "?")}\n'
                        f'[green]Bounty:[/green] {comp.get("bounty_amount", 0) / 1e9:.2f} ALPHA\n'
                        f'[green]Miner 1:[/green] {comp.get("miner1_hotkey", "?")[:12]}...\n'
                        f'[green]Miner 2:[/green] {comp.get("miner2_hotkey", "?")[:12]}...\n'
                        f'[green]Status:[/green] {comp.get("status", "Unknown")}',
                        title='Active Competition',
                        border_style='green',
                    )
                    console.print(comp_panel)
            else:
                console.print('[dim]No active competitions found.[/dim]')
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        console.print(f'[red]Error: Cannot connect to API at {resolved_api_url}[/red]')
        console.print('[dim]Ensure the API is running: cd das-gittensor && npm run start:dev[/dim]')


@issue.command('withdraw')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation prompt')
def issue_withdraw(force: bool):
    """
    Clear issue preferences (stop competing for new issues).

    This removes your local preferences file. You will no longer
    be matched for new competitions.

    NOTE: You cannot withdraw from an active competition once started.

    \b
    Example:
        gitt issue withdraw
        gitt issue withdraw --force
    """
    preferences = load_preferences()

    if not preferences:
        console.print('[yellow]No preferences to clear.[/yellow]')
        return

    console.print(f'[cyan]Current preferences:[/cyan] {preferences}')

    if force or click.confirm('\nClear all issue preferences?', default=False):
        if clear_preferences():
            console.print('[green]Preferences cleared.[/green]')
            console.print('[dim]You will not be matched for new competitions.[/dim]')
        else:
            console.print('[red]Failed to clear preferences.[/red]')


@issue.command('elo')
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL for ELO lookup',
)
def issue_elo(wallet_name: str, wallet_hotkey: str, api_url: str):
    """
    View your ELO rating and competition history.

    Shows your current ELO rating, win/loss record, and eligibility
    status for competitions.

    ELO System:
    - Initial rating: 800
    - Cutoff for eligibility: 700
    - Uses 30-day rolling EMA

    \b
    Example:
        gitt issue elo
        gitt issue elo --wallet-name mywallet
    """
    console.print('\n[bold cyan]ELO Rating[/bold cyan]\n')

    # Resolve API URL from CLI, env, or config
    resolved_api_url = get_api_url(api_url)

    # Get hotkey address for lookup
    try:
        import bittensor as bt
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        hotkey_address = wallet.hotkey.ss58_address
    except Exception:
        console.print('[red]Error: Cannot load wallet. Check wallet name and hotkey.[/red]')
        return

    # Query API for actual ELO rating
    try:
        req = urllib.request.Request(f'{resolved_api_url}/elo/{hotkey_address}')
        with urllib.request.urlopen(req, timeout=5) as resp:
            elo_data = json.loads(resp.read().decode())
            wins = elo_data.get('wins', 0)
            losses = elo_data.get('losses', 0)
            total = wins + losses
            win_rate = (wins / total * 100) if total > 0 else 0
            elo_score = elo_data.get('elo', 800)
            is_eligible = elo_score >= 700

            elo_panel = Panel(
                f'[bold green]Current ELO:[/bold green] {elo_score}\n'
                f'[green]Wins:[/green] {wins}\n'
                f'[green]Losses:[/green] {losses}\n'
                f'[green]Win Rate:[/green] {win_rate:.1f}%\n'
                f'[green]Eligible:[/green] {"Yes" if is_eligible else "No"} (ELO >= 700)',
                title='Your ELO Rating',
                border_style='green',
            )
            console.print(elo_panel)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            console.print('[yellow]No ELO record found. You have not participated in any competitions yet.[/yellow]')
            console.print('[dim]Your initial ELO will be 800 when you join your first competition.[/dim]')
        else:
            console.print(f'[red]Error: API returned status {e.code}[/red]')
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        console.print(f'[red]Error: Cannot connect to API at {resolved_api_url}[/red]')
        console.print('[dim]Ensure the API is running: cd das-gittensor && npm run start:dev[/dim]')
        return

    # ELO explanation
    console.print('\n[bold]ELO System Info:[/bold]')
    console.print('  - Initial rating: 800')
    console.print('  - Eligibility cutoff: 700 (3-4 consecutive losses)')
    console.print('  - K-factor: 40 (rating changes per match)')
    console.print('  - 30-day rolling EMA (older matches weighted less)')
    console.print('  - Inactivity: ELO decays toward 800 over 30 days')


@issue.command('competitions')
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL',
)
@click.option('--limit', default=10, help='Maximum competitions to show')
def issue_competitions(api_url: str, limit: int):
    """
    View all active competitions.

    Shows current head-to-head competitions across the network.

    \b
    Example:
        gitt issue competitions
        gitt issue competitions --limit 20
    """
    console.print('\n[bold cyan]Active Competitions[/bold cyan]\n')

    # Resolve API URL from CLI, env, or config
    resolved_api_url = get_api_url(api_url)

    # Query API for actual competitions
    try:
        req = urllib.request.Request(f'{resolved_api_url}/competitions?limit={limit}')
        with urllib.request.urlopen(req, timeout=5) as resp:
            competitions = json.loads(resp.read().decode())

            if not competitions:
                console.print('[dim]No active competitions found.[/dim]')
                return

            table = Table(show_header=True, header_style='bold magenta')
            table.add_column('ID', style='cyan', justify='right')
            table.add_column('Issue', style='green')
            table.add_column('Miner 1', style='yellow')
            table.add_column('Miner 2', style='yellow')
            table.add_column('Bounty', style='magenta', justify='right')
            table.add_column('Status', style='blue')

            for comp in competitions:
                comp_id = str(comp.get('id', '?'))
                repo = comp.get('repository_full_name', '?')
                issue_num = comp.get('issue_number', '?')
                issue_ref = f'{repo.split("/")[-1] if "/" in repo else repo}#{issue_num}'
                m1 = comp.get('miner1_hotkey', '?')[:12] + '...'
                m2 = comp.get('miner2_hotkey', '?')[:12] + '...'
                bounty = f'{comp.get("bounty_amount", 0) / 1e9:.1f}'
                status = comp.get('status', 'Unknown')
                table.add_row(comp_id, issue_ref, m1, m2, bounty, status)

            console.print(table)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        console.print(f'[red]Error: Cannot connect to API at {resolved_api_url}[/red]')
        console.print('[dim]Ensure the API is running: cd das-gittensor && npm run start:dev[/dim]')


@issue.command('leaderboard')
@click.option(
    '--api-url',
    default=DEFAULT_API_URL,
    help='Gittensor API URL',
)
@click.option('--limit', default=10, help='Number of miners to show')
def issue_leaderboard(api_url: str, limit: int):
    """
    View the ELO leaderboard.

    Shows top miners by ELO rating.

    \b
    Example:
        gitt issue leaderboard
        gitt issue leaderboard --limit 25
    """
    console.print('\n[bold cyan]ELO Leaderboard[/bold cyan]\n')

    # Resolve API URL from CLI, env, or config
    resolved_api_url = get_api_url(api_url)

    # Query API for actual leaderboard
    try:
        req = urllib.request.Request(f'{resolved_api_url}/elo/leaderboard?limit={limit}')
        with urllib.request.urlopen(req, timeout=5) as resp:
            leaderboard = json.loads(resp.read().decode())

            if not leaderboard:
                console.print('[dim]No ELO data found. No competitions have been completed yet.[/dim]')
                return

            table = Table(show_header=True, header_style='bold magenta')
            table.add_column('Rank', style='cyan', justify='right')
            table.add_column('Miner', style='green')
            table.add_column('ELO', style='yellow', justify='right')
            table.add_column('W/L', style='magenta', justify='center')
            table.add_column('Win %', style='blue', justify='right')
            table.add_column('Eligible', style='green', justify='center')

            for i, entry in enumerate(leaderboard, 1):
                hotkey = entry.get('hotkey', '?')
                miner_display = hotkey[:12] + '...' if len(hotkey) > 12 else hotkey
                elo = entry.get('elo', 800)
                wins = entry.get('wins', 0)
                losses = entry.get('losses', 0)
                wl = f'{wins}/{losses}'
                total = wins + losses
                win_pct = f'{(wins / total * 100):.0f}%' if total > 0 else 'N/A'
                eligible = 'Yes' if elo >= 700 else 'No'
                table.add_row(str(i), miner_display, str(elo), wl, win_pct, eligible)

            console.print(table)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        console.print(f'[red]Error: Cannot connect to API at {resolved_api_url}[/red]')
        console.print('[dim]Ensure the API is running: cd das-gittensor && npm run start:dev[/dim]')


@issue.command('register')
@click.option(
    '--repo',
    required=True,
    help='Repository in owner/repo format (e.g., opentensor/btcli)',
)
@click.option(
    '--issue',
    'issue_number',
    required=True,
    type=int,
    help='GitHub issue number',
)
@click.option(
    '--bounty',
    required=True,
    type=float,
    help='Bounty amount in ALPHA tokens',
)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses default if empty)',
)
@click.option('--testnet', is_flag=True, help='Use testnet contract address')
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name (must be contract owner)',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
def issue_register(
    repo: str,
    issue_number: int,
    bounty: float,
    rpc_url: str,
    contract: str,
    testnet: bool,
    wallet_name: str,
    wallet_hotkey: str,
):
    """
    Register a new issue with a bounty (OWNER ONLY).

    This command registers a GitHub issue on the smart contract
    with a target bounty amount. Only the contract owner can
    register new issues.

    \b
    Arguments:
        --repo: Repository in owner/repo format
        --issue: GitHub issue number
        --bounty: Target bounty amount in ALPHA

    \b
    Examples:
        gitt issue register --repo opentensor/btcli --issue 144 --bounty 100
        gitt issue register --repo tensorflow/tensorflow --issue 12345 --bounty 50 --testnet
    """
    console.print('\n[bold cyan]Register Issue for Competition[/bold cyan]\n')

    # Validate repo format
    if '/' not in repo:
        console.print('[red]Error: Repository must be in owner/repo format[/red]')
        return

    # Construct GitHub URL
    github_url = f'https://github.com/{repo}/issues/{issue_number}'

    # Display registration details
    # Get contract address and endpoint from env/config if not provided
    contract_addr = get_contract_address(contract, testnet)
    ws_endpoint = get_ws_endpoint(rpc_url)

    # Determine network name from config
    config = load_contract_config()
    network_name = config.get('network', 'mainnet').capitalize()
    if testnet:
        network_name = 'Testnet'

    console.print(Panel(
        f'[cyan]Repository:[/cyan] {repo}\n'
        f'[cyan]Issue Number:[/cyan] #{issue_number}\n'
        f'[cyan]GitHub URL:[/cyan] {github_url}\n'
        f'[cyan]Target Bounty:[/cyan] {bounty:.2f} ALPHA\n'
        f'[cyan]Network:[/cyan] {network_name}\n'
        f'[cyan]WS Endpoint:[/cyan] {ws_endpoint}\n'
        f'[cyan]Contract:[/cyan] {contract_addr if contract_addr else "(not configured)"}',
        title='Issue Registration',
        border_style='blue',
    ))

    if not contract_addr:
        console.print('\n[red]Error: Contract address not configured.[/red]')
        console.print('[dim]Run ./up.sh --issues to deploy the contract first.[/dim]')
        return

    if not click.confirm('\nProceed with registration?', default=True):
        console.print('[yellow]Registration cancelled.[/yellow]')
        return

    # Perform actual contract call (on-chain transaction)
    console.print('\n[yellow]Submitting on-chain transaction to contract...[/yellow]')

    try:
        from substrateinterface import SubstrateInterface, Keypair
        from substrateinterface.contracts import ContractInstance
        import bittensor as bt

        # Connect to subtensor
        console.print(f'[dim]Connecting to {ws_endpoint}...[/dim]')
        substrate = SubstrateInterface(url=ws_endpoint)

        # Load wallet from CLI config or CLI args
        cli_config = load_cli_config()
        effective_wallet = cli_config.get('wallet', wallet_name)
        effective_hotkey = cli_config.get('hotkey', wallet_hotkey)

        # For local development, check config first, then fall back to //Alice
        if network_name.lower() == 'local' and effective_wallet == 'default' and effective_hotkey == 'default':
            console.print('[dim]Using //Alice for local development (no config set)...[/dim]')
            keypair = Keypair.create_from_uri('//Alice')
        else:
            # Load wallet from config or CLI args
            console.print(f'[dim]Loading wallet {effective_wallet}/{effective_hotkey}...[/dim]')
            wallet = bt.Wallet(name=effective_wallet, hotkey=effective_hotkey)
            # Use COLDKEY for owner-only operations (register_issue requires owner)
            # Contract owner is set to deployer's coldkey during contract instantiation
            keypair = wallet.coldkey

        # Load contract
        contract_metadata = Path(__file__).parent.parent.parent / 'smart-contracts' / 'ink' / 'target' / 'ink' / 'issue_bounty_manager.contract'
        if not contract_metadata.exists():
            console.print(f'[red]Error: Contract metadata not found at {contract_metadata}[/red]')
            return

        contract = ContractInstance.create_from_address(
            contract_address=contract_addr,
            metadata_file=str(contract_metadata),
            substrate=substrate,
        )

        # Convert bounty to contract units (9 decimals for ALPHA)
        bounty_amount = int(bounty * 1_000_000_000)

        console.print('[yellow]Calling register_issue on contract...[/yellow]')

        result = contract.exec(
            keypair,
            'register_issue',
            args={
                'github_url': github_url,
                'repository_full_name': repo,
                'issue_number': issue_number,
                'target_bounty': bounty_amount,
            },
            gas_limit={'ref_time': 10_000_000_000, 'proof_size': 1_000_000},
        )

        # Check if transaction was successful
        if hasattr(result, 'is_success') and not result.is_success:
            console.print(f'\n[red]Transaction failed![/red]')
            if hasattr(result, 'error_message'):
                console.print(f'[red]Error: {result.error_message}[/red]')
            console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
            return

        console.print(f'\n[green]Issue registered successfully![/green]')
        console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
        console.print(f'[dim]Issue will be visible once bounty is funded via depositToPool()[/dim]')

    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
        console.print('[dim]Install with: pip install substrate-interface bittensor[/dim]')
    except Exception as e:
        console.print(f'[red]Error registering issue: {e}[/red]')


@issue.command('harvest')
@click.option(
    '--wallet-name',
    default='validator',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show detailed output')
def issue_harvest(wallet_name: str, wallet_hotkey: str, rpc_url: str, contract: str, verbose: bool):
    """
    Manually trigger emission harvest from contract treasury.

    This command is useful for debugging harvest failures. It will:
    - Show wallet balance (must have >1 TAO for fees)
    - Attempt to call harvest_emissions() on the contract
    - Display full error details if harvest fails

    The harvest operation is permissionless - any wallet can trigger it.
    The contract handles emission collection and distribution internally.

    \b
    Examples:
        gitt issue harvest
        gitt issue harvest --verbose
        gitt issue harvest --wallet-name mywallet --wallet-hotkey mykey
    """
    console.print('\n[bold cyan]Manual Emission Harvest[/bold cyan]\n')

    # Get configuration
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        console.print('[dim]Set CONTRACT_ADDRESS env var or run ./up.sh --issues[/dim]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Endpoint: {ws_endpoint}[/dim]')
    console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey}[/dim]\n')

    try:
        import bittensor as bt
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        # Load wallet
        console.print('[yellow]Loading wallet...[/yellow]')
        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        hotkey_addr = wallet.hotkey.ss58_address
        console.print(f'[green]Hotkey address:[/green] {hotkey_addr}')

        # Connect to subtensor
        console.print(f'\n[yellow]Connecting to subtensor...[/yellow]')
        subtensor = bt.Subtensor(network=ws_endpoint)

        # Show wallet balance (informational only - let contract client handle insufficient funds)
        if verbose:
            try:
                balance = subtensor.get_balance(hotkey_addr)
                console.print(f'[dim]Wallet balance: {balance}[/dim]')
            except Exception as e:
                console.print(f'[dim]Could not fetch balance: {e}[/dim]')

        # Create contract client
        console.print(f'\n[yellow]Initializing contract client...[/yellow]')
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        if verbose:
            # Show contract state
            console.print('[dim]Reading contract state...[/dim]')
            try:
                alpha_pool = client.get_alpha_pool()
                pending = client.get_pending_emissions()
                last_harvest = client.get_last_harvest_block()
                current_block = subtensor.get_current_block()

                console.print(f'[dim]Alpha pool: {alpha_pool / 1e9:.4f} ALPHA[/dim]')
                console.print(f'[dim]Pending emissions: {pending / 1e9:.4f} ALPHA[/dim]')
                console.print(f'[dim]Last harvest block: {last_harvest}[/dim]')
                console.print(f'[dim]Current block: {current_block}[/dim]')
                if last_harvest > 0:
                    console.print(f'[dim]Blocks since harvest: {current_block - last_harvest}[/dim]')
            except Exception as e:
                console.print(f'[yellow]Warning: Could not read contract state: {e}[/yellow]')

        # Attempt harvest
        console.print(f'\n[yellow]Calling harvest_emissions()...[/yellow]')
        result = client.harvest_emissions(wallet)

        if result:
            if result.get('status') == 'success':
                console.print(f'\n[green]Harvest succeeded![/green]')
                console.print(f'[cyan]Transaction hash:[/cyan] {result.get("tx_hash", "N/A")}')
                if result.get('recycled'):
                    console.print('[dim]Emissions recycled to staking pool.[/dim]')
            elif result.get('status') == 'failed':
                console.print(f'\n[red]Harvest failed![/red]')
                console.print(f'[red]Error: {result.get("error", "Unknown error")}[/red]')
            else:
                console.print(f'\n[yellow]Harvest result: {result}[/yellow]')
        else:
            console.print(f'\n[red]Harvest returned None - check logs for details.[/red]')
            console.print('[dim]Run with --verbose for more information.[/dim]')

    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
        console.print('[dim]Install with: pip install bittensor substrate-interface[/dim]')
    except Exception as e:
        import traceback
        console.print(f'\n[red]Error during harvest: {type(e).__name__}: {e}[/red]')
        if verbose:
            console.print(f'[dim]Full traceback:\n{traceback.format_exc()}[/dim]')
        else:
            console.print('[dim]Run with --verbose for full traceback.[/dim]')


@issue.group(hidden=True)
def admin():
    """Admin/testing commands for direct contract interaction (development only).

    These commands provide direct access to contract methods for debugging
    and testing. They bypass the normal API layer and read directly from
    the smart contract.
    """
    pass


@admin.command('pool')
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def admin_pool(rpc_url: str, contract: str, verbose: bool):
    """View current alpha pool balance."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        packed = _read_contract_packed_storage(substrate, contract_addr, verbose)

        if packed:
            alpha_pool = packed.get('alpha_pool', 0)
            console.print(f'[green]Alpha Pool:[/green] {alpha_pool / 1e9:.4f} ALPHA ({alpha_pool} raw)')
        else:
            console.print('[yellow]Could not read contract storage.[/yellow]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('pending')
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def admin_pending(rpc_url: str, contract: str, verbose: bool):
    """View pending emissions value (current stake on treasury)."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        pending = client.get_pending_emissions()
        last_known = client.get_last_known_stake()
        delta = pending - last_known if pending > last_known else 0

        console.print(f'[green]Current Stake (Total):[/green] {pending / 1e9:.4f} ALPHA')
        console.print(f'[green]Last Known Stake:[/green] {last_known / 1e9:.4f} ALPHA')
        console.print(f'[green]Delta (New Emissions):[/green] {delta / 1e9:.4f} ALPHA')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('issue')
@click.argument('issue_id', type=int)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def admin_issue(issue_id: int, rpc_url: str, contract: str, verbose: bool):
    """View raw issue data from contract."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Reading issue {issue_id}...[/dim]\n')

    try:
        from substrateinterface import SubstrateInterface

        substrate = SubstrateInterface(url=ws_endpoint)
        issues = _read_issues_from_child_storage(substrate, contract_addr, verbose)

        issue = next((i for i in issues if i['id'] == issue_id), None)

        if issue:
            console.print(Panel(
                f'[cyan]ID:[/cyan] {issue["id"]}\n'
                f'[cyan]Repository:[/cyan] {issue["repository_full_name"]}\n'
                f'[cyan]Issue Number:[/cyan] #{issue["issue_number"]}\n'
                f'[cyan]Bounty Amount:[/cyan] {issue["bounty_amount"] / 1e9:.4f} ALPHA\n'
                f'[cyan]Target Bounty:[/cyan] {issue["target_bounty"] / 1e9:.4f} ALPHA\n'
                f'[cyan]Fill %:[/cyan] {(issue["bounty_amount"] / issue["target_bounty"] * 100) if issue["target_bounty"] > 0 else 0:.1f}%\n'
                f'[cyan]Status:[/cyan] {issue["status"]}',
                title=f'Issue #{issue_id}',
                border_style='green',
            ))
        else:
            console.print(f'[yellow]Issue {issue_id} not found.[/yellow]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('competition')
@click.argument('competition_id', type=int)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def admin_competition(competition_id: int, rpc_url: str, contract: str, verbose: bool):
    """View competition details from contract."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Reading competition {competition_id}...[/dim]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        comp = client.get_competition(competition_id)

        if comp:
            # Handle dataclass return type
            payout = comp.payout_amount if comp.payout_amount else 0
            winner = comp.winner_hotkey if comp.winner_hotkey else 'None'
            console.print(Panel(
                f'[cyan]ID:[/cyan] {comp.id}\n'
                f'[cyan]Issue ID:[/cyan] {comp.issue_id}\n'
                f'[cyan]Miner 1:[/cyan] {comp.miner1_hotkey}\n'
                f'[cyan]Miner 2:[/cyan] {comp.miner2_hotkey}\n'
                f'[cyan]Status:[/cyan] {comp.status.name}\n'
                f'[cyan]Start Block:[/cyan] {comp.start_block}\n'
                f'[cyan]Deadline Block:[/cyan] {comp.deadline_block}\n'
                f'[cyan]Winner:[/cyan] {winner}\n'
                f'[cyan]Payout Amount:[/cyan] {payout / 1e9:.4f} ALPHA',
                title=f'Competition #{competition_id}',
                border_style='green',
            ))
        else:
            console.print(f'[yellow]Competition {competition_id} not found.[/yellow]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('proposal')
@click.argument('issue_id', type=int)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def admin_proposal(issue_id: int, rpc_url: str, contract: str, verbose: bool):
    """View pair proposal state for an issue."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[dim]Reading proposal for issue {issue_id}...[/dim]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        proposal = client.get_pair_proposal(issue_id)

        if proposal:
            # Handle dataclass return type
            console.print(Panel(
                f'[cyan]Issue ID:[/cyan] {proposal.issue_id}\n'
                f'[cyan]Miner 1:[/cyan] {proposal.miner1_hotkey}\n'
                f'[cyan]Miner 2:[/cyan] {proposal.miner2_hotkey}\n'
                f'[cyan]Proposer:[/cyan] {proposal.proposer}\n'
                f'[cyan]Proposed At Block:[/cyan] {proposal.proposed_at_block}\n'
                f'[cyan]Total Stake Voted:[/cyan] {proposal.total_stake_voted / 1e9:.4f}',
                title=f'Pair Proposal for Issue #{issue_id}',
                border_style='yellow',
            ))
        else:
            console.print(f'[yellow]No active proposal for issue {issue_id}.[/yellow]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('propose')
@click.argument('issue_id', type=int)
@click.argument('miner1_hotkey', type=str)
@click.argument('miner2_hotkey', type=str)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
def admin_propose(
    issue_id: int,
    miner1_hotkey: str,
    miner2_hotkey: str,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Propose a miner pair for competition testing."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Proposing pair for issue {issue_id}...[/yellow]\n')
    console.print(f'  Miner 1: {miner1_hotkey}')
    console.print(f'  Miner 2: {miner2_hotkey}\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        result = client.propose_pair(issue_id, miner1_hotkey, miner2_hotkey, wallet)
        if result:
            console.print(f'[green]Proposal submitted![/green]')
        else:
            console.print('[red]Proposal failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('vote-pair')
@click.argument('issue_id', type=int)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
def admin_vote_pair(
    issue_id: int,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Vote on an existing pair proposal."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Voting on pair proposal for issue {issue_id}...[/yellow]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        result = client.vote_pair(issue_id, wallet)
        if result:
            console.print(f'[green]Vote submitted![/green]')
        else:
            console.print('[red]Vote failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('vote-solution')
@click.argument('competition_id', type=int)
@click.argument('winner_hotkey', type=str)
@click.argument('pr_url', type=str)
@click.option(
    '--wallet-name',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
def admin_vote_solution(
    competition_id: int,
    winner_hotkey: str,
    pr_url: str,
    wallet_name: str,
    wallet_hotkey: str,
    rpc_url: str,
    contract: str,
):
    """Vote for a solution winner in an active competition."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print(f'[yellow]Voting on solution for competition {competition_id}...[/yellow]\n')
    console.print(f'  Winner: {winner_hotkey}')
    console.print(f'  PR URL: {pr_url}\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        result = client.vote_solution(competition_id, winner_hotkey, pr_url, wallet)
        if result:
            console.print(f'[green]Solution vote submitted![/green]')
        else:
            console.print('[red]Vote failed.[/red]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


@admin.command('competitions')
@click.option(
    '--rpc-url',
    default='wss://entrypoint-finney.opentensor.ai:443',
    help='Subtensor RPC endpoint',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
def admin_competitions(rpc_url: str, contract: str, verbose: bool):
    """List all active competitions from contract."""
    contract_addr = get_contract_address(contract, testnet=False)
    ws_endpoint = get_ws_endpoint(rpc_url)

    if not contract_addr:
        console.print('[red]Error: Contract address not configured.[/red]')
        return

    console.print(f'[dim]Contract: {contract_addr}[/dim]')
    console.print('[dim]Reading active competitions...[/dim]\n')

    try:
        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )
        import bittensor as bt

        subtensor = bt.Subtensor(network=ws_endpoint)
        client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=subtensor,
        )

        competitions = client.get_active_competitions()

        if competitions:
            table = Table(show_header=True, header_style='bold magenta')
            table.add_column('ID', style='cyan', justify='right')
            table.add_column('Issue ID', style='green', justify='right')
            table.add_column('Miner 1', style='yellow')
            table.add_column('Miner 2', style='yellow')
            table.add_column('Status', style='blue')
            table.add_column('Deadline', style='magenta', justify='right')

            for comp in competitions:
                # Handle dataclass return type
                table.add_row(
                    str(comp.id),
                    str(comp.issue_id),
                    comp.miner1_hotkey[:12] + '...',
                    comp.miner2_hotkey[:12] + '...',
                    comp.status.name,
                    str(comp.deadline_block),
                )

            console.print(table)
            console.print(f'\n[dim]Found {len(competitions)} active competition(s)[/dim]')
        else:
            console.print('[dim]No active competitions found.[/dim]')
    except ImportError as e:
        console.print(f'[red]Error: Missing dependency - {e}[/red]')
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')


def register_issue_commands(cli):
    """Register issue commands with a parent CLI group."""
    cli.add_command(issue)
