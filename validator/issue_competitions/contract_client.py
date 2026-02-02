# The MIT License (MIT)
# Copyright 2025 Entrius

"""Client for interacting with the Issue Bounty smart contract"""

import hashlib
import json
import os
import struct
import traceback
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

import bittensor as bt

try:
    from substrateinterface import Keypair
    from substrateinterface.contracts import ContractInstance
    from substrateinterface.exceptions import ExtrinsicNotFound

    SUBSTRATE_INTERFACE_AVAILABLE = True
except ImportError:
    SUBSTRATE_INTERFACE_AVAILABLE = False
    ContractInstance = None
    Keypair = None
    ExtrinsicNotFound = None

try:
    from async_substrate_interface.errors import ExtrinsicNotFound as AsyncExtrinsicNotFound
except ImportError:
    AsyncExtrinsicNotFound = None

try:
    from async_substrate_interface.errors import SubstrateRequestException
except ImportError:
    try:
        from substrateinterface.exceptions import SubstrateRequestException
    except ImportError:
        SubstrateRequestException = None

# Default gas limits for contract calls
DEFAULT_GAS_LIMIT = {
    'ref_time': 10_000_000_000,
    'proof_size': 500_000,
}

GITTENSOR_CONFIG_PATH = Path.home() / '.gittensor' / 'contract_config.json'
CONTRACT_METADATA_FILENAME = 'issue_bounty_manager.contract'


def _find_contract_metadata_path() -> Optional[Path]:
    """Find the contract metadata file path."""
    env_path = os.environ.get('CONTRACT_METADATA_PATH')
    if env_path:
        path = Path(env_path)
        if path.exists():
            return path

    if GITTENSOR_CONFIG_PATH.exists():
        try:
            config = json.loads(GITTENSOR_CONFIG_PATH.read_text())
            metadata_path = config.get('metadata_path')
            if metadata_path:
                path = Path(metadata_path)
                if path.exists():
                    return path
        except (json.JSONDecodeError, IOError):
            pass

    current_file = Path(__file__).resolve()
    repo_root_candidates = [
        current_file.parent.parent.parent.parent,
    ]

    try:
        import gittensor
        if hasattr(gittensor, '__path__'):
            for gittensor_path in gittensor.__path__:
                repo_root_candidates.append(Path(gittensor_path).parent)
    except (ImportError, AttributeError):
        pass

    for repo_root in repo_root_candidates:
        search_paths = [
            repo_root / 'smart-contracts' / 'ink' / 'target' / 'ink' / CONTRACT_METADATA_FILENAME,
            repo_root / 'smart-contracts' / 'target' / 'ink' / CONTRACT_METADATA_FILENAME,
        ]
        for path in search_paths:
            if path.exists():
                return path

    return None


def get_contract_metadata_path() -> Optional[Path]:
    """Get the contract metadata file path (cached)."""
    if not hasattr(get_contract_metadata_path, '_cached_path'):
        get_contract_metadata_path._cached_path = _find_contract_metadata_path()
    return get_contract_metadata_path._cached_path


def _get_default_metadata_path() -> Path:
    """Get default metadata path, with fallback."""
    path = get_contract_metadata_path()
    if path:
        return path
    return Path.home() / '.gittensor' / 'contracts' / CONTRACT_METADATA_FILENAME


def get_contract_address_from_config() -> Optional[str]:
    """Get contract address from environment or config file."""
    env_addr = os.environ.get('CONTRACT_ADDRESS')
    if env_addr:
        return env_addr

    if GITTENSOR_CONFIG_PATH.exists():
        try:
            with open(GITTENSOR_CONFIG_PATH) as f:
                config = json.load(f)
                addr = config.get('contract_address')
                if addr:
                    return addr
        except (json.JSONDecodeError, IOError):
            pass

    return None


class IssueStatus(Enum):
    """Status of an issue in its lifecycle"""
    REGISTERED = 0
    ACTIVE = 1
    COMPLETED = 2
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


