import struct

from gittensor.validator.issue_competitions.storage_utils import (
    compute_ink5_lazy_key,
    decode_issue_from_storage,
    decode_packed_contract_storage,
    get_contract_child_storage_key,
    read_contract_packed_storage,
)


def _scale_compact_len(value: int) -> bytes:
    if value < 64:
        return bytes([value << 2])
    if value < 1 << 14:
        encoded = (value << 2) | 0x01
        return struct.pack('<H', encoded)
    raise ValueError('Test helper supports compact lengths up to 14 bits')


def _build_issue_bytes(
    issue_id: int,
    github_url_hash: bytes,
    repository_full_name: str,
    issue_number: int,
    bounty_amount: int,
    target_bounty: int,
    status_byte: int,
    registered_at_block: int,
) -> bytes:
    repo_bytes = repository_full_name.encode('utf-8')
    bounty_lo = bounty_amount & 0xFFFFFFFFFFFFFFFF
    bounty_hi = bounty_amount >> 64
    target_lo = target_bounty & 0xFFFFFFFFFFFFFFFF
    target_hi = target_bounty >> 64

    return b''.join(
        [
            struct.pack('<Q', issue_id),
            github_url_hash,
            _scale_compact_len(len(repo_bytes)),
            repo_bytes,
            struct.pack('<I', issue_number),
            struct.pack('<QQ', bounty_lo, bounty_hi),
            struct.pack('<QQ', target_lo, target_hi),
            bytes([status_byte]),
            struct.pack('<I', registered_at_block),
        ]
    )


class _FakeContractInfo:
    def __init__(self, value):
        self.value = value


class _FakeSubstrate:
    def __init__(self, contract_info, packed_hex):
        self._contract_info = contract_info
        self._packed_hex = packed_hex

    def query(self, module, storage, params):
        assert module == 'Contracts'
        assert storage == 'ContractInfoOf'
        assert params
        return self._contract_info

    def rpc_request(self, method, params):
        if method == 'childstate_getKeysPaged':
            return {'result': ['0xaaaa', '0xbeef00000000']}
        if method == 'childstate_getStorage':
            return {'result': self._packed_hex}
        raise AssertionError(f'Unexpected method {method}')


def test_compute_ink5_lazy_key_matches_expected_fixture():
    encoded_id = struct.pack('<Q', 1)
    assert compute_ink5_lazy_key('52789899', encoded_id) == '0x67fcb56968696987a6b4a84fb45eed3d527898990100000000000000'


def test_decode_packed_contract_storage_decodes_all_fields():
    owner = bytes(range(32))
    treasury = bytes(reversed(range(32)))
    netuid = 42
    next_issue_id = 55
    alpha_pool = (3 << 64) + 7
    data = owner + treasury + struct.pack('<H', netuid) + struct.pack('<Q', next_issue_id) + struct.pack('<QQ', 7, 3)

    decoded = decode_packed_contract_storage(data)

    assert decoded is not None
    assert decoded.owner == owner
    assert decoded.treasury_hotkey == treasury
    assert decoded.netuid == netuid
    assert decoded.next_issue_id == next_issue_id
    assert decoded.alpha_pool == alpha_pool


def test_decode_packed_contract_storage_rejects_short_bytes():
    assert decode_packed_contract_storage(b'\x00' * 73) is None


def test_decode_issue_from_storage_single_byte_string_length():
    data = _build_issue_bytes(
        issue_id=5,
        github_url_hash=b'\x11' * 32,
        repository_full_name='entrius/gittensor',
        issue_number=223,
        bounty_amount=150_000_000_000,
        target_bounty=200_000_000_000,
        status_byte=1,
        registered_at_block=999,
    )

    decoded = decode_issue_from_storage(data)

    assert decoded is not None
    assert decoded.id == 5
    assert decoded.github_url_hash == b'\x11' * 32
    assert decoded.repository_full_name == 'entrius/gittensor'
    assert decoded.issue_number == 223
    assert decoded.bounty_amount == 150_000_000_000
    assert decoded.target_bounty == 200_000_000_000
    assert decoded.status_byte == 1
    assert decoded.registered_at_block == 999


def test_decode_issue_from_storage_two_byte_string_length():
    long_repo = 'a' * 70
    bounty_amount = (2 << 64) + 9
    target_bounty = (5 << 64) + 13
    data = _build_issue_bytes(
        issue_id=99,
        github_url_hash=b'\x22' * 32,
        repository_full_name=long_repo,
        issue_number=777,
        bounty_amount=bounty_amount,
        target_bounty=target_bounty,
        status_byte=2,
        registered_at_block=12345,
    )

    decoded = decode_issue_from_storage(data)

    assert decoded is not None
    assert decoded.id == 99
    assert decoded.repository_full_name == long_repo
    assert decoded.issue_number == 777
    assert decoded.bounty_amount == bounty_amount
    assert decoded.target_bounty == target_bounty
    assert decoded.status_byte == 2
    assert decoded.registered_at_block == 12345


def test_decode_issue_from_storage_returns_none_for_invalid_bytes():
    assert decode_issue_from_storage(b'\x00\x01\x02') is None


def test_get_contract_child_storage_key_accepts_multiple_trie_id_formats():
    expected_suffix = '3a6368696c645f73746f726167653a64656661756c743a0102'

    with_string = get_contract_child_storage_key(
        _FakeSubstrate(_FakeContractInfo({'trie_id': '0x0102'}), packed_hex='0x'),
        '5Contract',
    )
    with_bytes = get_contract_child_storage_key(
        _FakeSubstrate(_FakeContractInfo({'trie_id': b'\x01\x02'}), packed_hex='0x'),
        '5Contract',
    )
    with_list = get_contract_child_storage_key(
        _FakeSubstrate(_FakeContractInfo({'trie_id': [1, 2]}), packed_hex='0x'),
        '5Contract',
    )

    assert with_string == f'0x{expected_suffix}'
    assert with_bytes == f'0x{expected_suffix}'
    assert with_list == f'0x{expected_suffix}'


def test_read_contract_packed_storage_reads_and_decodes_bytes():
    owner = bytes(range(32))
    treasury = bytes([9] * 32)
    packed_bytes = owner + treasury + struct.pack('<H', 11) + struct.pack('<Q', 101) + struct.pack('<QQ', 1, 4)
    substrate = _FakeSubstrate(
        _FakeContractInfo({'trie_id': '0x0102'}),
        packed_hex='0x' + packed_bytes.hex(),
    )

    decoded = read_contract_packed_storage(substrate, '5Contract')

    assert decoded is not None
    assert decoded.netuid == 11
    assert decoded.next_issue_id == 101
    assert decoded.alpha_pool == (4 << 64) + 1
