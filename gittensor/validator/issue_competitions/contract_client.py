# The MIT License (MIT)
# Copyright 2025 Entrius

"""Client for interacting with the Issues Competition smart contract."""

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

import bittensor as bt

try:
    from substrateinterface import Keypair
    from substrateinterface.contracts import ContractInstance

    SUBSTRATE_INTERFACE_AVAILABLE = True
except ImportError:
    SUBSTRATE_INTERFACE_AVAILABLE = False
    ContractInstance = None
    Keypair = None

# Default gas limits for contract calls
DEFAULT_GAS_LIMIT = {
    'ref_time': 10_000_000_000,  # 10 billion
    'proof_size': 500_000,  # 500 KB
}

# Config file path for local dev environment
GITTENSOR_CONFIG_PATH = Path.home() / '.gittensor' / 'contract_config.json'

# Contract metadata filename
CONTRACT_METADATA_FILENAME = 'issue_bounty_manager.contract'


def _find_contract_metadata_path() -> Optional[Path]:
    """
    Find the contract metadata file path using multiple resolution strategies.

    Priority:
    1. CONTRACT_METADATA_PATH environment variable
    2. metadata_path from ~/.gittensor/contract_config.json
    3. Search common locations relative to gittensor package

    Returns:
        Path to contract metadata file if found, None otherwise
    """
    # 1. Environment variable (highest priority)
    env_path = os.environ.get('CONTRACT_METADATA_PATH')
    if env_path:
        path = Path(env_path)
        if path.exists():
            bt.logging.debug(f'Using contract metadata from env: {path}')
            return path
        bt.logging.warning(f'CONTRACT_METADATA_PATH set but file not found: {path}')

    # 2. Config file (written by dev-environment up.sh)
    if GITTENSOR_CONFIG_PATH.exists():
        try:
            config = json.loads(GITTENSOR_CONFIG_PATH.read_text())
            metadata_path = config.get('metadata_path')
            if metadata_path:
                path = Path(metadata_path)
                if path.exists():
                    bt.logging.debug(f'Using contract metadata from config: {path}')
                    return path
        except (json.JSONDecodeError, IOError):
            pass

    # 3. Search common locations relative to this package
    # Try to find gittensor package root by looking for known markers
    search_paths = []

    # From this file's location, try to find the smart-contracts folder
    # This file is at: gittensor/gittensor/validator/issue_competitions/contract_client.py
    # smart-contracts is at: gittensor/smart-contracts/
    current_file = Path(__file__).resolve()

    # Go up to gittensor/gittensor/, then up one more to gittensor/ (repo root)
    # contract_client.py -> issue_competitions -> validator -> gittensor -> gittensor (repo)
    repo_root_candidates = [
        current_file.parent.parent.parent.parent,  # 4 levels up from contract_client.py
    ]

    # Also check if we're installed as a package and have a data directory
    try:
        import gittensor
        if hasattr(gittensor, '__path__'):
            for gittensor_path in gittensor.__path__:
                # gittensor package is at gittensor/gittensor/, so repo is one level up
                repo_root_candidates.append(Path(gittensor_path).parent)
    except (ImportError, AttributeError):
        pass

    # Build search paths
    for repo_root in repo_root_candidates:
        search_paths.extend([
            repo_root / 'smart-contracts' / 'ink' / 'target' / 'ink' / CONTRACT_METADATA_FILENAME,
            repo_root / 'smart-contracts' / 'target' / 'ink' / CONTRACT_METADATA_FILENAME,
        ])

    # Try each path
    for path in search_paths:
        if path.exists():
            bt.logging.debug(f'Found contract metadata at: {path}')
            return path

    # Log searched paths for debugging
    bt.logging.debug(f'Contract metadata not found. Searched: {[str(p) for p in search_paths[:4]]}')
    return None


def get_contract_metadata_path() -> Optional[Path]:
    """
    Get the contract metadata file path.

    This function caches the result after first call.

    Returns:
        Path to contract metadata file if found, None otherwise
    """
    if not hasattr(get_contract_metadata_path, '_cached_path'):
        get_contract_metadata_path._cached_path = _find_contract_metadata_path()
    return get_contract_metadata_path._cached_path


# Default path - resolved lazily
def _get_default_metadata_path() -> Path:
    """Get default metadata path, with fallback to a placeholder."""
    path = get_contract_metadata_path()
    if path:
        return path
    # Return a placeholder that will fail gracefully
    return Path.home() / '.gittensor' / 'contracts' / CONTRACT_METADATA_FILENAME


def get_contract_address_from_config() -> Optional[str]:
    """
    Get contract address from environment variable or local config file.

    Priority:
    1. CONTRACT_ADDRESS environment variable
    2. ~/.gittensor/contract_config.json (written by dev-environment up.sh)
    3. Return None (caller should use constants as fallback)

    Returns:
        Contract address string or None
    """
    # 1. Environment variable (highest priority)
    env_addr = os.environ.get('CONTRACT_ADDRESS')
    if env_addr:
        bt.logging.debug(f'Using contract address from env: {env_addr[:20]}...')
        return env_addr

    # 2. Local config file (for dev environment)
    if GITTENSOR_CONFIG_PATH.exists():
        try:
            with open(GITTENSOR_CONFIG_PATH) as f:
                config = json.load(f)
                addr = config.get('contract_address')
                if addr:
                    bt.logging.debug(f'Using contract address from config: {addr[:20]}...')
                    return addr
        except (json.JSONDecodeError, IOError) as e:
            bt.logging.warning(f'Failed to read contract config: {e}')

    return None


class IssueStatus(Enum):
    """Status of an issue in its lifecycle."""

    REGISTERED = 0
    ACTIVE = 1
    IN_COMPETITION = 2
    COMPLETED = 3
    CANCELLED = 4


