# The MIT License (MIT)
# Copyright 2025 Entrius

"""Client for interacting with the Issue Bounty smart contract"""

import hashlib
import json
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bittensor as bt
from substrateinterface import Keypair
from substrateinterface.exceptions import ExtrinsicNotFound

# Bittensor uses async_substrate_interface which has its own exception type
try:
    from async_substrate_interface.errors import ExtrinsicNotFound as AsyncExtrinsicNotFound
except ImportError:
    AsyncExtrinsicNotFound = ExtrinsicNotFound

# Default gas limits for contract calls
DEFAULT_GAS_LIMIT = {
    'ref_time': 10_000_000_000,
    'proof_size': 500_000,
}

# Load contract metadata from JSON (selectors and arg types)
# Regenerate with: python gittensor/validator/issue_competitions/update_metadata.py
_METADATA_PATH = Path(__file__).parent / 'metadata.json'


def load_contract_metadata() -> Tuple[Dict[str, bytes], Dict[str, List]]:
    """Load selectors and arg types from metadata.json."""
    with open(_METADATA_PATH) as f:
        data = json.load(f)

    selectors = {name: bytes.fromhex(sel) for name, sel in data['selectors'].items()}
    arg_types = {name: [tuple(arg) for arg in args] for name, args in data['arg_types'].items()}

    return selectors, arg_types


