"""Helpers for reading and decoding repos-v0 registry contract storage.

Read path is pure childstate RPC — no chain-extension or dry-run calls — and
every read accepts an `at` block hash so results pin to a snapshot.

ink! 5 AutoKey mapping root keys are `XXH32('RepoRegistry::<field>')` encoded
as SCALE little-endian u32 (ink_primitives KeyComposer). The derivation is
verified against issues-v0's known `IssueBountyManager::issues` -> '52789899'.
Lazy cell keys then follow the issues-v0 blake2_128concat scheme.

The packed root cell layout (root key 0) is the byte-offset table documented
on the RepoRegistry struct in smart-contracts/repos-v0/lib.rs.
"""

import logging
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

from gittensor.validator.issue_competitions.storage_utils import (
    compute_ink5_lazy_key,
    get_contract_child_storage_key,
)

logger = logging.getLogger(__name__)

_XXH_PRIME1 = 2654435761
_XXH_PRIME2 = 2246822519
_XXH_PRIME3 = 3266489917
_XXH_PRIME4 = 668265263
_XXH_PRIME5 = 374761393
_U32 = 0xFFFFFFFF


def _rotl32(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & _U32


def xxh32(data: bytes, seed: int = 0) -> int:
    """Pure-python XXH32 (ink! KeyComposer uses seed 0)."""
    n = len(data)
    i = 0
    if n >= 16:
        v1 = (seed + _XXH_PRIME1 + _XXH_PRIME2) & _U32
        v2 = (seed + _XXH_PRIME2) & _U32
        v3 = seed
        v4 = (seed - _XXH_PRIME1) & _U32

        def _round(acc: int, offset: int) -> int:
            lane = int.from_bytes(data[offset : offset + 4], 'little')
            return (_rotl32((acc + lane * _XXH_PRIME2) & _U32, 13) * _XXH_PRIME1) & _U32

        while i <= n - 16:
            v1 = _round(v1, i)
            v2 = _round(v2, i + 4)
            v3 = _round(v3, i + 8)
            v4 = _round(v4, i + 12)
            i += 16
        h = (_rotl32(v1, 1) + _rotl32(v2, 7) + _rotl32(v3, 12) + _rotl32(v4, 18)) & _U32
    else:
        h = (seed + _XXH_PRIME5) & _U32
    h = (h + n) & _U32
    while i <= n - 4:
        h = (h + int.from_bytes(data[i : i + 4], 'little') * _XXH_PRIME3) & _U32
        h = (_rotl32(h, 17) * _XXH_PRIME4) & _U32
        i += 4
    while i < n:
        h = (h + data[i] * _XXH_PRIME5) & _U32
        h = (_rotl32(h, 11) * _XXH_PRIME1) & _U32
        i += 1
    h ^= h >> 15
    h = (h * _XXH_PRIME2) & _U32
    h ^= h >> 13
    h = (h * _XXH_PRIME3) & _U32
    h ^= h >> 16
    return h


def ink_mapping_root_key(field_name: str) -> str:
    """AutoKey root for a RepoRegistry mapping field, as SCALE-LE hex."""
    key = xxh32(f'RepoRegistry::{field_name}'.encode())
    return key.to_bytes(4, 'little').hex()


REPOS_ROOT_KEY = ink_mapping_root_key('repos')
PARAMS_ROOT_KEY = ink_mapping_root_key('params')
PARAM_BOUNDS_ROOT_KEY = ink_mapping_root_key('param_bounds')
LABEL_MULTS_ROOT_KEY = ink_mapping_root_key('label_mults')
BRANCH_PATTERNS_ROOT_KEY = ink_mapping_root_key('branch_patterns')
BASKETS_ROOT_KEY = ink_mapping_root_key('baskets')

PACKED_ROOT_STORAGE_KEY = compute_ink5_lazy_key('00000000', b'')

WEIGHT_SUM = 65_535

# owner (32) + paused (1) + netuid (2) + storage_version (4) + price_last (8)
# + price_last_block (8) + last_reg_block (8) + regs_in_block (4) + constants (68)
_PACKED_FIXED_SIZE = 32 + 1 + 2 + 4 + 8 + 8 + 8 + 4 + 68


@dataclass
class RegistryConstants:
    """One-tx adjustable launch constants (Constants in types.rs)."""

    max_repos: int
    immunity_period: int
    price_floor: int
    price_ceiling: int
    price_half_life: int
    price_bump_q32: int
    max_regs_per_block: int
    param_rate_limit_blocks: int
    snapshot_interval: int
    basket_cap: int


@dataclass
class PackedRegistryStorage:
    """Decoded packed root cell of the RepoRegistry contract."""

    owner: bytes
    paused: bool
    netuid: int
    storage_version: int
    price_last: int
    price_last_block: int
    last_reg_block: int
    regs_in_block: int
    constants: RegistryConstants
    repo_ids: List[int]
    voters: List[bytes]
    bound_keys: List[int]


@dataclass
class DecodedRepo:
    """Decoded repos mapping entry (Repo in types.rs)."""

    github_id: int
    full_name: str
    owner: bytes
    reg_block: int
    active: bool


@dataclass
class ParamBounds:
    """Decoded param_bounds mapping entry (Bounds in types.rs)."""

    min: int
    max: int


def encode_compact_length(n: int) -> bytes:
    """SCALE compact length prefix for Vec/String payloads."""
    if n < 0:
        raise ValueError(f'Length must be non-negative: {n}')
    if n < 1 << 6:
        return bytes([n << 2])
    if n < 1 << 14:
        return ((n << 2) | 1).to_bytes(2, 'little')
    if n < 1 << 30:
        return ((n << 2) | 2).to_bytes(4, 'little')
    raise ValueError(f'Length too large for compact encoding: {n}')


def _read_compact(data: bytes, offset: int) -> Tuple[int, int]:
    """Decode a SCALE compact integer, returning (value, next_offset)."""
    mode = data[offset] & 0x03
    if mode == 0:
        return data[offset] >> 2, offset + 1
    width = 2 if mode == 1 else 4
    if mode == 3 or offset + width > len(data):
        raise ValueError('Unsupported or truncated compact integer')
    return int.from_bytes(data[offset : offset + width], 'little') >> 2, offset + width


def _read_bytes(data: bytes, offset: int, n: int) -> Tuple[bytes, int]:
    if offset + n > len(data):
        raise ValueError('Truncated buffer')
    return data[offset : offset + n], offset + n


def _read_bool(data: bytes, offset: int) -> Tuple[bool, int]:
    raw, offset = _read_bytes(data, offset, 1)
    if raw[0] > 1:
        raise ValueError(f'Invalid bool byte: {raw[0]}')
    return raw[0] == 1, offset


def _read_string(data: bytes, offset: int) -> Tuple[str, int]:
    length, offset = _read_compact(data, offset)
    raw, offset = _read_bytes(data, offset, length)
    return raw.decode('utf-8'), offset


def _ensure_consumed(data: bytes, offset: int) -> None:
    if offset != len(data):
        raise ValueError(f'Trailing bytes: consumed {offset} of {len(data)}')


def decode_packed_registry_storage(data: bytes) -> Optional[PackedRegistryStorage]:
    """Decode the packed root cell per the lib.rs byte-offset table."""
    if len(data) < _PACKED_FIXED_SIZE:
        return None
    try:
        offset = 0
        owner, offset = _read_bytes(data, offset, 32)
        paused, offset = _read_bool(data, offset)
        netuid, storage_version = struct.unpack_from('<HI', data, offset)
        offset += 6
        price_last, price_last_block, last_reg_block = struct.unpack_from('<QQQ', data, offset)
        offset += 24
        regs_in_block = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        constants = RegistryConstants(*struct.unpack_from('<IQQQQQIQQI', data, offset))
        offset += 68

        count, offset = _read_compact(data, offset)
        repo_ids = []
        for _ in range(count):
            raw, offset = _read_bytes(data, offset, 8)
            repo_ids.append(int.from_bytes(raw, 'little'))

        count, offset = _read_compact(data, offset)
        voters = []
        for _ in range(count):
            voter, offset = _read_bytes(data, offset, 32)
            voters.append(voter)

        count, offset = _read_compact(data, offset)
        raw, offset = _read_bytes(data, offset, count)
        bound_keys = list(raw)

        _ensure_consumed(data, offset)
    except (struct.error, IndexError, ValueError) as e:
        logger.debug('Failed to decode packed registry storage: %s', e)
        return None

    return PackedRegistryStorage(
        owner=owner,
        paused=paused,
        netuid=netuid,
        storage_version=storage_version,
        price_last=price_last,
        price_last_block=price_last_block,
        last_reg_block=last_reg_block,
        regs_in_block=regs_in_block,
        constants=constants,
        repo_ids=repo_ids,
        voters=voters,
        bound_keys=bound_keys,
    )


def decode_repo(data: bytes) -> Optional[DecodedRepo]:
    """Decode one repos mapping value."""
    try:
        offset = 0
        raw, offset = _read_bytes(data, offset, 8)
        github_id = int.from_bytes(raw, 'little')
        full_name, offset = _read_string(data, offset)
        owner, offset = _read_bytes(data, offset, 32)
        raw, offset = _read_bytes(data, offset, 8)
        reg_block = int.from_bytes(raw, 'little')
        active, offset = _read_bool(data, offset)
        _ensure_consumed(data, offset)
    except (IndexError, ValueError, UnicodeDecodeError) as e:
        logger.debug('Failed to decode repo entry: %s', e)
        return None
    return DecodedRepo(github_id=github_id, full_name=full_name, owner=owner, reg_block=reg_block, active=active)


def decode_param_entries(data: bytes) -> Optional[List[Tuple[int, int]]]:
    """Decode a params mapping value: Vec<(u8, u64)>."""
    try:
        count, offset = _read_compact(data, 0)
        entries = []
        for _ in range(count):
            raw, offset = _read_bytes(data, offset, 9)
            entries.append((raw[0], int.from_bytes(raw[1:], 'little')))
        _ensure_consumed(data, offset)
    except (IndexError, ValueError) as e:
        logger.debug('Failed to decode param entries: %s', e)
        return None
    return entries


def decode_bounds(data: bytes) -> Optional[ParamBounds]:
    """Decode a param_bounds mapping value: Bounds { min: u64, max: u64 }."""
    if len(data) != 16:
        return None
    lo, hi = struct.unpack('<QQ', data)
    return ParamBounds(min=lo, max=hi)


def decode_label_entries(data: bytes) -> Optional[List[Tuple[str, int]]]:
    """Decode a label_mults mapping value: Vec<(String, u64)>."""
    try:
        count, offset = _read_compact(data, 0)
        entries = []
        for _ in range(count):
            label, offset = _read_string(data, offset)
            raw, offset = _read_bytes(data, offset, 8)
            entries.append((label, int.from_bytes(raw, 'little')))
        _ensure_consumed(data, offset)
    except (IndexError, ValueError, UnicodeDecodeError) as e:
        logger.debug('Failed to decode label entries: %s', e)
        return None
    return entries


def decode_string_vec(data: bytes) -> Optional[List[str]]:
    """Decode a branch_patterns mapping value: Vec<String>."""
    try:
        count, offset = _read_compact(data, 0)
        patterns = []
        for _ in range(count):
            pattern, offset = _read_string(data, offset)
            patterns.append(pattern)
        _ensure_consumed(data, offset)
    except (IndexError, ValueError, UnicodeDecodeError) as e:
        logger.debug('Failed to decode string vec: %s', e)
        return None
    return patterns


def decode_basket_entries(data: bytes) -> Optional[List[Tuple[int, int]]]:
    """Decode a baskets mapping value: Vec<(u64, u16)>."""
    try:
        count, offset = _read_compact(data, 0)
        entries = []
        for _ in range(count):
            raw, offset = _read_bytes(data, offset, 10)
            entries.append((int.from_bytes(raw[:8], 'little'), int.from_bytes(raw[8:], 'little')))
        _ensure_consumed(data, offset)
    except (IndexError, ValueError) as e:
        logger.debug('Failed to decode basket entries: %s', e)
        return None
    return entries


def read_child_storage_bytes(substrate, child_key: str, storage_key: str, at: Optional[str] = None) -> Optional[bytes]:
    """Read one contract child-storage cell, optionally pinned to a block hash."""
    result = substrate.rpc_request('childstate_getStorage', [child_key, storage_key, at])
    raw_hex = result.get('result')
    if not raw_hex:
        return None
    return bytes.fromhex(raw_hex.replace('0x', ''))


def read_packed_registry_storage(
    substrate, contract_addr: str, at: Optional[str] = None
) -> Optional[PackedRegistryStorage]:
    """Read and decode the packed root cell for a deployed registry contract."""
    child_key = get_contract_child_storage_key(substrate, contract_addr)
    if not child_key:
        return None
    data = read_child_storage_bytes(substrate, child_key, PACKED_ROOT_STORAGE_KEY, at)
    if not data:
        return None
    return decode_packed_registry_storage(data)
