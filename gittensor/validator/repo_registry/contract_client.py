# The MIT License (MIT)
# Copyright 2025 Entrius

"""Client for the repos-v0 repository registry smart contract.

Reads go through childstate storage only (no chain extensions, no dry-runs)
and accept an `at` block hash so every read pins to a snapshot. Mutations go
through raw Contracts::call extrinsics, mirroring IssueCompetitionContractClient.
"""

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bittensor as bt
from async_substrate_interface.errors import ExtrinsicNotFound

from gittensor.validator.issue_competitions.storage_utils import (
    compute_ink5_lazy_key,
    get_contract_child_storage_key,
)
from gittensor.validator.repo_registry import price
from gittensor.validator.repo_registry.storage_utils import (
    BASKETS_ROOT_KEY,
    BRANCH_PATTERNS_ROOT_KEY,
    LABEL_MULTS_ROOT_KEY,
    PARAM_BOUNDS_ROOT_KEY,
    PARAMS_ROOT_KEY,
    REPOS_ROOT_KEY,
    WEIGHT_SUM,
    PackedRegistryStorage,
    ParamBounds,
    decode_basket_entries,
    decode_bounds,
    decode_label_entries,
    decode_param_entries,
    decode_repo,
    decode_string_vec,
    encode_compact_length,
    read_child_storage_bytes,
    read_packed_registry_storage,
)

DEFAULT_GAS_LIMIT = {
    'ref_time': 10_000_000_000,
    'proof_size': 500_000,
}

REGISTER_GAS_LIMIT = {
    'ref_time': 10_000_000_000,
    'proof_size': 1_000_000,
}

# Load contract metadata from JSON (selectors and arg types)
# Regenerate with: python gittensor/validator/repo_registry/update_metadata.py
_METADATA_PATH = Path(__file__).parent / 'metadata.json'


def load_contract_metadata() -> Tuple[Dict[str, bytes], Dict[str, List]]:
    """Load selectors and arg types from metadata.json."""
    with open(_METADATA_PATH) as f:
        data = json.load(f)

    selectors = {name: bytes.fromhex(sel) for name, sel in data['selectors'].items()}
    arg_types = {name: [tuple(arg) for arg in args] for name, args in data['arg_types'].items()}

    return selectors, arg_types


CONTRACT_SELECTORS, CONTRACT_ARG_TYPES = load_contract_metadata()


@dataclass
class ContractRepo:
    """Repository record from the registry contract."""

    github_id: int
    full_name: str
    owner: str
    reg_block: int
    active: bool


def validate_basket_entries(entries: List[Tuple[int, int]]) -> None:
    """Validate chain-independent basket invariants; raises ValueError."""
    if not entries:
        raise ValueError('Basket must not be empty')
    seen = set()
    total = 0
    for github_id, weight in entries:
        if weight == 0:
            raise ValueError(f'Zero weight for repo {github_id}')
        if github_id in seen:
            raise ValueError(f'Duplicate basket entry for repo {github_id}')
        seen.add(github_id)
        total += weight
    if total != WEIGHT_SUM:
        raise ValueError(f'Basket weights must sum to {WEIGHT_SUM}, got {total}')