CONTRACT_SELECTORS, CONTRACT_ARG_TYPES = load_contract_metadata()


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
        contract_address: str,
        subtensor: bt.Subtensor,
    ):
        """Initialize the contract client.

        Args:
            contract_address: SS58 address of the deployed contract.
            subtensor: Connected Subtensor instance.

        Raises:
            ValueError: If contract_address is empty or contract not found on-chain.
        """
        if not contract_address:
            raise ValueError('contract_address is required')

        self.contract_address = contract_address
        self.subtensor = subtensor

        # Validate the contract exists on-chain
        try:
            contract_info = self.subtensor.substrate.query(
                'Contracts', 'ContractInfoOf', [self.contract_address]
            )
            if not contract_info or (hasattr(contract_info, 'value') and not contract_info.value):
                raise ValueError(
                    f'No contract found at {self.contract_address}. '
                    'Verify the address and that the contract is deployed.'
                )
        except ValueError:
            raise
        except Exception as e:
            bt.logging.warning(f'Could not verify contract at {self.contract_address}: {e}')

        bt.logging.debug(f'Contract client initialized: {self.contract_address}')

    @staticmethod
    def hash_url(url: str) -> bytes:
        """Hash a URL for deduplication."""
        return hashlib.sha256(url.encode()).digest()

    # =========================================================================
    # Query Functions (Read-only)
    # =========================================================================

    def _get_child_storage_key(self) -> Optional[str]:
        """Get the child storage key for the contract's trie."""
        try:
            contract_info = self.subtensor.substrate.query('Contracts', 'ContractInfoOf', [self.contract_address])
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

    def compute_ink5_lazy_key(self, root_key_hex: str, encoded_key: bytes) -> str:
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

            val_result = self.subtensor.substrate.rpc_request('childstate_getStorage', [child_key, packed_key, None])
            if not val_result.get('result'):
                return None

            data = bytes.fromhex(val_result['result'].replace('0x', ''))

            # owner (32) + treasury (32) + netuid (2) + next_issue_id (8)
            if len(data) < 74:
                return None

            offset = 64  # Skip owner + treasury
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

    def read_issue_from_child_storage(self, issue_id: int) -> Optional[ContractIssue]:
        """Read a single issue from contract child storage."""
        child_key = self._get_child_storage_key()
        if not child_key:
            return None

        try:
            encoded_id = struct.pack('<Q', issue_id)
            lazy_key = self.compute_ink5_lazy_key('52789899', encoded_id)

            val_result = self.subtensor.substrate.rpc_request('childstate_getStorage', [child_key, lazy_key, None])
            if not val_result.get('result'):
                return None

            data = bytes.fromhex(val_result['result'].replace('0x', ''))

            offset = 0
            stored_id = struct.unpack_from('<Q', data, offset)[0]
            offset += 8

            github_url_hash = data[offset : offset + 32]
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
                issue = self.read_issue_from_child_storage(issue_id)
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
        try:
            return self.read_issue_from_child_storage(issue_id)
        except Exception as e:
            bt.logging.error(f'Error fetching issue {issue_id}: {e}')
            return None

    def get_alpha_pool(self) -> int:
        """Get the current alpha pool balance."""
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
        try:
            selector = CONTRACT_SELECTORS.get(method_name)
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

            result = self.subtensor.substrate.rpc_request('state_call', ['ContractsApi_call', '0x' + call_params.hex()])

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
        pr_number: int,
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
            pr_number: PR number that solved the issue (combined with repo for URL)
            wallet: Validator wallet for signing

        Returns:
            True if vote succeeded
        """
        try:
            bt.logging.info(f'Voting solution for issue {issue_id}: solver={solver_hotkey[:8]}... PR#{pr_number}')

            keypair = wallet.hotkey
            tx_hash = self._exec_contract_raw(
                method_name='vote_solution',
                args={
                    'issue_id': issue_id,
                    'solver_hotkey': solver_hotkey,
                    'solver_coldkey': solver_coldkey,
                    'pr_number': pr_number,
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
        gas_limit = gas_limit or DEFAULT_GAS_LIMIT

        try:
            selector = CONTRACT_SELECTORS.get(method_name)
            if not selector:
                bt.logging.error(f'Method {method_name} not found in CONTRACT_SELECTORS')
                return None

            encoded_args = self._encode_args(method_name, args)
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
                },
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

            _extrinsic_not_found_types = tuple(t for t in [ExtrinsicNotFound, AsyncExtrinsicNotFound] if t is not None)
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

    def _encode_args(self, method_name: str, args: dict) -> bytes:
        """SCALE-encode method arguments using hardcoded type definitions."""
        arg_types = CONTRACT_ARG_TYPES.get(method_name, [])
        encoded = b''

        for arg_name, type_def in arg_types:
            if arg_name not in args:
                raise ValueError(f'Missing argument: {arg_name}')

            value = args[arg_name]

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
                        raise ValueError('Array must be 32 bytes')
                    encoded += value
                elif isinstance(value, list):
                    if len(value) != 32:
                        raise ValueError('Array must be 32 bytes')
                    encoded += bytes(value)
                else:
                    raise ValueError(f'Unknown array format: {type(value)}')
            else:
                raise ValueError(f'Unsupported type: {type_def} for arg {arg_name}')

        return encoded

    # =========================================================================
    # Emission Harvesting Functions
    # =========================================================================

    def get_treasury_stake(self) -> int:
        """
        Query total stake on treasury hotkey owned by the contract.

        NOTE: Chain extensions don't work in dry-run mode (state_call), so we
        query the Subtensor Alpha storage directly instead of using the
        contract's get_treasury_stake() method.

        Returns:
            Total stake amount (0 if no stake found)
        """
        try:
            # Read contract's packed storage to get treasury_hotkey, owner, and netuid
            child_key = self._get_child_storage_key()
            if not child_key:
                bt.logging.debug('Cannot get treasury stake: no child storage key')
                return 0

            # Get packed storage key (ends with 00000000)
            keys_result = self.subtensor.substrate.rpc_request(
                'childstate_getKeysPaged', [child_key, '0x', 10, None, None]
            )
            keys = keys_result.get('result', [])
            packed_key = next((k for k in keys if k.endswith('00000000')), None)
            if not packed_key:
                bt.logging.debug('Cannot get treasury stake: no packed storage key')
                return 0

            # Read packed storage
            val_result = self.subtensor.substrate.rpc_request('childstate_getStorage', [child_key, packed_key, None])
            if not val_result.get('result'):
                bt.logging.debug('Cannot get treasury stake: no packed storage value')
                return 0

            data = bytes.fromhex(val_result['result'].replace('0x', ''))
            if len(data) < 74:  # Need at least owner(32) + treasury(32) + netuid(2) + next_id(8)
                bt.logging.debug('Cannot get treasury stake: packed storage too small')
                return 0

            # Extract owner (coldkey), treasury_hotkey, and netuid from packed storage
            # Layout: owner(32) + treasury(32) + netuid(2) + next_issue_id(8) + alpha_pool(16)
            owner = data[0:32]
            treasury_hotkey = data[32:64]
            netuid = struct.unpack_from('<H', data, 64)[0]

            # Convert to SS58 addresses
            owner_ss58 = self.subtensor.substrate.ss58_encode(owner.hex())
            treasury_ss58 = self.subtensor.substrate.ss58_encode(treasury_hotkey.hex())

            # Query SubtensorModule::Alpha directly
            # Alpha storage: (hotkey, coldkey, netuid) -> U64F64 stake amount
            alpha_result = self.subtensor.substrate.query(
                'SubtensorModule', 'Alpha', [treasury_ss58, owner_ss58, netuid]
            )

            if not alpha_result:
                bt.logging.debug('No Alpha stake found')
                return 0

            # Alpha returns U64F64 fixed-point: bits field contains raw value
            # Upper 64 bits are integer part (the stake amount in raw units)
            if hasattr(alpha_result, 'value') and alpha_result.value:
                bits = alpha_result.value.get('bits', 0)
            elif isinstance(alpha_result, dict):
                bits = alpha_result.get('bits', 0)
            else:
                bits = 0

            # Extract integer part (upper 64 bits of U64F64)
            stake_raw = bits >> 64 if bits else 0

            bt.logging.debug(f'Treasury stake (direct query): {stake_raw} ({stake_raw / 1e9:.4f} α)')
            return stake_raw

        except Exception as e:
            bt.logging.error(f'Error fetching treasury stake: {e}')
            return 0

    def get_last_harvest_block(self) -> int:
        """Query the block number of the last harvest."""
        try:
            value = self._read_contract_u32('get_last_harvest_block')
            return value
        except Exception as e:
            bt.logging.error(f'Error fetching last harvest block: {e}')
            return 0

    def harvest_emissions(self, wallet: bt.Wallet) -> Optional[dict]:
        """Harvest emissions from the treasury hotkey and distribute to bounties."""
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
        wallet: bt.Wallet,
    ) -> Optional[int]:
        """Pay out a completed bounty to the solver.

        The solver address is determined by validator consensus and stored
        in the contract - no need to pass it here.

        Args:
            issue_id: The ID of the completed issue
            wallet: Owner wallet for signing (uses coldkey)

        Returns:
            Payout amount in raw units, or None on failure
        """
        try:
            issue = self.get_issue(issue_id)
            expected_payout = issue.bounty_amount if issue else None

            bt.logging.info(f'Paying out bounty for issue {issue_id}')

            keypair = wallet.coldkey
            tx_hash = self._exec_contract_raw(
                method_name='payout_bounty',
                args={
                    'issue_id': issue_id,
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

    def cancel_issue(
        self,
        issue_id: int,
        wallet: bt.Wallet,
    ) -> bool:
        """Cancel an issue (owner only).

        Args:
            issue_id: The ID of the issue to cancel
            wallet: Owner wallet for signing (uses coldkey)

        Returns:
            True if cancellation succeeded
        """
        try:
            bt.logging.info(f'Cancelling issue {issue_id}')

            keypair = wallet.coldkey
            tx_hash = self._exec_contract_raw(
                method_name='cancel_issue',
                args={
                    'issue_id': issue_id,
                },
                keypair=keypair,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if tx_hash:
                bt.logging.info(f'Issue {issue_id} cancelled: {tx_hash}')
                return True
            else:
                bt.logging.error(f'Failed to cancel issue {issue_id}')
                return False

        except Exception as e:
            bt.logging.error(f'Error cancelling issue: {e}')
            return False

    def set_owner(
        self,
        new_owner: str,
        wallet: bt.Wallet,
    ) -> bool:
        """Transfer contract ownership (owner only).

        WARNING: This operation is irreversible. The current owner will
        lose all admin privileges.

        Args:
            new_owner: SS58 address of the new owner
            wallet: Current owner wallet for signing (uses coldkey)

        Returns:
            True if ownership transfer succeeded
        """
        try:
            bt.logging.info(f'Transferring ownership to {new_owner}')

            keypair = wallet.coldkey
            tx_hash = self._exec_contract_raw(
                method_name='set_owner',
                args={
                    'new_owner': new_owner,
                },
                keypair=keypair,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if tx_hash:
                bt.logging.info(f'Ownership transferred: {tx_hash}')
                return True
            else:
                bt.logging.error('Failed to transfer ownership')
                return False

        except Exception as e:
            bt.logging.error(f'Error transferring ownership: {e}')
            return False

    def add_validator(
        self,
        hotkey: str,
        wallet: bt.Wallet,
    ) -> bool:
        """Add a validator hotkey to the whitelist (owner only).

        Args:
            hotkey: SS58 address of the validator hotkey to whitelist
            wallet: Owner wallet for signing (uses coldkey)

        Returns:
            True if addition succeeded
        """
        try:
            bt.logging.info(f'Adding validator {hotkey}')

            keypair = wallet.coldkey
            tx_hash = self._exec_contract_raw(
                method_name='add_validator',
                args={
                    'hotkey': hotkey,
                },
                keypair=keypair,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if tx_hash:
                bt.logging.info(f'Validator added: {tx_hash}')
                return True
            else:
                bt.logging.error('Failed to add validator')
                return False

        except Exception as e:
            bt.logging.error(f'Error adding validator: {e}')
            return False

    def remove_validator(
        self,
        hotkey: str,
        wallet: bt.Wallet,
    ) -> bool:
        """Remove a validator hotkey from the whitelist (owner only).

        Args:
            hotkey: SS58 address of the validator hotkey to remove
            wallet: Owner wallet for signing (uses coldkey)

        Returns:
            True if removal succeeded
        """
        try:
            bt.logging.info(f'Removing validator {hotkey}')

            keypair = wallet.coldkey
            tx_hash = self._exec_contract_raw(
                method_name='remove_validator',
                args={
                    'hotkey': hotkey,
                },
                keypair=keypair,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if tx_hash:
                bt.logging.info(f'Validator removed: {tx_hash}')
                return True
            else:
                bt.logging.error('Failed to remove validator')
                return False

        except Exception as e:
            bt.logging.error(f'Error removing validator: {e}')
            return False

    def get_validators(self) -> List[str]:
        """Query the list of whitelisted validator hotkeys.

        Returns:
            List of SS58 addresses, or empty list on error.
        """
        try:
            response = self._raw_contract_read('get_validators')
            if response is None:
                return []

            return self._decode_validator_list(response)
        except Exception as e:
            bt.logging.error(f'Error fetching validators: {e}')
            return []

    def _decode_validator_list(self, response_bytes: bytes) -> List[str]:
        """Decode a SCALE-encoded Vec<AccountId> from contract response.

        The response has an ink! envelope prefix (flags + result bytes),
        then a SCALE compact-encoded length followed by 32-byte AccountIds.
        """
        if not response_bytes or len(response_bytes) < 12:
            return []

        try:
            # Skip ink! response envelope: look for the compact-encoded vec length
            # The envelope is: flags(4) + Ok discriminant(1) + data...
            # But exact offset varies, so we scan for a plausible start.
            # With 0 validators, the compact length byte is 0x00.
            # With N validators, compact length = N << 2 (for N < 64).
            # We know the data payload starts at offset 11 based on other extractors.
            offset = 11

            if offset >= len(response_bytes):
                return []

            # Read SCALE compact length
            first_byte = response_bytes[offset]
            mode = first_byte & 0x03
            if mode == 0:
                count = first_byte >> 2
                offset += 1
            elif mode == 1:
                if offset + 2 > len(response_bytes):
                    return []
                count = (response_bytes[offset] | (response_bytes[offset + 1] << 8)) >> 2
                offset += 2
            else:
                return []

            validators = []
            for _ in range(count):
                if offset + 32 > len(response_bytes):
                    break
                account_bytes = response_bytes[offset:offset + 32]
                ss58 = self.subtensor.substrate.ss58_encode(account_bytes.hex())
                validators.append(ss58)
                offset += 32

            return validators

        except Exception as e:
            bt.logging.debug(f'Error decoding validator list: {e}')
            return []

    def set_treasury_hotkey(
        self,
        new_hotkey: str,
        wallet: bt.Wallet,
    ) -> bool:
        """Change the treasury hotkey (owner only).

        Args:
            new_hotkey: SS58 address of the new treasury hotkey
            wallet: Owner wallet for signing (uses coldkey)

        Returns:
            True if treasury hotkey change succeeded
        """
        try:
            bt.logging.info(f'Setting treasury hotkey to {new_hotkey}')

            keypair = wallet.coldkey
            tx_hash = self._exec_contract_raw(
                method_name='set_treasury_hotkey',
                args={
                    'new_hotkey': new_hotkey,
                },
                keypair=keypair,
                gas_limit=DEFAULT_GAS_LIMIT,
            )

            if tx_hash:
                bt.logging.info(f'Treasury hotkey updated: {tx_hash}')
                return True
            else:
                bt.logging.error('Failed to set treasury hotkey')
                return False

        except Exception as e:
            bt.logging.error(f'Error setting treasury hotkey: {e}')
            return False
