import struct

import pytest

from gittensor.validator.issue_competitions.storage_utils import ISSUES_MAPPING_ROOT_KEY
from gittensor.validator.repo_registry.storage_utils import (
    BASKETS_ROOT_KEY,
    BRANCH_PATTERNS_ROOT_KEY,
    LABEL_MULTS_ROOT_KEY,
    PACKED_ROOT_STORAGE_KEY,
    PARAM_BOUNDS_ROOT_KEY,
    PARAMS_ROOT_KEY,
    REPOS_ROOT_KEY,
    decode_basket_entries,
    decode_bounds,
    decode_label_entries,
    decode_packed_registry_storage,
    decode_param_entries,
    decode_repo,
    decode_string_vec,
    encode_compact_length,
    ink_mapping_root_key,
    read_packed_registry_storage,
    xxh32,
)

LAUNCH_CONSTANTS = struct.pack(
    '<IQQQQQIQQI',
    32,  # max_repos
    216_000,  # immunity_period
    500_000_000_000,  # price_floor
    500_000_000_000_000,  # price_ceiling
    100_800,  # price_half_life
    2 << 32,  # price_bump_q32
    1,  # max_regs_per_block
    3_600,  # param_rate_limit_blocks
    3_600,  # snapshot_interval
    10,  # basket_cap
)


def _build_packed(
    owner: bytes = b'\x01' * 32,
    paused: bool = False,
    netuid: int = 2,
    storage_version: int = 1,
    price_last: int = 500_000_000_000,
    price_last_block: int = 100,
    last_reg_block: int = 0,
    regs_in_block: int = 0,
    constants: bytes = LAUNCH_CONSTANTS,
    repo_ids: list = (),
    voters: list = (),
    bound_keys: list = (),
) -> bytes:
    return b''.join(
        [
            owner,
            bytes([paused]),
            struct.pack('<H', netuid),
            struct.pack('<I', storage_version),
            struct.pack('<QQQ', price_last, price_last_block, last_reg_block),
            struct.pack('<I', regs_in_block),
            constants,
            encode_compact_length(len(repo_ids)),
            b''.join(struct.pack('<Q', repo_id) for repo_id in repo_ids),
            encode_compact_length(len(voters)),
            b''.join(voters),
            encode_compact_length(len(bound_keys)),
            bytes(bound_keys),
        ]
    )


def _build_repo(
    github_id: int = 42,
    full_name: str = 'entrius/gittensor',
    owner: bytes = b'\x07' * 32,
    reg_block: int = 999,
    active: bool = True,
) -> bytes:
    name_bytes = full_name.encode('utf-8')
    return b''.join(
        [
            struct.pack('<Q', github_id),
            encode_compact_length(len(name_bytes)),
            name_bytes,
            owner,
            struct.pack('<Q', reg_block),
            bytes([active]),
        ]
    )


class _FakeContractInfo:
    def __init__(self, value):
        self.value = value


class _FakeSubstrate:
    def __init__(self, packed_hex):
        self._packed_hex = packed_hex
        self.storage_requests = []

    def query(self, module, storage, params):
        assert module == 'Contracts'
        assert storage == 'ContractInfoOf'
        return _FakeContractInfo({'trie_id': '0x0102'})

    def rpc_request(self, method, params):
        assert method == 'childstate_getStorage'
        self.storage_requests.append(params)
        return {'result': self._packed_hex}


# ============================================================================
# Root key derivation
# ============================================================================


def test_xxh32_reproduces_issues_v0_mapping_root_key():
    # Cross-implementation anchor: ink! KeyComposer produced '52789899' for
    # issues-v0; the same derivation must reproduce it here.
    key = xxh32(b'IssueBountyManager::issues')
    assert key.to_bytes(4, 'little').hex() == ISSUES_MAPPING_ROOT_KEY


def test_mapping_root_key_fixtures():
    assert REPOS_ROOT_KEY == 'cb55726b'
    assert PARAMS_ROOT_KEY == 'a8c90dad'
    assert PARAM_BOUNDS_ROOT_KEY == 'a03795cf'
    assert LABEL_MULTS_ROOT_KEY == '7bba888c'
    assert BRANCH_PATTERNS_ROOT_KEY == 'f086cc94'
    assert BASKETS_ROOT_KEY == '5539377f'


def test_ink_mapping_root_key_matches_key_composer_formula():
    assert ink_mapping_root_key('repos') == xxh32(b'RepoRegistry::repos').to_bytes(4, 'little').hex()


def test_packed_root_storage_key_is_root_zero_cell():
    assert PACKED_ROOT_STORAGE_KEY.startswith('0x')
    assert PACKED_ROOT_STORAGE_KEY.endswith('00000000')


# ============================================================================
# Packed root decode
# ============================================================================


def test_decode_packed_empty_registry():
    decoded = decode_packed_registry_storage(_build_packed())

    assert decoded is not None
    assert decoded.owner == b'\x01' * 32
    assert decoded.paused is False
    assert decoded.netuid == 2
    assert decoded.storage_version == 1
    assert decoded.price_last == 500_000_000_000
    assert decoded.price_last_block == 100
    assert decoded.last_reg_block == 0
    assert decoded.regs_in_block == 0
    assert decoded.repo_ids == []
    assert decoded.voters == []
    assert decoded.bound_keys == []