class RepoRegistryContractClient:
    """Client for the repos-v0 registry contract (childstate reads + raw tx writes)."""

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

        try:
            contract_info = self.subtensor.substrate.query('Contracts', 'ContractInfoOf', [self.contract_address])
            if not contract_info or (hasattr(contract_info, 'value') and not contract_info.value):
                raise ValueError(
                    f'No contract found at {self.contract_address}. '
                    'Verify the address and that the contract is deployed.'
                )
        except ValueError:
            raise
        except Exception as e:
            bt.logging.warning(f'Could not verify contract at {self.contract_address}: {e}')

        bt.logging.debug(f'Repo registry client initialized: {self.contract_address}')

    # =========================================================================
    # Query Functions (childstate storage, pinnable via `at` block hash)
    # =========================================================================

    def _get_child_storage_key(self) -> Optional[str]:
        """Get the child storage key for the contract's trie (stable for the contract's lifetime)."""
        try:
            return get_contract_child_storage_key(self.subtensor.substrate, self.contract_address)
        except Exception as e:
            bt.logging.debug(f'Error getting child storage key: {e}')
            return None

    def _read_mapping(self, root_key_hex: str, encoded_key: bytes, at: Optional[str]) -> Optional[bytes]:
        """Read one lazy-mapping cell from contract child storage."""
        child_key = self._get_child_storage_key()
        if not child_key:
            return None
        lazy_key = compute_ink5_lazy_key(root_key_hex, encoded_key)
        try:
            return read_child_storage_bytes(self.subtensor.substrate, child_key, lazy_key, at)
        except Exception as e:
            bt.logging.debug(f'Error reading mapping cell {lazy_key}: {e}')
            return None

    def get_registry(self, at: Optional[str] = None) -> Optional[PackedRegistryStorage]:
        """Read the packed root cell (config, repo_ids, voters, bound_keys)."""
        try:
            return read_packed_registry_storage(self.subtensor.substrate, self.contract_address, at)
        except Exception as e:
            bt.logging.debug(f'Error reading packed registry storage: {e}')
            return None

    def get_repo(self, github_id: int, at: Optional[str] = None) -> Optional[ContractRepo]:
        """Read a single repository record."""
        data = self._read_mapping(REPOS_ROOT_KEY, struct.pack('<Q', github_id), at)
        if not data:
            return None
        decoded = decode_repo(data)
        if decoded is None:
            return None
        return ContractRepo(
            github_id=decoded.github_id,
            full_name=decoded.full_name,
            owner=self.subtensor.substrate.ss58_encode(decoded.owner.hex()),
            reg_block=decoded.reg_block,
            active=decoded.active,
        )

    def get_all_repos(self, at: Optional[str] = None) -> List[ContractRepo]:
        """Read all active repositories (repo_ids walk; inactive entries filtered)."""
        packed = self.get_registry(at)
        if not packed:
            return []
        repos = []
        for github_id in packed.repo_ids:
            repo = self.get_repo(github_id, at)
            if repo and repo.active:
                repos.append(repo)
        return repos

    def get_params(self, github_id: int, at: Optional[str] = None) -> Dict[int, int]:
        """Read a repo's hyperparam entries as {key: value}."""
        data = self._read_mapping(PARAMS_ROOT_KEY, struct.pack('<Q', github_id), at)
        if not data:
            return {}
        entries = decode_param_entries(data)
        return dict(entries) if entries else {}

    def get_bounds(self, key: int, at: Optional[str] = None) -> Optional[ParamBounds]:
        """Read the bounds for one param key."""
        data = self._read_mapping(PARAM_BOUNDS_ROOT_KEY, struct.pack('<B', key), at)
        if not data:
            return None
        return decode_bounds(data)

    def get_all_bounds(self, at: Optional[str] = None) -> Dict[int, ParamBounds]:
        """Read the full bounds table as {key: ParamBounds} (bound_keys walk)."""
        packed = self.get_registry(at)
        if not packed:
            return {}
        bounds = {}
        for key in packed.bound_keys:
            entry = self.get_bounds(key, at)
            if entry:
                bounds[key] = entry
        return bounds

    def get_label_multipliers(self, github_id: int, at: Optional[str] = None) -> Dict[str, int]:
        """Read a repo's label multipliers as {label: value}."""
        data = self._read_mapping(LABEL_MULTS_ROOT_KEY, struct.pack('<Q', github_id), at)
        if not data:
            return {}
        entries = decode_label_entries(data)
        return dict(entries) if entries else {}

    def get_branch_patterns(self, github_id: int, at: Optional[str] = None) -> List[str]:
        """Read a repo's additional acceptable branch patterns."""
        data = self._read_mapping(BRANCH_PATTERNS_ROOT_KEY, struct.pack('<Q', github_id), at)
        if not data:
            return []
        return decode_string_vec(data) or []

    def get_basket(self, hotkey: str, at: Optional[str] = None) -> Optional[List[Tuple[int, int]]]:
        """Read one validator's basket as [(github_id, weight)]."""
        account = bytes.fromhex(self.subtensor.substrate.ss58_decode(hotkey))
        data = self._read_mapping(BASKETS_ROOT_KEY, account, at)
        if not data:
            return None
        return decode_basket_entries(data)

    def get_all_baskets(self, at: Optional[str] = None) -> Dict[str, List[Tuple[int, int]]]:
        """Read all whitelisted voters' baskets as {hotkey_ss58: entries} (voters walk)."""
        packed = self.get_registry(at)
        if not packed:
            return {}
        baskets = {}
        for voter in packed.voters:
            data = self._read_mapping(BASKETS_ROOT_KEY, voter, at)
            if not data:
                continue
            entries = decode_basket_entries(data)
            if entries:
                baskets[self.subtensor.substrate.ss58_encode(voter.hex())] = entries
        return baskets

    def get_voters(self, at: Optional[str] = None) -> List[str]:
        """Read the whitelisted voter hotkeys as SS58 addresses."""
        packed = self.get_registry(at)
        if not packed:
            return []
        return [self.subtensor.substrate.ss58_encode(voter.hex()) for voter in packed.voters]

    def quote_price(self, at: Optional[str] = None) -> Optional[int]:
        """Registration price quote via the python price replica (no dry-run)."""
        packed = self.get_registry(at)
        if not packed:
            return None
        try:
            now = self.subtensor.substrate.get_block_number(at) if at else self.subtensor.block
        except Exception as e:
            bt.logging.debug(f'Error resolving block number for price quote: {e}')
            return None
        constants = packed.constants
        return price.lazy_price(
            packed.price_last,
            max(0, now - packed.price_last_block),
            constants.price_half_life,
            constants.price_floor,
            constants.price_ceiling,
        )

    # =========================================================================
    # Transaction Functions (Write)
    # =========================================================================

    def register(
        self,
        github_id: int,
        full_name: str,
        fee_hotkey: str,
        wallet: bt.Wallet,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Register a repository, paying the dynamic alpha fee from the caller's
        stake on `fee_hotkey`. Signed by the registrant coldkey.

        Returns:
            (hash, error). Revert: both set. Pre-submission failure: hash is None.
        """
        return self._exec_contract_raw(
            method_name='register',
            args={'github_id': github_id, 'full_name': full_name, 'fee_hotkey': fee_hotkey},
            keypair=wallet.coldkey,
            gas_limit=REGISTER_GAS_LIMIT,
        )

    def _owner_tx(self, method_name: str, args: dict, wallet: bt.Wallet) -> Tuple[Optional[str], Optional[str]]:
        """Execute a repo-owner message signed by the coldkey. Returns (hash, error)."""
        return self._exec_contract_raw(
            method_name=method_name,
            args=args,
            keypair=wallet.coldkey,
            gas_limit=DEFAULT_GAS_LIMIT,
        )

    def set_param(self, github_id: int, key: int, value: int, wallet: bt.Wallet) -> Tuple[Optional[str], Optional[str]]:
        """Set one hyperparam (repo owner coldkey signs)."""
        return self._owner_tx('set_param', {'github_id': github_id, 'key': key, 'value': value}, wallet)

    def set_label_multiplier(
        self, github_id: int, label: str, value: int, wallet: bt.Wallet
    ) -> Tuple[Optional[str], Optional[str]]:
        """Set or update a label multiplier (repo owner coldkey signs)."""
        return self._owner_tx('set_label_multiplier', {'github_id': github_id, 'label': label, 'value': value}, wallet)

    def remove_label_multiplier(
        self, github_id: int, label: str, wallet: bt.Wallet
    ) -> Tuple[Optional[str], Optional[str]]:
        """Remove a label multiplier (repo owner coldkey signs)."""
        return self._owner_tx('remove_label_multiplier', {'github_id': github_id, 'label': label}, wallet)

    def set_branch_patterns(
        self, github_id: int, patterns: List[str], wallet: bt.Wallet
    ) -> Tuple[Optional[str], Optional[str]]:
        """Replace the branch pattern list; empty clears (repo owner coldkey signs)."""
        return self._owner_tx('set_branch_patterns', {'github_id': github_id, 'patterns': patterns}, wallet)

    def update_full_name(
        self, github_id: int, full_name: str, wallet: bt.Wallet
    ) -> Tuple[Optional[str], Optional[str]]:
        """Follow a GitHub rename (repo owner coldkey signs, rate-limited)."""
        return self._owner_tx('update_full_name', {'github_id': github_id, 'full_name': full_name}, wallet)

    def transfer_ownership(
        self, github_id: int, new_owner: str, wallet: bt.Wallet
    ) -> Tuple[Optional[str], Optional[str]]:
        """Transfer repo ownership (repo owner coldkey signs)."""
        return self._owner_tx('transfer_ownership', {'github_id': github_id, 'new_owner': new_owner}, wallet)

    def deregister(self, github_id: int, wallet: bt.Wallet) -> Tuple[Optional[str], Optional[str]]:
        """Deregister a repo, freeing its slot with no refund (repo owner coldkey signs)."""
        return self._owner_tx('deregister', {'github_id': github_id}, wallet)

    def set_basket(self, entries: List[Tuple[int, int]], wallet: bt.Wallet) -> bool:
        """Publish a validator basket (whitelisted hotkey signs).

        Raises:
            ValueError: If entries violate chain-independent basket invariants.
        """
        validate_basket_entries(entries)
        return self._exec_tx_bool(
            method_name='set_basket',
            args={'entries': entries},
            keypair=wallet.hotkey,
            label=f'Setting basket ({len(entries)} entries)',
            gas_limit=DEFAULT_GAS_LIMIT,
        )

    def clear_basket(self, wallet: bt.Wallet) -> bool:
        """Clear the caller's basket (hotkey signs)."""
        return self._exec_tx_bool(
            method_name='clear_basket',
            args={},
            keypair=wallet.hotkey,
            label='Clearing basket',
            gas_limit=DEFAULT_GAS_LIMIT,
        )

    # =========================================================================
    # Raw Extrinsic Execution (Ink! 5 Workaround)
    # =========================================================================

    def _exec_tx_bool(
        self,
        method_name: str,
        args: dict,
        keypair,
        label: str,
        gas_limit: dict = None,  # type: ignore[assignment]
    ) -> bool:
        """Execute a contract transaction and return True on success."""
        try:
            bt.logging.info(label)
            tx_hash, error = self._exec_contract_raw(
                method_name=method_name,
                args=args,
                keypair=keypair,
                gas_limit=gas_limit,
            )
            if tx_hash and not error:
                bt.logging.info(f'{label} — ok ({tx_hash})')
                return True
            bt.logging.error(f'{label} — failed')
            return False
        except Exception as e:
            bt.logging.error(f'{label} — {e}')
            return False

    def _exec_contract_raw(
        self,
        method_name: str,
        args: dict,
        keypair,
        gas_limit: dict = None,  # type: ignore[assignment]
        value: int = 0,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Execute a contract method via raw extrinsic. Returns (hash, error).

        Revert: both set. Pre-submission failure: hash is None.
        """
        gas_limit = gas_limit or DEFAULT_GAS_LIMIT

        try:
            selector = CONTRACT_SELECTORS.get(method_name)
            if not selector:
                err = f'Method {method_name} not found in CONTRACT_SELECTORS'
                bt.logging.error(err)
                return None, err

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
                account_data = account_info.value  # type: ignore[union-attr]
            else:
                account_data = account_info
            free_balance = account_data.get('data', {}).get('free', 0)  # type: ignore[union-attr]
            if free_balance < 100_000_000:
                err = f'{method_name}: insufficient balance for fees'
                bt.logging.error(err)
                return None, err

            extrinsic = self.subtensor.substrate.create_signed_extrinsic(
                call=call,
                keypair=keypair,
            )

            result = self.subtensor.substrate.submit_extrinsic(
                extrinsic,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )

            try:
                if result.is_success:
                    return result.extrinsic_hash, None
                err = f'{method_name} failed: {result.error_message}'
                bt.logging.error(err)
                return result.extrinsic_hash, err
            except ExtrinsicNotFound:
                return result.extrinsic_hash, None

        except Exception as e:
            err = f'{method_name} error: {e}'
            bt.logging.error(err)
            return None, err

    def _encode_args(self, method_name: str, args: dict) -> bytes:
        """SCALE-encode method arguments using metadata type definitions."""
        arg_types = CONTRACT_ARG_TYPES.get(method_name, [])
        encoded = b''

        for arg_name, type_def in arg_types:
            if arg_name not in args:
                raise ValueError(f'Missing argument: {arg_name}')

            value = args[arg_name]

            if type_def == 'u8':
                encoded += struct.pack('<B', value)
            elif type_def == 'u16':
                encoded += struct.pack('<H', value)
            elif type_def == 'u32':
                encoded += struct.pack('<I', value)
            elif type_def == 'u64':
                encoded += struct.pack('<Q', value)
            elif type_def == 'u128':
                encoded += struct.pack('<QQ', value & 0xFFFFFFFFFFFFFFFF, value >> 64)
            elif type_def == 'str':
                if not isinstance(value, str):
                    raise ValueError(f'Expected str for {arg_name}, got {type(value).__name__}')
                data = value.encode('utf-8')
                encoded += encode_compact_length(len(data)) + data
            elif type_def == 'AccountId':
                if isinstance(value, str):
                    encoded += bytes.fromhex(self.subtensor.substrate.ss58_decode(value))
                elif isinstance(value, (list, bytes)):
                    encoded += bytes(value) if isinstance(value, list) else value
                else:
                    raise ValueError(f'Unknown AccountId format: {type(value)}')
            elif type_def == 'vec_u64_u16':
                encoded += encode_compact_length(len(value))
                for item_u64, item_u16 in value:
                    encoded += struct.pack('<QH', item_u64, item_u16)
            elif type_def == 'vec_str':
                encoded += encode_compact_length(len(value))
                for item in value:
                    data = item.encode('utf-8')
                    encoded += encode_compact_length(len(data)) + data
            else:
                raise ValueError(f'Unsupported type: {type_def} for arg {arg_name}')

        return encoded
