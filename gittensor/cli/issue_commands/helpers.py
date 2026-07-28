# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Issue-specific helpers: bounty validation, issue contract-storage reads,
and PR-submission rendering. Generic CLI infra lives in gittensor.cli.core."""

import os
import struct
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import click

from gittensor.cli.core.helpers import (
    ALPHA_DECIMALS,
    ALPHA_RAW_UNIT,
    console,
    err_console,
    loading_context,
    print_success,
    print_warning,
    resolve_wallet_config,
)
from gittensor.cli.issue_commands.tables import build_pr_table
from gittensor.constants import MAX_ISSUE_ID
from gittensor.validator.issue_competitions.storage_utils import (
    ISSUES_MAPPING_ROOT_KEY,
    compute_ink5_lazy_key,
    decode_issue_from_storage,
    decode_packed_contract_storage,
    get_contract_child_storage_key,
    read_contract_packed_storage_bytes,
)

MIN_BOUNTY_ALPHA = 10
MAX_BOUNTY_ALPHA = 100_000_000
MAX_ISSUE_NUMBER = 2**32 - 1

# Status display colors
STATUS_COLORS: Dict[str, str] = {
    'Active': 'green',
    'Registered': 'yellow',
    'Completed': 'dim',
    'Cancelled': 'dim',
}


def colorize_status(status: str) -> str:
    """Wrap status text with the appropriate Rich color tag."""
    color = STATUS_COLORS.get(status, 'white')
    return f'[{color}]{status}[/{color}]'


def fetch_open_issue_pull_requests(
    repository_full_name: str,
    issue_number: int,
    as_json: bool,
) -> Optional[list]:
    """Fetch open PR submissions for a GitHub issue.

    Returns a (possibly empty) list of PRs, or ``None`` when the GitHub lookup
    fails. Callers must treat ``None`` as a failure (not "no submissions"); see
    ``find_prs_for_issue``.
    """
    token = os.environ.get('GITTENSOR_MINER_PAT') or ''
    if not token and not as_json:
        print_warning('No GitHub token found; set GITTENSOR_MINER_PAT to fetch GitHub issue submissions')

    try:
        from gittensor.utils.github_api_tools import find_prs_for_issue

        with loading_context('Fetching open pull request submissions from GitHub...', as_json):
            prs = find_prs_for_issue(
                repository_full_name,
                issue_number,
                token=token or None,
                open_only=True,
            )
            # Intentionally return GitHub tool output as-is (no CLI schema mapping yet);
            # this includes the None failure sentinel, which the caller must handle.
            return prs
    except Exception as e:
        raise click.ClickException(f'Failed to fetch PR submissions from GitHub: {e}')


def print_issue_submission_table(
    repository_full_name: str,
    issue_number: int,
    pull_requests: List[Dict[str, Any]],
    trailing_newline: bool = False,
) -> None:
    """Render the shared PR submissions success message and table."""
    issue_url = f'https://github.com/{repository_full_name}/issues/{issue_number}'
    print_success(f'{len(pull_requests)} open pull request submissions available. [blue]{issue_url}[/blue]')
    console.print(build_pr_table(pull_requests))
    suffix = '\n' if trailing_newline else ''
    console.print(f'Showing {len(pull_requests)} submissions{suffix}')


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

    _sign, _digits, exponent = d.as_tuple()
    decimal_places = max(0, -int(exponent))
    if decimal_places > ALPHA_DECIMALS:
        raise click.BadParameter(
            f'Maximum {ALPHA_DECIMALS} decimal places allowed (got {decimal_places})',
            param_hint='--bounty',
        )

    raw = int(d * ALPHA_RAW_UNIT)
    if raw <= 0:
        raise click.BadParameter('Bounty must result in a positive amount', param_hint='--bounty')

    return raw


# ============================================================================
# Contract storage reading helpers (shared by view and admin commands)
# ============================================================================


def _read_contract_packed_storage(substrate, contract_addr: str, verbose: bool = False) -> Optional[Dict[str, Any]]:
    """
    Read the packed root storage from a contract using childstate RPC.

    This bypasses the broken state_call/ContractsApi_call method and reads
    storage directly. Works around substrate-interface Ink! 5 compatibility issues.

    Returns ``None`` only when the contract genuinely has no readable packed
    storage at this address (no child key, no bytes, undersized payload).
    RPC / decode failures propagate so callers can distinguish a failed read
    from a clean "no storage yet" state.

    Args:
        substrate: SubstrateInterface instance
        contract_addr: Contract address
        verbose: If True, print debug output

    Returns:
        Dict with owner, netuid, next_issue_id, etc., or ``None`` when the
        contract has no packed storage at this address.
    """
    child_key = get_contract_child_storage_key(substrate, contract_addr)
    if not child_key:
        if verbose:
            err_console.print('[dim]Debug: Contract has no child storage key[/dim]')
        return None

    packed_bytes = read_contract_packed_storage_bytes(substrate, child_key)
    if not packed_bytes:
        if verbose:
            err_console.print('[dim]Debug: No packed storage bytes returned[/dim]')
        return None

    if verbose:
        err_console.print(f'[dim]Debug: Packed storage data length = {len(packed_bytes)} bytes[/dim]')

    packed = decode_packed_contract_storage(packed_bytes)
    if not packed:
        if verbose:
            err_console.print(f'[dim]Debug: Packed storage too small ({len(packed_bytes)} < 74 bytes)[/dim]')
        return None

    return {
        'owner': substrate.ss58_encode(packed.owner.hex()),
        'treasury_hotkey': substrate.ss58_encode(packed.treasury_hotkey.hex()),
        'netuid': packed.netuid,
        'next_issue_id': packed.next_issue_id,
        'alpha_pool': packed.alpha_pool,
    }


_ISSUE_STATUS_NAMES = ['Registered', 'Active', 'Completed', 'Cancelled']


def _read_one_issue_from_child_storage(
    substrate, child_key: str, issue_id: int, verbose: bool = False
) -> Optional[Dict[str, Any]]:
    """Read and decode a single issue from contract child storage by ID (one RPC).

    RPC failures propagate. Decode failures and a genuinely-absent storage entry
    return ``None``, matching the existing per-issue contract in the full scan.
    """
    encoded_id = struct.pack('<Q', issue_id)
    lazy_key = compute_ink5_lazy_key(ISSUES_MAPPING_ROOT_KEY, encoded_id)

    val_result = substrate.rpc_request('childstate_getStorage', [child_key, lazy_key, None])
    if not val_result.get('result'):
        if verbose:
            err_console.print(f'[dim]Debug: No storage found for issue_id={issue_id} (key={lazy_key[:20]}...)[/dim]')
        return None

    data = bytes.fromhex(val_result['result'].replace('0x', ''))
    try:
        decoded = decode_issue_from_storage(data)
        if decoded is None:
            raise ValueError('Issue decode returned no data')

        status = (
            _ISSUE_STATUS_NAMES[decoded.status_byte] if decoded.status_byte < len(_ISSUE_STATUS_NAMES) else 'Unknown'
        )
        issue = {
            'id': decoded.id,
            'repository_full_name': decoded.repository_full_name,
            'issue_number': decoded.issue_number,
            'bounty_amount': decoded.bounty_amount,
            'target_bounty': decoded.target_bounty,
            'status': status,
        }
        if verbose:
            err_console.print(
                f'[dim]Debug: Decoded issue {issue["id"]}: '
                f'{issue["repository_full_name"]}#{issue["issue_number"]}[/dim]'
            )
        return issue
    except Exception as e:
        if verbose:
            err_console.print(f'[dim]Debug: Failed to decode issue {issue_id}: {e}[/dim]')
        return None


def _read_issues_from_child_storage(substrate, contract_addr: str, verbose: bool = False) -> List[Dict[str, Any]]:
    """
    Read all issues from contract child storage.

    Uses Ink! 5 lazy mapping key computation to directly read issue storage.

    Returns ``[]`` only when the contract genuinely has no issues yet
    (no child storage key, missing packed storage, ``next_issue_id <= 1``).
    RPC / decode failures propagate so callers can distinguish a failed read
    from a real empty contract.

    Args:
        substrate: SubstrateInterface instance
        contract_addr: Contract address
        verbose: If True, print debug output

    Returns:
        List of issue dictionaries (empty for a real empty contract).
    """
    child_key = get_contract_child_storage_key(substrate, contract_addr)
    if not child_key:
        if verbose:
            err_console.print(f'[dim]Debug: Cannot read issues - no child storage key for {contract_addr}[/dim]')
        return []

    # First, read packed storage to get next_issue_id
    packed_storage = _read_contract_packed_storage(substrate, contract_addr, verbose)
    if not packed_storage:
        if verbose:
            err_console.print('[dim]Debug: Cannot read issues - packed storage read failed[/dim]')
        return []

    next_issue_id = packed_storage.get('next_issue_id', 1)
    if verbose:
        err_console.print(f'[dim]Debug: next_issue_id from contract = {next_issue_id}[/dim]')

    # Sanity check: next_issue_id should be reasonable (< 1 million for any real deployment)
    if next_issue_id > MAX_ISSUE_ID:
        err_console.print(f'[yellow]Warning: next_issue_id ({next_issue_id}) is unreasonably large.[/yellow]')
        err_console.print('[yellow]This may indicate a storage format mismatch. Check contract version.[/yellow]')
        return []

    # If next_issue_id is 1, no issues have been registered yet
    if next_issue_id <= 1:
        if verbose:
            err_console.print('[dim]Debug: No issues registered (next_issue_id <= 1)[/dim]')
        return []

    # Iterate through all issue IDs (1 to next_issue_id - 1)
    # Issues mapping root key is '52789899'
    if verbose:
        err_console.print(f'[dim]Debug: Reading issues 1 to {next_issue_id - 1} using mapping key 52789899[/dim]')

    issues = []
    for issue_id in range(1, next_issue_id):
        issue = _read_one_issue_from_child_storage(substrate, child_key, issue_id, verbose)
        if issue is not None:
            issues.append(issue)

    # Sort by ID
    issues.sort(key=lambda x: x['id'])
    return issues


def _make_contract_client(contract_addr: str, ws_endpoint: str, wallet_name: str, wallet_hotkey: str):
    """Instantiate a wallet and IssueCompetitionContractClient from CLI args.

    Returns (wallet, client). Lazy-imports bittensor and the contract client so
    that the top-level CLI remains importable without those heavy dependencies.
    """
    import bittensor as bt

    from gittensor.validator.issue_competitions.contract_client import (
        IssueCompetitionContractClient,
    )

    effective_wallet, effective_hotkey = resolve_wallet_config(
        wallet_name,
        wallet_hotkey,
        wallet_default='default',
        hotkey_default='default',
    )
    wallet = bt.Wallet(name=effective_wallet, hotkey=effective_hotkey)
    subtensor = bt.Subtensor(network=ws_endpoint)
    client = IssueCompetitionContractClient(
        contract_address=contract_addr,
        subtensor=subtensor,
    )
    return wallet, client


def read_issues_from_contract(ws_endpoint: str, contract_addr: str, verbose: bool = False) -> List[Dict[str, Any]]:
    """
    Read issues directly from the smart contract (no API dependency).

    Uses childstate_getStorage RPC to read contract storage directly,
    bypassing the broken ContractsApi_call method in substrate-interface.

    Raises on connection / RPC / decode failures so callers can distinguish a
    failed read from an empty contract. ``ImportError`` is also propagated;
    callers are expected to route both through their CLI error handler.

    Args:
        ws_endpoint: WebSocket endpoint for Subtensor
        contract_addr: Contract address
        verbose: If True, print debug output

    Returns:
        List of issue dictionaries
    """
    from async_substrate_interface import SubstrateInterface

    if verbose:
        err_console.print(f'[dim]Debug: Connecting to {ws_endpoint}...[/dim]')

    substrate = SubstrateInterface(url=ws_endpoint)

    if verbose:
        err_console.print('[dim]Debug: Connected successfully[/dim]')

    return _read_issues_from_child_storage(substrate, contract_addr, verbose)


def read_issue_from_contract(
    ws_endpoint: str,
    contract_addr: str,
    issue_id: int,
    verbose: bool = False,
) -> Optional[Dict[str, Any]]:
    """Read one issue from the contract by ID with a single childstate RPC (no full scan).

    Raises on connection / RPC failures so callers can distinguish a failed read
    from a missing issue — mirrors the ``read_issues_from_contract`` contract.
    Returns ``None`` only when the issue is genuinely absent from contract
    storage (or the contract has no child storage key yet).
    """
    from async_substrate_interface import SubstrateInterface

    if verbose:
        err_console.print(f'[dim]Debug: Connecting to {ws_endpoint}...[/dim]')

    substrate = SubstrateInterface(url=ws_endpoint)

    if verbose:
        err_console.print('[dim]Debug: Connected successfully[/dim]')

    child_key = get_contract_child_storage_key(substrate, contract_addr)
    if not child_key:
        if verbose:
            err_console.print(f'[dim]Debug: Cannot read issue - no child storage key for {contract_addr}[/dim]')
        return None
    return _read_one_issue_from_child_storage(substrate, child_key, issue_id, verbose)


def fetch_issue_from_contract(
    ws_endpoint: str,
    contract_addr: str,
    issue_id: int,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Resolve an on-chain issue and validate bountied status."""
    try:
        issue = read_issue_from_contract(ws_endpoint, contract_addr, issue_id, verbose)
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f'Error reading from contract: {e}')

    if not issue:
        raise click.ClickException(f'Issue ID {issue_id} not found on-chain.')

    status = issue.get('status') or ''
    status_normalized = str(status).strip().lower()
    if status_normalized not in {'registered', 'active'}:
        raise click.ClickException(f'Issue #{issue_id} is not in a bountied state (status: {status}).')

    repo = issue.get('repository_full_name', '')
    issue_number = issue.get('issue_number', 0)
    if not repo or not issue_number:
        raise click.ClickException('Issue missing repository or issue number.')

    return issue