def test_decode_packed_launch_constants():
    constants = decode_packed_registry_storage(_build_packed()).constants

    assert constants.max_repos == 32
    assert constants.immunity_period == 216_000
    assert constants.price_floor == 500_000_000_000
    assert constants.price_ceiling == 500_000_000_000_000
    assert constants.price_half_life == 100_800
    assert constants.price_bump_q32 == 2 << 32
    assert constants.max_regs_per_block == 1
    assert constants.param_rate_limit_blocks == 3_600
    assert constants.snapshot_interval == 3_600
    assert constants.basket_cap == 10


def test_decode_packed_populated_registry():
    voters = [b'\xaa' * 32, b'\xbb' * 32]
    data = _build_packed(
        paused=True,
        repo_ids=[7, 42, 1_000_000],
        voters=voters,
        bound_keys=list(range(1, 27)),
    )

    decoded = decode_packed_registry_storage(data)

    assert decoded is not None
    assert decoded.paused is True
    assert decoded.repo_ids == [7, 42, 1_000_000]
    assert decoded.voters == voters
    assert decoded.bound_keys == list(range(1, 27))


@pytest.mark.parametrize('length', [0, 32, 134, 135, 137])
def test_decode_packed_rejects_short_buffers(length):
    assert decode_packed_registry_storage(b'\x00' * length) is None


def test_decode_packed_rejects_trailing_bytes():
    assert decode_packed_registry_storage(_build_packed() + b'\x00') is None


def test_decode_packed_rejects_invalid_paused_byte():
    data = bytearray(_build_packed())
    data[32] = 2
    assert decode_packed_registry_storage(bytes(data)) is None


def test_decode_packed_rejects_truncated_voter_list():
    data = _build_packed(voters=[b'\xaa' * 32])
    assert decode_packed_registry_storage(data[:-5]) is None


# ============================================================================
# Mapping value decoders
# ============================================================================


def test_decode_repo_roundtrip():
    decoded = decode_repo(_build_repo())

    assert decoded is not None
    assert decoded.github_id == 42
    assert decoded.full_name == 'entrius/gittensor'
    assert decoded.owner == b'\x07' * 32
    assert decoded.reg_block == 999
    assert decoded.active is True


def test_decode_repo_inactive():
    decoded = decode_repo(_build_repo(active=False))
    assert decoded is not None
    assert decoded.active is False


def test_decode_repo_two_byte_name_length():
    long_name = 'org/' + 'a' * 90
    decoded = decode_repo(_build_repo(full_name=long_name))
    assert decoded is not None
    assert decoded.full_name == long_name


def test_decode_repo_rejects_invalid_bytes():
    assert decode_repo(b'\x00\x01\x02') is None
    assert decode_repo(_build_repo() + b'\xff') is None
    assert decode_repo(_build_repo()[:-1]) is None


def test_decode_param_entries():
    data = encode_compact_length(2) + struct.pack('<BQ', 1, 500_000) + struct.pack('<BQ', 17, 30)
    assert decode_param_entries(data) == [(1, 500_000), (17, 30)]


def test_decode_param_entries_empty_and_invalid():
    assert decode_param_entries(encode_compact_length(0)) == []
    assert decode_param_entries(encode_compact_length(2) + struct.pack('<BQ', 1, 500_000)) is None


def test_decode_bounds():
    bounds = decode_bounds(struct.pack('<QQ', 300_000, 1_000_000))
    assert bounds is not None
    assert bounds.min == 300_000
    assert bounds.max == 1_000_000


def test_decode_bounds_rejects_wrong_size():
    assert decode_bounds(b'\x00' * 15) is None
    assert decode_bounds(b'\x00' * 17) is None


def test_decode_label_entries():
    label = 'bug'.encode()
    data = encode_compact_length(1) + encode_compact_length(len(label)) + label + struct.pack('<Q', 1_500_000)
    assert decode_label_entries(data) == [('bug', 1_500_000)]


def test_decode_label_entries_rejects_truncated():
    assert decode_label_entries(encode_compact_length(1) + encode_compact_length(3) + b'bu') is None


def test_decode_string_vec():
    patterns = ['main', 'release/*']
    data = encode_compact_length(len(patterns))
    for pattern in patterns:
        raw = pattern.encode()
        data += encode_compact_length(len(raw)) + raw
    assert decode_string_vec(data) == patterns


def test_decode_basket_entries():
    data = encode_compact_length(2) + struct.pack('<QH', 7, 30_000) + struct.pack('<QH', 42, 35_535)
    assert decode_basket_entries(data) == [(7, 30_000), (42, 35_535)]


def test_decode_basket_entries_empty_and_invalid():
    assert decode_basket_entries(encode_compact_length(0)) == []
    assert decode_basket_entries(encode_compact_length(1) + struct.pack('<Q', 7)) is None
    assert decode_basket_entries(encode_compact_length(1) + struct.pack('<QH', 7, 1) + b'\x00') is None


# ============================================================================
# Read plumbing
# ============================================================================


def test_read_packed_registry_storage_decodes_and_pins_block_hash():
    substrate = _FakeSubstrate('0x' + _build_packed(repo_ids=[42]).hex())

    decoded = read_packed_registry_storage(substrate, '5Contract', at='0xabc123')

    assert decoded is not None
    assert decoded.repo_ids == [42]
    child_key, storage_key, at = substrate.storage_requests[0]
    assert child_key.startswith('0x3a6368696c645f73746f726167653a64656661756c743a')
    assert storage_key == PACKED_ROOT_STORAGE_KEY
    assert at == '0xabc123'


def test_read_packed_registry_storage_returns_none_when_cell_missing():
    substrate = _FakeSubstrate(None)
    assert read_packed_registry_storage(substrate, '5Contract') is None