class CompetitionStatus(Enum):
    """Status of a competition."""

    ACTIVE = 0
    COMPLETED = 1
    TIMED_OUT = 2
    CANCELLED = 3


@dataclass
class ContractIssue:
    """Issue data from the smart contract."""

    id: int
    github_url_hash: bytes
    repository_full_name: str
    issue_number: int
    bounty_amount: int
    target_bounty: int
    status: IssueStatus
    registered_at_block: int
    is_fully_funded: bool


@dataclass
class ContractCompetition:
    """Competition data from the smart contract."""

    id: int
    issue_id: int
    miner1_hotkey: str
    miner2_hotkey: str
    start_block: int
    submission_window_end_block: int
    deadline_block: int
    status: CompetitionStatus
    winner_hotkey: Optional[str] = None
    winning_pr_url_hash: Optional[bytes] = None
    payout_amount: Optional[int] = None


@dataclass
class PairProposal:
    """A proposal to pair two miners for a competition."""

    issue_id: int
    miner1_hotkey: str
    miner2_hotkey: str
    proposer: str
    proposed_at_block: int
    total_stake_voted: int


class IssueCompetitionContractClient:
    """
    Client for interacting with the Issues Competition smart contract.

    This client handles all read/write operations with the on-chain contract
    for the issue competitions sub-mechanism.
    """

    def __init__(
        self,
        contract_address: Optional[str] = None,
        subtensor: Optional[bt.Subtensor] = None,
        metadata_path: Optional[Path] = None,
    ):
        """
        Initialize the contract client.

        Args:
            contract_address: Address of the deployed contract (optional, will check env/config)
            subtensor: Bittensor subtensor instance for chain interaction
            metadata_path: Path to the .contract metadata file (optional)
        """
        # Try to get contract address from various sources
        if contract_address:
            self.contract_address = contract_address
        else:
            # Check env var and config file
            config_addr = get_contract_address_from_config()
            if config_addr:
                self.contract_address = config_addr
            else:
                # Fall back to constants (may be empty for testnet/mainnet)
                try:
                    from gittensor.validator.issue_competitions.constants import (
                        ISSUE_CONTRACT_ADDRESS_TESTNET,
                    )
                    self.contract_address = ISSUE_CONTRACT_ADDRESS_TESTNET
                except ImportError:
                    self.contract_address = ''

        self.subtensor = subtensor
        self.metadata_path = metadata_path or _get_default_metadata_path()
        self._contract = None
        self._initialized = False

        bt.logging.debug(f'IssueCompetitionContractClient initialized:')
        bt.logging.debug(f'  contract_address: {self.contract_address or "NOT SET"}')
        bt.logging.debug(f'  metadata_path: {self.metadata_path}')
        bt.logging.debug(f'  metadata_exists: {self.metadata_path.exists() if self.metadata_path else False}')

        if not self.contract_address:
            bt.logging.warning('Issue competition contract address not set')

    def _ensure_contract(self) -> bool:
        """
        Ensure contract connection is established.

        Returns:
            True if contract is ready, False otherwise
        """
        if not self.contract_address:
            bt.logging.warning('Cannot connect: contract address not set')
            return False

        if not SUBSTRATE_INTERFACE_AVAILABLE:
            bt.logging.warning(
                'substrate-interface library not available. '
                'Install with: pip install substrate-interface'
            )
            return False

        # Initialize contract if not already done
        if self._contract is None and not self._initialized:
            self._initialized = True
            try:
                # Check if metadata file exists
                if not self.metadata_path.exists():
                    bt.logging.warning(
                        f'Contract metadata not found at {self.metadata_path}. '
                        'Contract may not be compiled yet.'
                    )
                    return False

                # Create contract instance from address and metadata
                self._contract = ContractInstance.create_from_address(
                    contract_address=self.contract_address,
                    metadata_file=str(self.metadata_path),
                    substrate=self.subtensor.substrate,
                )
                bt.logging.info(
                    f'Connected to contract at {self.contract_address}'
                )
            except Exception as e:
                bt.logging.error(f'Failed to initialize contract: {e}')
                self._contract = None
                return False

        return self._contract is not None

    def _raw_contract_read(self, method_name: str, args: dict = None) -> Optional[bytes]:
        """
        Read from contract using raw RPC call.

        This is a workaround for substrate-interface Ink! 5 incompatibility.
        Uses state_call to ContractsApi_call and extracts raw result bytes.

        Args:
            method_name: Contract method name (e.g., 'next_issue_id')
            args: Optional method arguments

        Returns:
            Raw return data bytes, or None on error
        """
        if not self._ensure_contract():
            return None

        try:
            # Get method selector from metadata
            metadata_path = self.metadata_path.with_suffix('.json')
            if not metadata_path.exists():
                metadata_path = self.metadata_path  # Try .contract file

            with open(metadata_path) as f:
                metadata = json.load(f)

            selector = None
            for msg in metadata.get('spec', {}).get('messages', []):
                if msg['label'] == method_name:
                    selector = bytes.fromhex(msg['selector'].replace('0x', ''))
                    break

            if not selector:
                bt.logging.error(f'Method {method_name} not found in contract metadata')
                return None

            # Build input data (selector + encoded args)
            # For now, we only support no-arg methods
            input_data = selector

            # Build ContractsApi_call params
            # origin (32 bytes) + dest (32 bytes) + value (16 bytes) + gas_limit (1 byte None) + storage_limit (1 byte None) + input_data (compact Vec)
            from substrateinterface import Keypair
            caller = Keypair.create_from_uri('//Alice')

            origin = bytes.fromhex(self.subtensor.substrate.ss58_decode(caller.ss58_address))
            dest = bytes.fromhex(self.subtensor.substrate.ss58_decode(self.contract_address))
            value = b'\x00' * 16  # 0 balance
            gas_limit = b'\x00'  # None
            storage_limit = b'\x00'  # None

            # Compact encode input_data length
            data_len = len(input_data)
            if data_len < 64:
                compact_len = bytes([data_len << 2])
            else:
                compact_len = bytes([(data_len << 2) | 1, data_len >> 6])

            call_params = origin + dest + value + gas_limit + storage_limit + compact_len + input_data

            # Make state_call
            result = self.subtensor.substrate.rpc_request(
                'state_call',
                ['ContractsApi_call', '0x' + call_params.hex()]
            )

            if not result.get('result'):
                return None

            raw = bytes.fromhex(result['result'].replace('0x', ''))

            # Parse response to extract return data
            # Structure: gas_consumed(16) + more_data... + return_value
            if len(raw) < 32:
                return None

            # For simple u32 returns, the value is typically at offset 27
            # For more complex returns, we'd need full SCALE decoding
            # Return the raw bytes after gas info for caller to decode
            return raw[16:]

        except Exception as e:
            bt.logging.debug(f'Raw contract read failed: {e}')
            return None

    def _extract_u32_from_response(self, response_bytes: bytes) -> Optional[int]:
        """Extract u32 value from Ink! 5 contract response bytes."""
        if not response_bytes or len(response_bytes) < 15:
            return None

        # For simple u32 returns in Ink! 5, value is at offset 11 in the response after gas
        # (offset 27 from raw, minus 16 for gas = 11)
        try:
            import struct
            return struct.unpack_from('<I', response_bytes, 11)[0]
        except Exception:
            return None

    def _extract_u128_from_response(self, response_bytes: bytes) -> Optional[int]:
        """Extract u128 value from Ink! 5 contract response bytes."""
        if not response_bytes or len(response_bytes) < 27:
            return None

        # Ink! 5 contract response structure after gas info (16 bytes):
        # - Result flags and status bytes
        # - Then the actual return value
        # For u128, we need to find where the 16-byte value starts
        try:
            import struct
            # The response structure varies, try to find the u128 value
            # Typically at offset 11 after gas (which is already removed)
            # u128 is 16 bytes little-endian
            low = struct.unpack_from('<Q', response_bytes, 11)[0]
            high = struct.unpack_from('<Q', response_bytes, 19)[0]
            return low + (high << 64)
        except Exception:
            return None

    def _read_contract_u128(self, method_name: str) -> int:
        """
        Read a u128 value from a no-arg contract method using raw RPC.

        This is a workaround for substrate-interface Ink! 5 decoding issues.

        Args:
            method_name: Contract method name

        Returns:
            u128 value, or 0 on error
        """
        response = self._raw_contract_read(method_name)
        if response is None:
            return 0

        value = self._extract_u128_from_response(response)
        return value if value is not None else 0

    def _read_contract_u32(self, method_name: str) -> int:
        """
        Read a u32 value from a no-arg contract method using raw RPC.

        Args:
            method_name: Contract method name

        Returns:
            u32 value, or 0 on error
        """
        response = self._raw_contract_read(method_name)
        if response is None:
            return 0

        value = self._extract_u32_from_response(response)
        return value if value is not None else 0

    @staticmethod
    def hash_url(url: str) -> bytes:
        """
        Hash a GitHub URL for deduplication (matches contract implementation).

        Args:
            url: GitHub issue URL

        Returns:
            32-byte hash of the URL
        """
        return hashlib.sha256(url.encode()).digest()

    @staticmethod
    def hash_pr_url(pr_url: str) -> bytes:
        """
        Hash a PR URL for on-chain storage.

        Args:
            pr_url: GitHub PR URL

        Returns:
            32-byte hash of the PR URL
        """
        return hashlib.sha256(pr_url.encode()).digest()

    # =========================================================================
    # Query Functions (Read-only)
    # =========================================================================

    def _get_read_keypair(self) -> 'Keypair':
        """Get a keypair for read-only contract calls."""
        if not SUBSTRATE_INTERFACE_AVAILABLE:
            return None
        # Use Alice as a dummy caller for read-only queries
        return Keypair.create_from_uri('//Alice')

    def _get_child_storage_key(self) -> Optional[str]:
        """
        Get the child storage key for the contract's trie.

        Returns:
            Hex-encoded child storage key or None if contract doesn't exist
        """
        if not self.subtensor or not self.contract_address:
            return None

        try:
            contract_info = self.subtensor.substrate.query(
                'Contracts', 'ContractInfoOf', [self.contract_address]
            )
            if not contract_info:
                return None

            # Handle both object with .value and direct dict returns
            if hasattr(contract_info, 'value'):
                info = contract_info.value
            else:
                info = contract_info

            if not info or 'trie_id' not in info:
                return None

            trie_id = info['trie_id']

            # Handle different formats: hex string, tuple of ints, or tuple wrapper
            if isinstance(trie_id, str):
                trie_id_hex = trie_id.replace('0x', '')
                trie_id_bytes = bytes.fromhex(trie_id_hex)
            elif isinstance(trie_id, (tuple, list)):
                # Might be ((bytes...),) or (bytes...) - unwrap if needed
                if len(trie_id) == 1 and isinstance(trie_id[0], (tuple, list)):
                    trie_id = trie_id[0]
                trie_id_bytes = bytes(trie_id)
            elif isinstance(trie_id, bytes):
                trie_id_bytes = trie_id
            else:
                bt.logging.debug(f'Unknown trie_id format: {type(trie_id)}')
                return None

            prefix = b':child_storage:default:'
            return '0x' + (prefix + trie_id_bytes).hex()
        except Exception as e:
            bt.logging.debug(f'Error getting child storage key: {e}')
            return None

    def _compute_ink5_lazy_key(self, root_key_hex: str, encoded_key: bytes) -> str:
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

    def _read_packed_storage(self) -> Optional[dict]:
        """
        Read the packed root storage from the contract.

        Returns:
            Dict with next_issue_id, next_competition_id, etc. or None on error
        """
        import struct

        child_key = self._get_child_storage_key()
        if not child_key:
            return None

        try:
            # Get all storage keys
            keys_result = self.subtensor.substrate.rpc_request(
                'childstate_getKeysPaged', [child_key, '0x', 10, None, None]
            )
            keys = keys_result.get('result', [])

            # Find the packed storage key (ends with 00000000)
            packed_key = None
            for k in keys:
                if k.endswith('00000000'):
                    packed_key = k
                    break

            if not packed_key:
                return None

            # Read the packed storage value
            val_result = self.subtensor.substrate.rpc_request(
                'childstate_getStorage', [child_key, packed_key, None]
            )
            if not val_result.get('result'):
                return None

            data = bytes.fromhex(val_result['result'].replace('0x', ''))

            # Decode packed struct (minimum 74 bytes for core fields)
            if len(data) < 74:
                return None

            offset = 64  # Skip owner (32) + treasury (32)
            netuid = struct.unpack_from('<H', data, offset)[0]
            offset += 2
            next_issue_id = struct.unpack_from('<Q', data, offset)[0]
            offset += 8
            next_competition_id = struct.unpack_from('<Q', data, offset)[0]

            return {
                'netuid': netuid,
                'next_issue_id': next_issue_id,
                'next_competition_id': next_competition_id,
            }
        except Exception as e:
            bt.logging.debug(f'Error reading packed storage: {e}')
            return None

    def _read_issue_from_child_storage(self, issue_id: int) -> Optional[ContractIssue]:
        """
        Read a single issue from contract child storage using Ink! 5 lazy mapping keys.

        Args:
            issue_id: The issue ID to read

        Returns:
            ContractIssue or None if not found
        """
        import struct

        child_key = self._get_child_storage_key()
        if not child_key:
            return None

        try:
            # Compute lazy mapping key for issues (root key: 52789899)
            encoded_id = struct.pack('<Q', issue_id)
            lazy_key = self._compute_ink5_lazy_key('52789899', encoded_id)

            val_result = self.subtensor.substrate.rpc_request(
                'childstate_getStorage', [child_key, lazy_key, None]
            )
            if not val_result.get('result'):
                return None

            data = bytes.fromhex(val_result['result'].replace('0x', ''))

            # Decode Issue struct
            offset = 0
            stored_id = struct.unpack_from('<Q', data, offset)[0]
            offset += 8

            github_url_hash = data[offset:offset + 32]
            offset += 32

            # String: compact-encoded length
            len_byte = data[offset]
            if len_byte & 0x03 == 0:
                str_len = len_byte >> 2
                offset += 1
            elif len_byte & 0x03 == 1:
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
            offset += 1

            registered_at_block = struct.unpack_from('<I', data, offset)[0]

            return ContractIssue(
                id=stored_id,
                github_url_hash=github_url_hash,
                repository_full_name=repo_name,
                issue_number=issue_number,
                bounty_amount=int(bounty_amount),
                target_bounty=int(target_bounty),
                status=IssueStatus(status_byte),
                registered_at_block=registered_at_block,
                is_fully_funded=int(bounty_amount) >= int(target_bounty),
            )
        except Exception as e:
            bt.logging.debug(f'Error reading issue {issue_id} from child storage: {e}')
            return None

    def _read_competition_from_child_storage(self, comp_id: int) -> Optional[ContractCompetition]:
        """
        Read a single competition from contract child storage using Ink! 5 lazy mapping keys.

        Args:
            comp_id: The competition ID to read

        Returns:
            ContractCompetition or None if not found
        """
        import struct

        child_key = self._get_child_storage_key()
        if not child_key:
            return None

        try:
            # Compute lazy mapping key for competitions (root key: f3a8d93e)
            encoded_id = struct.pack('<Q', comp_id)
            lazy_key = self._compute_ink5_lazy_key('f3a8d93e', encoded_id)

            val_result = self.subtensor.substrate.rpc_request(
                'childstate_getStorage', [child_key, lazy_key, None]
            )
            if not val_result.get('result'):
                return None

            data = bytes.fromhex(val_result['result'].replace('0x', ''))

            # Decode Competition struct
            offset = 0
            stored_id = struct.unpack_from('<Q', data, offset)[0]
            offset += 8

            issue_id = struct.unpack_from('<Q', data, offset)[0]
            offset += 8

            miner1_hotkey = self.subtensor.substrate.ss58_encode(data[offset:offset + 32].hex())
            offset += 32

            miner2_hotkey = self.subtensor.substrate.ss58_encode(data[offset:offset + 32].hex())
            offset += 32

            start_block = struct.unpack_from('<I', data, offset)[0]
            offset += 4

            submission_window_end = struct.unpack_from('<I', data, offset)[0]
            offset += 4

            deadline_block = struct.unpack_from('<I', data, offset)[0]
            offset += 4

            status_byte = data[offset]
            offset += 1

            winner_hotkey = self.subtensor.substrate.ss58_encode(data[offset:offset + 32].hex())
            offset += 32

            winning_pr_hash = data[offset:offset + 32]
            offset += 32

            payout_lo, payout_hi = struct.unpack_from('<QQ', data, offset)
            payout_amount = payout_lo + (payout_hi << 64)

            return ContractCompetition(
                id=stored_id,
                issue_id=issue_id,
                miner1_hotkey=miner1_hotkey,
                miner2_hotkey=miner2_hotkey,
                start_block=start_block,
                submission_window_end_block=submission_window_end,
                deadline_block=deadline_block,
                status=CompetitionStatus(status_byte),
                winner_hotkey=winner_hotkey if winner_hotkey != '5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM' else None,
                winning_pr_url_hash=winning_pr_hash if winning_pr_hash != b'\x00' * 32 else None,
                payout_amount=int(payout_amount) if payout_amount > 0 else None,
            )
        except Exception as e:
            bt.logging.debug(f'Error reading competition {comp_id} from child storage: {e}')
            return None

    def get_available_issues(self) -> List[ContractIssue]:
        """
        Query contract for issues with status=Active (ready for competition).

        Uses direct child storage reads to bypass Ink! 5 type decoding issues.

        Returns:
            List of active issues available for competition
        """
        if not self._ensure_contract():
            bt.logging.debug('Contract not ready, returning empty issue list')
            return []

        # Use direct child storage reads (bypasses Ink! 5 type issues)
        try:
            packed = self._read_packed_storage()
            if not packed:
                bt.logging.debug('Could not read packed storage')
                return []

            next_issue_id = packed.get('next_issue_id', 1)
            if next_issue_id <= 1:
                return []

            issues = []
            for issue_id in range(1, next_issue_id):
                issue = self._read_issue_from_child_storage(issue_id)
                if issue and issue.status == IssueStatus.ACTIVE:
                    issues.append(issue)

            bt.logging.debug(f'Found {len(issues)} active issues via child storage')
            return issues
        except Exception as e:
            bt.logging.error(f'Error fetching available issues: {e}')
            return []

    def get_issue(self, issue_id: int) -> Optional[ContractIssue]:
        """
        Get a specific issue by ID.

        Uses direct child storage reads to bypass Ink! 5 type decoding issues.

        Args:
            issue_id: The issue ID to query

        Returns:
            Issue data if found, None otherwise
        """
        if not self._ensure_contract():
            return None

        # Use direct child storage read (bypasses Ink! 5 type issues)
        try:
            return self._read_issue_from_child_storage(issue_id)
        except Exception as e:
            bt.logging.error(f'Error fetching issue {issue_id}: {e}')
            return None

    def is_miner_in_competition(self, miner_hotkey: str) -> bool:
        """
        Check if a miner is currently in an active competition.

        Args:
            miner_hotkey: Miner's hotkey (SS58 address)

        Returns:
            True if miner is in an active competition
        """
        if not self._ensure_contract():
            return False

        try:
            keypair = self._get_read_keypair()
            result = self._contract.read(
                keypair,
                'is_miner_in_competition',
                args={'hotkey': miner_hotkey},
            )
            return bool(result.contract_result_data)
        except Exception as e:
            bt.logging.error(f'Error checking miner competition status: {e}')
            return False

    def get_active_competitions(self) -> List[ContractCompetition]:
        """
        Get all active competitions.

        Uses direct child storage reads to bypass Ink! 5 type decoding issues.

        Returns:
            List of active competitions
        """
        if not self._ensure_contract():
            return []

        # Use direct child storage reads (bypasses Ink! 5 type issues)
        try:
            packed = self._read_packed_storage()
            if not packed:
                bt.logging.debug('Could not read packed storage for competitions')
                return []

            next_comp_id = packed.get('next_competition_id', 1)
            if next_comp_id <= 1:
                return []

            competitions = []
            for comp_id in range(1, next_comp_id):
                comp = self._read_competition_from_child_storage(comp_id)
                if comp and comp.status == CompetitionStatus.ACTIVE:
                    competitions.append(comp)

            bt.logging.debug(f'Found {len(competitions)} active competitions via child storage')
            return competitions
        except Exception as e:
            bt.logging.error(f'Error fetching active competitions: {e}')
            return []

    def get_competition(self, competition_id: int) -> Optional[ContractCompetition]:
        """
        Get a specific competition by ID.

        Uses direct child storage reads to bypass Ink! 5 type decoding issues.

        Args:
            competition_id: The competition ID to query

        Returns:
            Competition data if found, None otherwise
        """
        if not self._ensure_contract():
            return None

        # Use direct child storage read (bypasses Ink! 5 type issues)
        try:
            return self._read_competition_from_child_storage(competition_id)
        except Exception as e:
            bt.logging.error(f'Error fetching competition {competition_id}: {e}')
            return None

    def get_pair_proposal(self, issue_id: int) -> Optional[PairProposal]:
        """
        Get the active pair proposal for an issue.

        Args:
            issue_id: The issue ID

        Returns:
            Active proposal if exists, None otherwise
        """
        if not self._ensure_contract():
            return None

        try:
            keypair = self._get_read_keypair()
            result = self._contract.read(
                keypair,
                'get_pair_proposal',
                args={'issue_id': issue_id},
            )
            if result.contract_result_data is None:
                return None
            return self._parse_pair_proposal(result.contract_result_data)
        except Exception as e:
            bt.logging.error(f'Error fetching pair proposal for issue {issue_id}: {e}')
            return None

    def get_alpha_pool(self) -> int:
        """
        Get the current alpha pool balance.

        Returns:
            Unallocated ALPHA in the pool (0 if contract not ready)
        """
        if not self._ensure_contract():
            return 0

        # Use raw RPC call due to substrate-interface Ink! 5 decoding issues
        try:
            value = self._read_contract_u128('get_alpha_pool')
            bt.logging.debug(f'Alpha pool (raw read): {value}')
            return value
        except Exception as e:
            bt.logging.error(f'Error fetching alpha pool: {e}')
            return 0

    # =========================================================================
    # Transaction Functions (Write)
    # =========================================================================

    def propose_pair(
        self,
        issue_id: int,
        miner1_hotkey: str,
        miner2_hotkey: str,
        wallet: bt.Wallet,
    ) -> bool:
        """
        Propose a miner pair for competition.

        Creates a new pair proposal or votes on an existing one if the same
        pair is proposed. Requires stake to vote.

        Args:
            issue_id: Issue to start competition for
            miner1_hotkey: First miner's hotkey
            miner2_hotkey: Second miner's hotkey
            wallet: Validator wallet for signing

        Returns:
            True if proposal/vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot propose pair: contract not ready')
            return False

        try:
            bt.logging.info(
                f'Proposing pair for issue {issue_id}: '
                f'{miner1_hotkey[:8]}... vs {miner2_hotkey[:8]}...'
            )

            # Create keypair from wallet hotkey
            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='propose_pair',
                args={
                    'issue_id': issue_id,
                    'miner1_hotkey': miner1_hotkey,
                    'miner2_hotkey': miner2_hotkey,
                },
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Pair proposal succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Pair proposal failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error proposing pair: {e}')
            return False

    def vote_pair(self, issue_id: int, wallet: bt.Wallet) -> bool:
        """
        Vote on an existing pair proposal.

        Adds the caller's stake-weighted vote to the proposal. If consensus
        (51%) is reached, the competition starts automatically.

        Args:
            issue_id: Issue with active proposal
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote pair: contract not ready')
            return False

        try:
            bt.logging.info(f'Voting on pair proposal for issue {issue_id}')

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='vote_pair',
                args={'issue_id': issue_id},
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Vote pair succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Vote pair failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting on pair: {e}')
            return False

    def vote_solution(
        self,
        competition_id: int,
        winner_hotkey: str,
        pr_url: str,
        wallet: bt.Wallet,
    ) -> bool:
        """
        Vote for a competition winner.

        Casts a stake-weighted vote for the proposed winner. If consensus
        (51%) is reached, the competition completes and bounty is paid.

        Args:
            competition_id: Competition to vote on
            winner_hotkey: Hotkey of the proposed winner
            pr_url: URL of the winning PR (will be hashed)
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote solution: contract not ready')
            return False

        try:
            pr_url_hash = self.hash_pr_url(pr_url)
            bt.logging.info(
                f'Voting solution for competition {competition_id}: '
                f'winner={winner_hotkey[:8]}...'
            )

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='vote_solution',
                args={
                    'competition_id': competition_id,
                    'winner_hotkey': winner_hotkey,
                    'pr_url_hash': list(pr_url_hash),  # Convert bytes to list for SCALE encoding
                },
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Vote solution succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Vote solution failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting solution: {e}')
            return False

    def vote_timeout(self, competition_id: int, wallet: bt.Wallet) -> bool:
        """
        Vote to timeout a competition that has passed its deadline.

        Casts a stake-weighted vote to timeout. If consensus (51%) is reached,
        the competition is cancelled and bounty returns to pool.

        Args:
            competition_id: Competition to timeout
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote timeout: contract not ready')
            return False

        try:
            bt.logging.info(f'Voting timeout for competition {competition_id}')

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='vote_timeout',
                args={'competition_id': competition_id},
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Vote timeout succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Vote timeout failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting timeout: {e}')
            return False

    def vote_cancel(
        self,
        competition_id: int,
        reason: str,
        wallet: bt.Wallet,
    ) -> bool:
        """
        Vote to cancel a competition (e.g., external solution detected).

        Args:
            competition_id: Competition to cancel
            reason: Reason for cancellation
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote cancel: contract not ready')
            return False

        try:
            reason_hash = hashlib.sha256(reason.encode()).digest()
            bt.logging.info(
                f'Voting cancel for competition {competition_id}: {reason}'
            )

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='vote_cancel',
                args={
                    'competition_id': competition_id,
                    'reason_hash': list(reason_hash),  # Convert bytes to list for SCALE encoding
                },
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                bt.logging.info(f'Vote cancel succeeded: {result.extrinsic_hash}')
                return True
            else:
                bt.logging.error(f'Vote cancel failed: {result.error_message}')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting cancel: {e}')
            return False

    # =========================================================================
    # Helper Functions
    # =========================================================================

    def _parse_issue(self, raw_data: dict) -> ContractIssue:
        """Parse raw contract data into ContractIssue."""
        # ink! contract uses snake_case keys
        github_url_hash = raw_data.get('github_url_hash')
        if github_url_hash is None:
            github_url_hash = raw_data.get('githubUrlHash', b'\x00' * 32)
        if isinstance(github_url_hash, str):
            hex_str = github_url_hash.replace('0x', '').replace('0X', '')
            github_url_hash = bytes.fromhex(hex_str) if hex_str else b'\x00' * 32
        elif isinstance(github_url_hash, list):
            github_url_hash = bytes(github_url_hash) if github_url_hash else b'\x00' * 32

        # Use explicit None check to avoid treating 0 as falsy
        bounty_amount = raw_data.get('bounty_amount')
        if bounty_amount is None:
            bounty_amount = raw_data.get('bountyAmount', 0)
        target_bounty = raw_data.get('target_bounty')
        if target_bounty is None:
            target_bounty = raw_data.get('targetBounty', 0)

        return ContractIssue(
            id=raw_data.get('id', 0),
            github_url_hash=github_url_hash,
            repository_full_name=raw_data.get('repository_full_name') or raw_data.get('repositoryFullName', ''),
            issue_number=raw_data.get('issue_number') or raw_data.get('issueNumber', 0),
            bounty_amount=int(bounty_amount),
            target_bounty=int(target_bounty),
            status=IssueStatus(raw_data.get('status', 0)),
            registered_at_block=raw_data.get('registered_at_block') or raw_data.get('registeredAtBlock', 0),
            is_fully_funded=int(bounty_amount) >= int(target_bounty),
        )

    def _parse_competition(self, raw_data: dict) -> ContractCompetition:
        """Parse raw contract data into ContractCompetition."""
        # ink! contract uses snake_case keys
        return ContractCompetition(
            id=raw_data.get('id', 0),
            issue_id=raw_data.get('issue_id') or raw_data.get('issueId', 0),
            miner1_hotkey=raw_data.get('miner1_hotkey') or raw_data.get('miner1Hotkey', ''),
            miner2_hotkey=raw_data.get('miner2_hotkey') or raw_data.get('miner2Hotkey', ''),
            start_block=raw_data.get('start_block') or raw_data.get('startBlock', 0),
            submission_window_end_block=raw_data.get('submission_window_end_block') or raw_data.get('submissionWindowEndBlock', 0),
            deadline_block=raw_data.get('deadline_block') or raw_data.get('deadlineBlock', 0),
            status=CompetitionStatus(raw_data.get('status', 0)),
            winner_hotkey=raw_data.get('winner_hotkey') or raw_data.get('winnerHotkey'),
            winning_pr_url_hash=raw_data.get('winning_pr_url_hash') or raw_data.get('winningPrUrlHash'),
            payout_amount=raw_data.get('payout_amount') or raw_data.get('payoutAmount'),
        )

    def _parse_pair_proposal(self, raw_data: dict) -> PairProposal:
        """Parse raw contract data into PairProposal."""
        return PairProposal(
            issue_id=raw_data.get('issue_id') or raw_data.get('issueId', 0),
            miner1_hotkey=raw_data.get('miner1_hotkey') or raw_data.get('miner1Hotkey', ''),
            miner2_hotkey=raw_data.get('miner2_hotkey') or raw_data.get('miner2Hotkey', ''),
            proposer=raw_data.get('proposer', ''),
            proposed_at_block=raw_data.get('proposed_at_block') or raw_data.get('proposedAtBlock', 0),
            total_stake_voted=raw_data.get('total_stake_voted') or raw_data.get('totalStakeVoted', 0),
        )

    # =========================================================================
    # Emission Harvesting Functions
    # =========================================================================

    def get_pending_emissions(self) -> int:
        """
        Query pending emissions on the treasury hotkey owned by the contract.

        Uses the contract's chain extension to query Subtensor staking info.

        Returns:
            Pending emission amount (0 if contract not ready or no emissions)
        """
        if not self._ensure_contract():
            return 0

        # Use raw RPC call due to substrate-interface Ink! 5 decoding issues
        try:
            value = self._read_contract_u128('get_pending_emissions')
            bt.logging.debug(f'Pending emissions (raw read): {value}')
            return value
        except Exception as e:
            bt.logging.error(f'Error fetching pending emissions: {e}')
            return 0

    def get_overflow_pool(self) -> int:
        """
        Query the overflow pool balance (emissions that couldn't be recycled).

        Returns:
            Overflow pool balance (0 if contract not ready)
        """
        if not self._ensure_contract():
            return 0

        # Use raw RPC call due to substrate-interface Ink! 5 decoding issues
        try:
            value = self._read_contract_u128('get_overflow_pool')
            bt.logging.debug(f'Overflow pool (raw read): {value}')
            return value
        except Exception as e:
            bt.logging.error(f'Error fetching overflow pool: {e}')
            return 0

    def get_last_harvest_block(self) -> int:
        """
        Query the block number of the last harvest.

        Returns:
            Last harvest block number (0 if never harvested or not ready)
        """
        if not self._ensure_contract():
            return 0

        # Use raw RPC call due to substrate-interface Ink! 5 decoding issues
        try:
            value = self._read_contract_u32('get_last_harvest_block')
            bt.logging.debug(f'Last harvest block (raw read): {value}')
            return value
        except Exception as e:
            bt.logging.error(f'Error fetching last harvest block: {e}')
            return 0

    def harvest_emissions(self, wallet: bt.Wallet) -> Optional[dict]:
        """
        Harvest emissions from the treasury hotkey and distribute to bounties.

        This function is PERMISSIONLESS - anyone can call it.
        It queries pending emissions, fills bounties, and recycles the remainder.

        Args:
            wallet: Wallet for signing the transaction (any valid wallet works)

        Returns:
            HarvestResult dict with harvested, bounties_filled, recycled; None on error
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot harvest emissions: contract not ready')
            return None

        try:
            # Check pending emissions first
            pending = self.get_pending_emissions()
            if pending == 0:
                bt.logging.debug('No pending emissions to harvest')
                return {'harvested': 0, 'bounties_filled': 0, 'recycled': 0, 'status': 'no_pending'}

            bt.logging.info(f'Harvesting {pending} pending emissions...')

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='harvest_emissions',
                args={},
                value=0,
                gas_limit={
                    'ref_time': 50_000_000_000,  # 50 billion
                    'proof_size': 1_000_000,  # 1 MB
                },
            )

            if result.is_success:
                # Parse result from contract return value
                harvest_result = result.contract_result_data
                if harvest_result:
                    harvested = harvest_result.get('harvested', 0)
                    bounties_filled = harvest_result.get('bounties_filled', 0)
                    recycled = harvest_result.get('recycled', 0)
                    bt.logging.success(
                        f'Harvested {harvested} emissions, '
                        f'filled {bounties_filled} bounties, '
                        f'recycled {recycled}'
                    )
                    return {
                        'harvested': harvested,
                        'bounties_filled': bounties_filled,
                        'recycled': recycled,
                        'status': 'success',
                    }
                else:
                    bt.logging.info(f'Harvest succeeded: {result.extrinsic_hash}')
                    return {'status': 'success', 'tx_hash': result.extrinsic_hash}
            else:
                bt.logging.warning(f'Harvest failed: {result.error_message}')
                return {'status': 'failed', 'error': result.error_message}

        except Exception as e:
            bt.logging.error(f'Harvest error: {e}')
            return {'status': 'error', 'error': str(e)}

    def payout_bounty(
        self,
        competition_id: int,
        miner_coldkey: str,
        wallet: bt.Wallet,
    ) -> Optional[int]:
        """
        Pay out a completed bounty to the winning miner.

        This transfers stake ownership from the treasury hotkey to the miner's coldkey.
        Only the contract owner can call this function.

        Args:
            competition_id: ID of the completed competition
            miner_coldkey: SS58 address of the miner's coldkey
            wallet: Owner wallet for signing

        Returns:
            Payout amount on success, None on error
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot payout bounty: contract not ready')
            return None

        try:
            bt.logging.info(
                f'Paying out bounty for competition {competition_id} '
                f'to miner {miner_coldkey[:16]}...'
            )

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='payout_bounty',
                args={
                    'competition_id': competition_id,
                    'miner_coldkey': miner_coldkey,
                },
                value=0,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if result.is_success:
                payout_amount = result.contract_result_data
                bt.logging.success(
                    f'Bounty payout succeeded: {payout_amount} to {miner_coldkey[:16]}...'
                )
                return int(payout_amount) if payout_amount else 0
            else:
                bt.logging.error(f'Bounty payout failed: {result.error_message}')
                return None

        except Exception as e:
            bt.logging.error(f'Error paying out bounty: {e}')
            return None

    def get_stake_via_subtensor(
        self,
        treasury_hotkey: str,
        treasury_coldkey: str,
        netuid: int,
    ) -> int:
        """
        Query stake directly via subtensor RPC (bypasses chain extension).

        This is a workaround for local development where the chain extension
        for get_stake_info may not be implemented. Queries the TotalHotkeyAlpha
        directly from the subtensor storage.

        Args:
            treasury_hotkey: SS58 address of the treasury hotkey
            treasury_coldkey: SS58 address of the treasury coldkey (unused but kept for API compat)
            netuid: Subnet ID

        Returns:
            Stake amount in RAO (0 if not found or error)
        """
        if not self.subtensor:
            bt.logging.warning('Cannot query stake: subtensor not configured')
            return 0

        try:
            # Use TotalHotkeyAlpha which returns actual stake in RAO
            # (Alpha returns a U64F64 weighted value which is harder to interpret)
            result = self.subtensor.substrate.query(
                module='SubtensorModule',
                storage_function='TotalHotkeyAlpha',
                params=[treasury_hotkey, netuid],
            )
            stake = 0

            if result is None:
                stake = 0
            elif hasattr(result, 'value'):
                val = result.value
                if val is not None:
                    stake = int(val)
            elif isinstance(result, (int, float)):
                stake = int(result)
            else:
                # Try to convert directly
                stake = int(result) if result else 0

            bt.logging.debug(
                f'Stake via TotalHotkeyAlpha: hotkey={treasury_hotkey[:16]}..., '
                f'netuid={netuid}, stake={stake} RAO ({stake/1e9:.4f} TAO)'
            )
            return stake
        except Exception as e:
            bt.logging.error(f'Error querying stake via subtensor: {e}')
            return 0

    def harvest_emissions_external(
        self,
        wallet: bt.Wallet,
        amount: int,
    ) -> Optional[dict]:
        """
        Harvest emissions using an externally-provided amount.

        This is a workaround for local development where the chain extension
        for get_stake_info may not be implemented. The validator queries
        stake directly via subtensor RPC and passes the amount here.

        Args:
            wallet: Wallet for signing the transaction
            amount: Amount of emissions to harvest (from subtensor query)

        Returns:
            HarvestResult dict with harvested, bounties_filled, recycled; None on error
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot harvest emissions: contract not ready')
            return None

        if amount == 0:
            bt.logging.debug('No emissions to harvest (amount=0)')
            return {'harvested': 0, 'bounties_filled': 0, 'recycled': 0, 'status': 'no_pending'}

        try:
            bt.logging.info(f'Harvesting {amount} emissions (external)...')

            keypair = Keypair.create_from_uri(wallet.hotkey.ss58_address)

            result = self._contract.exec(
                keypair=keypair,
                method='harvest_emissions_external',
                args={'amount': amount},
                value=0,
                gas_limit={
                    'ref_time': 50_000_000_000,  # 50 billion
                    'proof_size': 1_000_000,  # 1 MB
                },
            )

            if result.is_success:
                harvest_result = result.contract_result_data
                if harvest_result:
                    harvested = harvest_result.get('harvested', 0)
                    bounties_filled = harvest_result.get('bounties_filled', 0)
                    recycled = harvest_result.get('recycled', 0)
                    bt.logging.success(
                        f'Harvested (external) {harvested} emissions, '
                        f'filled {bounties_filled} bounties, '
                        f'recycled {recycled}'
                    )
                    return {
                        'harvested': harvested,
                        'bounties_filled': bounties_filled,
                        'recycled': recycled,
                        'status': 'success',
                    }
                else:
                    bt.logging.info(f'Harvest (external) succeeded: {result.extrinsic_hash}')
                    return {'status': 'success', 'tx_hash': result.extrinsic_hash}
            else:
                bt.logging.warning(f'Harvest (external) failed: {result.error_message}')
                return {'status': 'failed', 'error': result.error_message}

        except Exception as e:
            bt.logging.error(f'Harvest (external) error: {e}')
            return {'status': 'error', 'error': str(e)}
