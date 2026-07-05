"""Shared helpers for reading and decoding issue competition contract storage."""

import hashlib
import logging
import struct
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ink! mapping selector for the issues storage map (matches the contract's storage layout).
ISSUES_MAPPING_ROOT_KEY = '52789899'


def _decode_u128le(data: bytes, offset: int) -> int:
    """Decode a little-endian u128 (two u64 limbs) from ``data`` at ``offset``."""
    lo, hi = struct.unpack_from('<QQ', data, offset)
    return lo + (hi << 64)


@dataclass
class PackedContractStorage:
    """Decoded root packed storage layout for the issue competition contract."""

    owner: bytes
    treasury_hotkey: bytes
    netuid: int
    next_issue_id: int
    alpha_pool: int


@dataclass
class DecodedIssueStorage:
    """Decoded issue mapping entry layout."""

    id: int
    github_url_hash: bytes
    repository_full_name: str
    issue_number: int
    bounty_amount: int
    target_bounty: int
    status_byte: int
    registered_at_block: int


def _extract_trie_id_bytes(contract_info) -> Optional[bytes]:
    """Extract contract trie ID bytes from substrate contract info."""
    info = contract_info.value if hasattr(contract_info, 'value') else contract_info
    if not info or 'trie_id' not in info:
        return None

    trie_id = info['trie_id']  # type: ignore[call-overload]
    if isinstance(trie_id, str):
        return bytes.fromhex(trie_id.replace('0x', ''))
    if isinstance(trie_id, (tuple, list)):
        if len(trie_id) == 1 and isinstance(trie_id[0], (tuple, list)):
            trie_id = trie_id[0]
        return bytes(trie_id)
    if isinstance(trie_id, bytes):
        return trie_id
    return None


def get_contract_child_storage_key(substrate, contract_addr: str) -> Optional[str]:
    """Build the child storage key prefix for a deployed contract."""
    contract_info = substrate.query('Contracts', 'ContractInfoOf', [contract_addr])
    if not contract_info:
        return None

    trie_id_bytes = _extract_trie_id_bytes(contract_info)
    if trie_id_bytes is None:
        return None

    prefix = b':child_storage:default:'
    return '0x' + (prefix + trie_id_bytes).hex()


def compute_ink5_lazy_key(root_key_hex: str, encoded_key: bytes) -> str:
    """Compute Ink! 5 mapping key using blake2_128concat."""
    root_key = bytes.fromhex(root_key_hex.replace('0x', ''))
    data = root_key + encoded_key
    h = hashlib.blake2b(data, digest_size=16).digest()
    return '0x' + (h + data).hex()


_PACKED_ROOT_STORAGE_KEY = compute_ink5_lazy_key('00000000', b'')

# owner (32) + treasury hotkey (32) + netuid (2) + next_issue_id (8) + alpha_pool (16)
_PACKED_CONTRACT_STORAGE_SIZE = 32 + 32 + 2 + 8 + 16


def read_contract_packed_storage_bytes(substrate, child_key: str) -> Optional[bytes]:
    """Read packed root storage bytes from contract child storage."""
    val_result = substrate.rpc_request('childstate_getStorage', [child_key, _PACKED_ROOT_STORAGE_KEY, None])
    raw_hex = val_result.get('result')
    if not raw_hex:
        return None
    return bytes.fromhex(raw_hex.replace('0x', ''))


def decode_packed_contract_storage(data: bytes) -> Optional[PackedContractStorage]:
    """Decode packed root storage bytes into typed fields."""
    if len(data) < _PACKED_CONTRACT_STORAGE_SIZE:
        return None

    try:
        offset = 0
        owner = data[offset : offset + 32]
        offset += 32
        treasury_hotkey = data[offset : offset + 32]
        offset += 32
        netuid = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        next_issue_id = struct.unpack_from('<Q', data, offset)[0]
        offset += 8
        alpha_pool = _decode_u128le(data, offset)
    except (struct.error, IndexError) as e:
        logger.debug('Failed to decode packed contract storage: %s', e)
        return None

    return PackedContractStorage(
        owner=owner,
        treasury_hotkey=treasury_hotkey,
        netuid=netuid,
        next_issue_id=next_issue_id,
        alpha_pool=alpha_pool,
    )


def read_contract_packed_storage(substrate, contract_addr: str) -> Optional[PackedContractStorage]:
    """Read and decode packed root storage for a contract."""
    child_key = get_contract_child_storage_key(substrate, contract_addr)
    if not child_key:
        return None

    packed_bytes = read_contract_packed_storage_bytes(substrate, child_key)
    if not packed_bytes:
        return None

    return decode_packed_contract_storage(packed_bytes)


def decode_issue_from_storage(data: bytes) -> Optional[DecodedIssueStorage]:
    """Decode one issue mapping value from contract child storage."""
    try:
        offset = 0
        stored_issue_id = struct.unpack_from('<Q', data, offset)[0]
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

        bounty_amount = _decode_u128le(data, offset)
        offset += 16

        target_bounty = _decode_u128le(data, offset)
        offset += 16

        status_byte = data[offset]
        offset += 1

        registered_at_block = struct.unpack_from('<I', data, offset)[0]

        return DecodedIssueStorage(
            id=stored_issue_id,
            github_url_hash=github_url_hash,
            repository_full_name=repo_name,
            issue_number=issue_number,
            bounty_amount=int(bounty_amount),
            target_bounty=int(target_bounty),
            status_byte=status_byte,
            registered_at_block=registered_at_block,
        )
    except (IndexError, struct.error, ValueError) as e:
        logger.debug('Failed to decode issue storage entry: %s', e)
        return None