class IssueCompetitionContractClient:
    """
    Client for interacting with the Issue Bounty smart contract

    This client handles all read/write operations with the on-chain contract
    for the issue bounties sub-mechanism.
    """

    def __init__(
        self,
        contract_address: Optional[str] = None,
        subtensor: Optional[bt.Subtensor] = None,
        metadata_path: Optional[Path] = None,
    ):
        """Initialize the contract client."""
        if contract_address:
            self.contract_address = contract_address
        else:
            config_addr = get_contract_address_from_config()
            if config_addr:
                self.contract_address = config_addr
            else:
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

        if not self.contract_address:
            bt.logging.warning('Issue bounty contract address not set')

    def _ensure_contract(self) -> bool:
        """Ensure contract connection is established."""
        if not self.contract_address:
            return False

        if not SUBSTRATE_INTERFACE_AVAILABLE:
            bt.logging.warning('substrate-interface library not available')
            return False

        if self._contract is None and not self._initialized:
            self._initialized = True
            try:
                if not self.metadata_path.exists():
                    bt.logging.warning(f'Contract metadata not found at {self.metadata_path}')
                    return False

                self._contract = ContractInstance.create_from_address(
                    contract_address=self.contract_address,
                    metadata_file=str(self.metadata_path),
                    substrate=self.subtensor.substrate,
                )
                bt.logging.info(f'Connected to contract at {self.contract_address}')
            except Exception as e:
                bt.logging.error(f'Failed to initialize contract: {e}')
                self._contract = None
                return False

        return self._contract is not None

    @staticmethod
    def hash_url(url: str) -> bytes:
        """Hash a URL for deduplication."""
        return hashlib.sha256(url.encode()).digest()

    @staticmethod
    def hash_pr_url(pr_url: str) -> bytes:
        """Hash a PR URL for on-chain storage."""
        return hashlib.sha256(pr_url.encode()).digest()

    # =========================================================================
    # Query Functions (Read-only)
    # =========================================================================

    def _get_read_keypair(self) -> 'Keypair':
        """Get a keypair for read-only contract calls."""
        if not SUBSTRATE_INTERFACE_AVAILABLE:
            return None
        return Keypair.create_from_uri('//Alice')

    def _get_child_storage_key(self) -> Optional[str]:
        """Get the child storage key for the contract's trie."""
        if not self.subtensor or not self.contract_address:
            return None

        try:
            contract_info = self.subtensor.substrate.query(
                'Contracts', 'ContractInfoOf', [self.contract_address]
            )
            if not contract_info:
                return None

            if hasattr(contract_info, 'value'):
                info = contract_info.value
            else:
                info = contract_info

            if not info or 'trie_id' not in info:
                return None

            trie_id = info['trie_id']

            if isinstance(trie_id, str):
                trie_id_hex = trie_id.replace('0x', '')
                trie_id_bytes = bytes.fromhex(trie_id_hex)
            elif isinstance(trie_id, (tuple, list)):
                if len(trie_id) == 1 and isinstance(trie_id[0], (tuple, list)):
                    trie_id = trie_id[0]
                trie_id_bytes = bytes(trie_id)
            elif isinstance(trie_id, bytes):
                trie_id_bytes = trie_id
            else:
                return None

            prefix = b':child_storage:default:'
            return '0x' + (prefix + trie_id_bytes).hex()
        except Exception as e:
            bt.logging.debug(f'Error getting child storage key: {e}')
            return None

    def _compute_ink5_lazy_key(self, root_key_hex: str, encoded_key: bytes) -> str:
        """Compute Ink! 5 lazy mapping storage key using blake2_128concat."""
        root_key = bytes.fromhex(root_key_hex.replace('0x', ''))
        data = root_key + encoded_key
        h = hashlib.blake2b(data, digest_size=16).digest()
        return '0x' + (h + data).hex()

    def _read_packed_storage(self) -> Optional[dict]:
        """Read the packed root storage from the contract"""
        child_key = self._get_child_storage_key()
        if not child_key:
            return None

        try:
            keys_result = self.subtensor.substrate.rpc_request(
                'childstate_getKeysPaged', [child_key, '0x', 10, None, None]
            )
            keys = keys_result.get('result', [])

            packed_key = None
            for k in keys:
                if k.endswith('00000000'):
                    packed_key = k
                    break

            if not packed_key:
                return None

            val_result = self.subtensor.substrate.rpc_request(
                'childstate_getStorage', [child_key, packed_key, None]
            )
            if not val_result.get('result'):
                return None

            data = bytes.fromhex(val_result['result'].replace('0x', ''))

            # owner (32) + treasury (32) + validator_hotkey (32) + netuid (2) + next_issue_id (8)
            if len(data) < 106:
                return None

            offset = 96  # Skip owner + treasury + validator_hotkey
            netuid = struct.unpack_from('<H', data, offset)[0]
            offset += 2
            next_issue_id = struct.unpack_from('<Q', data, offset)[0]

            return {
                'netuid': netuid,
                'next_issue_id': next_issue_id,
            }
        except Exception as e:
            bt.logging.debug(f'Error reading packed storage: {e}')
            return None

    def _read_issue_from_child_storage(self, issue_id: int) -> Optional[ContractIssue]:
        """Read a single issue from contract child storage."""
        child_key = self._get_child_storage_key()
        if not child_key:
            return None

        try:
            encoded_id = struct.pack('<Q', issue_id)
            lazy_key = self._compute_ink5_lazy_key('52789899', encoded_id)

            val_result = self.subtensor.substrate.rpc_request(
                'childstate_getStorage', [child_key, lazy_key, None]
            )
            if not val_result.get('result'):
                return None

            data = bytes.fromhex(val_result['result'].replace('0x', ''))

            offset = 0
            stored_id = struct.unpack_from('<Q', data, offset)[0]
            offset += 8

            github_url_hash = data[offset:offset + 32]
            offset += 32

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
            bt.logging.debug(f'Error reading issue {issue_id}: {e}')
            return None

    def get_issues_by_status(self, status: IssueStatus) -> List[ContractIssue]:
        """Get all issues with a given status."""
        if not self._ensure_contract():
            return []

        try:
            packed = self._read_packed_storage()
            if not packed:
                return []

            next_issue_id = packed.get('next_issue_id', 1)
            if next_issue_id <= 1:
                return []

            MAX_REASONABLE_ISSUE_ID = 1_000_000
            if next_issue_id > MAX_REASONABLE_ISSUE_ID:
                bt.logging.warning(f'next_issue_id ({next_issue_id}) unreasonably large')
                return []

            issues = []
            for issue_id in range(1, next_issue_id):
                issue = self._read_issue_from_child_storage(issue_id)
                if issue and issue.status == status:
                    issues.append(issue)

            return issues
        except Exception as e:
            bt.logging.error(f'Error fetching issues by status: {e}')
            return []

    def get_available_issues(self) -> List[ContractIssue]:
        """Query contract for issues with status=Active."""
        return self.get_issues_by_status(IssueStatus.ACTIVE)

    def get_issue(self, issue_id: int) -> Optional[ContractIssue]:
        """Get a specific issue by ID."""
        if not self._ensure_contract():
            return None

        try:
            return self._read_issue_from_child_storage(issue_id)
        except Exception as e:
            bt.logging.error(f'Error fetching issue {issue_id}: {e}')
            return None

    def get_alpha_pool(self) -> int:
        """Get the current alpha pool balance."""
        if not self._ensure_contract():
            return 0

        try:
            value = self._read_contract_u128('get_alpha_pool')
            return value
        except Exception as e:
            bt.logging.error(f'Error fetching alpha pool: {e}')
            return 0

    def _read_contract_u128(self, method_name: str) -> int:
        """Read a u128 value from a no-arg contract method."""
        response = self._raw_contract_read(method_name)
        if response is None:
            return 0

        value = self._extract_u128_from_response(response)
        return value if value is not None else 0

    def _read_contract_u32(self, method_name: str) -> int:
        """Read a u32 value from a no-arg contract method."""
        response = self._raw_contract_read(method_name)
        if response is None:
            return 0

        value = self._extract_u32_from_response(response)
        return value if value is not None else 0

    def _raw_contract_read(self, method_name: str, args: dict = None) -> Optional[bytes]:
        """Read from contract using raw RPC call."""
        if not self._ensure_contract():
            return None

        try:
            with open(self.metadata_path) as f:
                metadata = json.load(f)

            selector = None
            for msg in metadata.get('spec', {}).get('messages', []):
                if msg['label'] == method_name:
                    selector = bytes.fromhex(msg['selector'].replace('0x', ''))
                    break

            if not selector:
                return None

            input_data = selector

            caller = Keypair.create_from_uri('//Alice')

            origin = bytes.fromhex(self.subtensor.substrate.ss58_decode(caller.ss58_address))
            dest = bytes.fromhex(self.subtensor.substrate.ss58_decode(self.contract_address))
            value = b'\x00' * 16
            gas_limit = b'\x00'
            storage_limit = b'\x00'

            data_len = len(input_data)
            if data_len < 64:
                compact_len = bytes([data_len << 2])
            else:
                compact_len = bytes([(data_len << 2) | 1, data_len >> 6])

            call_params = origin + dest + value + gas_limit + storage_limit + compact_len + input_data

            result = self.subtensor.substrate.rpc_request(
                'state_call',
                ['ContractsApi_call', '0x' + call_params.hex()]
            )

            if not result.get('result'):
                return None

            raw = bytes.fromhex(result['result'].replace('0x', ''))

            if len(raw) < 32:
                return None

            return raw[16:]

        except Exception as e:
            bt.logging.debug(f'Raw contract read failed: {e}')
            return None

    def _extract_u32_from_response(self, response_bytes: bytes) -> Optional[int]:
        """Extract u32 value from response bytes."""
        if not response_bytes or len(response_bytes) < 15:
            return None
        try:
            return struct.unpack_from('<I', response_bytes, 11)[0]
        except Exception:
            return None

    def _extract_u128_from_response(self, response_bytes: bytes) -> Optional[int]:
        """Extract u128 value from response bytes."""
        if not response_bytes or len(response_bytes) < 27:
            return None
        try:
            low = struct.unpack_from('<Q', response_bytes, 11)[0]
            high = struct.unpack_from('<Q', response_bytes, 19)[0]
            return low + (high << 64)
        except Exception:
            return None

    # =========================================================================
    # Transaction Functions (Write)
    # =========================================================================

    def vote_solution(
        self,
        issue_id: int,
        solver_hotkey: str,
        solver_coldkey: str,
        pr_url: str,
        wallet: bt.Wallet,
    ) -> bool:
        """
        Vote for a solution on an active issue

        Casts a vote for the proposed solver. When consensus is reached,
        the issue completes and bounty is automatically paid to the
        solver's coldkey.

        Args:
            issue_id: Issue to vote on
            solver_hotkey: Hotkey of the proposed solver
            solver_coldkey: Coldkey to receive the payout
            pr_url: URL of the solving PR (will be hashed)
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
                f'Voting solution for issue {issue_id}: solver={solver_hotkey[:8]}...'
            )

            keypair = wallet.hotkey
            tx_hash = self._exec_contract_raw(
                method_name='vote_solution',
                args={
                    'issue_id': issue_id,
                    'solver_hotkey': solver_hotkey,
                    'solver_coldkey': solver_coldkey,
                    'pr_url_hash': pr_url_hash,
                },
                keypair=keypair,
            )

            if tx_hash:
                bt.logging.info(f'Vote solution succeeded: {tx_hash}')
                return True
            else:
                bt.logging.error('Vote solution failed')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting solution: {e}')
            return False

    def vote_cancel_issue(
        self,
        issue_id: int,
        reason: str,
        wallet: bt.Wallet,
    ) -> bool:
        """
        Vote to cancel an issue.

        Args:
            issue_id: Issue to cancel
            reason: Reason for cancellation
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        if not self._ensure_contract():
            bt.logging.warning('Cannot vote cancel issue: contract not ready')
            return False

        try:
            reason_hash = hashlib.sha256(reason.encode()).digest()
            bt.logging.info(f'Voting cancel for issue {issue_id}: {reason}')

            keypair = wallet.hotkey
            tx_hash = self._exec_contract_raw(
                method_name='vote_cancel_issue',
                args={
                    'issue_id': issue_id,
                    'reason_hash': reason_hash,
                },
                keypair=keypair,
            )

            if tx_hash:
                bt.logging.info(f'Vote cancel issue succeeded: {tx_hash}')
                return True
            else:
                bt.logging.error('Vote cancel issue failed')
                return False

        except Exception as e:
            bt.logging.error(f'Error voting cancel issue: {e}')
            return False

    # =========================================================================
    # Raw Extrinsic Execution (Ink! 5 Workaround)
    # =========================================================================

    def _exec_contract_raw(
        self,
        method_name: str,
        args: dict,
        keypair,
        gas_limit: dict = None,
        value: int = 0,
    ) -> Optional[str]:
        """Execute a contract method using raw extrinsic submission."""
        if not self._ensure_contract():
            return None

        gas_limit = gas_limit or DEFAULT_GAS_LIMIT

        try:
            with open(self.metadata_path) as f:
                metadata = json.load(f)

            selector = None
            method_spec = None
            for msg in metadata.get('spec', {}).get('messages', []):
                if msg['label'] == method_name:
                    selector = bytes.fromhex(msg['selector'].replace('0x', ''))
                    method_spec = msg
                    break

            if not selector:
                bt.logging.error(f'Method {method_name} not found in contract metadata')
                return None

            encoded_args = self._encode_args(method_spec, args, metadata)
            call_data = selector + encoded_args

            call = self.subtensor.substrate.compose_call(
                call_module='Contracts',
                call_function='call',
                call_params={
                    'dest': {'Id': self.contract_address},
                    'value': value,
                    'gas_limit': gas_limit,
                    'storage_deposit_limit': None,
                    'data': '0x' + call_data.hex(),
                }
            )

            signer_address = keypair.ss58_address
            account_info = self.subtensor.substrate.query('System', 'Account', [signer_address])
            if hasattr(account_info, 'value'):
                account_data = account_info.value
            else:
                account_data = account_info
            free_balance = account_data.get('data', {}).get('free', 0)
            if free_balance < 1_000_000_000:
                bt.logging.error(f'{method_name}: insufficient balance for fees')
                return None

            extrinsic = self.subtensor.substrate.create_signed_extrinsic(
                call=call,
                keypair=keypair,
            )

            result = self.subtensor.substrate.submit_extrinsic(
                extrinsic,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )

            _extrinsic_not_found_types = tuple(
                t for t in [ExtrinsicNotFound, AsyncExtrinsicNotFound] if t is not None
            )
            try:
                if result.is_success:
                    return result.extrinsic_hash
                else:
                    bt.logging.error(f'{method_name} failed: {result.error_message}')
                    return None
            except _extrinsic_not_found_types:
                return result.extrinsic_hash

        except Exception as e:
            bt.logging.error(f'{method_name} error: {e}')
            return None

    def _encode_args(self, method_spec: dict, args: dict, metadata: dict) -> bytes:
        """SCALE-encode method arguments for Ink! 5 contracts."""
        encoded = b''

        for arg_spec in method_spec.get('args', []):
            arg_name = arg_spec['label']
            arg_type_id = arg_spec['type']['type']

            if arg_name not in args:
                raise ValueError(f'Missing argument: {arg_name}')

            value = args[arg_name]
            type_def = self._get_type_def(arg_type_id, metadata)

            if type_def == 'u32':
                encoded += struct.pack('<I', value)
            elif type_def == 'u64':
                encoded += struct.pack('<Q', value)
            elif type_def == 'u128':
                encoded += struct.pack('<QQ', value & 0xFFFFFFFFFFFFFFFF, value >> 64)
            elif type_def == 'AccountId':
                if isinstance(value, str):
                    encoded += bytes.fromhex(self.subtensor.substrate.ss58_decode(value))
                elif isinstance(value, (list, bytes)):
                    encoded += bytes(value) if isinstance(value, list) else value
                else:
                    raise ValueError(f'Unknown AccountId format: {type(value)}')
            elif type_def == 'array32':
                if isinstance(value, bytes):
                    if len(value) != 32:
                        raise ValueError(f'Array must be 32 bytes')
                    encoded += value
                elif isinstance(value, list):
                    if len(value) != 32:
                        raise ValueError(f'Array must be 32 bytes')
                    encoded += bytes(value)
                else:
                    raise ValueError(f'Unknown array format: {type(value)}')
            else:
                raise ValueError(f'Unsupported type: {type_def} for arg {arg_name}')

        return encoded

    def _get_type_def(self, type_id: int, metadata: dict) -> str:
        """Get type definition string from metadata."""
        types = metadata.get('types', [])
        for t in types:
            if t.get('id') == type_id:
                type_def = t.get('type', {}).get('def', {})
                path = t.get('type', {}).get('path', [])
                if 'primitive' in type_def:
                    return type_def['primitive']
                if 'array' in type_def:
                    array_def = type_def['array']
                    if array_def.get('len') == 32:
                        return 'array32'
                    return 'array'
                if 'composite' in type_def:
                    if path and 'AccountId' in path[-1]:
                        return 'AccountId'
                    return 'composite'
        return 'unknown'

    # =========================================================================
    # Emission Harvesting Functions
    # =========================================================================

    def get_treasury_stake(self) -> int:
        """Query total stake on treasury hotkey."""
        if not self._ensure_contract():
            return 0

        try:
            value = self._read_contract_u128('get_treasury_stake')
            return value
        except Exception as e:
            bt.logging.error(f'Error fetching treasury stake: {e}')
            return 0

    def get_last_harvest_block(self) -> int:
        """Query the block number of the last harvest."""
        if not self._ensure_contract():
            return 0

        try:
            value = self._read_contract_u32('get_last_harvest_block')
            return value
        except Exception as e:
            bt.logging.error(f'Error fetching last harvest block: {e}')
            return 0

    def harvest_emissions(self, wallet: bt.Wallet) -> Optional[dict]:
        """Harvest emissions from the treasury hotkey and distribute to bounties."""
        if not self._ensure_contract():
            return None

        try:
            keypair = wallet.hotkey
            tx_hash = self._exec_contract_raw(
                method_name='harvest_emissions',
                args={},
                keypair=keypair,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if tx_hash:
                return {'status': 'success', 'tx_hash': tx_hash}
            else:
                return {'status': 'failed', 'error': 'Transaction failed'}

        except Exception as e:
            bt.logging.error(f'Harvest error: {e}')
            return {'status': 'error', 'error': str(e)}

    def payout_bounty(
        self,
        issue_id: int,
        solver_coldkey: str,
        wallet: bt.Wallet,
    ) -> Optional[int]:
        """Pay out a completed bounty to the solver"""
        if not self._ensure_contract():
            return None

        try:
            issue = self.get_issue(issue_id)
            expected_payout = issue.bounty_amount if issue else None

            bt.logging.info(f'Paying out bounty for issue {issue_id}')

            keypair = wallet.hotkey
            tx_hash = self._exec_contract_raw(
                method_name='payout_bounty',
                args={
                    'issue_id': issue_id,
                    'solver_coldkey': solver_coldkey,
                },
                keypair=keypair,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if tx_hash:
                return int(expected_payout) if expected_payout else 0
            else:
                return None

        except Exception as e:
            bt.logging.error(f'Error paying out bounty: {e}')
            return None
