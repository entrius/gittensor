# The MIT License (MIT)
# Copyright 2025 Entrius

"""Tests for Issue contract codec functions."""

import hashlib
import struct

from gittensor.validator.issue_competitions.codec import compute_ink5_lazy_key, decode_issue_bytes


def _build_issue_bytes(
    issue_id: int = 1,
    url_hash: bytes = b'\x00' * 32,
    repo_name: str = 'entrius/gittensor',
    issue_number: int = 42,
    bounty_amount: int = 1_000_000_000,
    target_bounty: int = 2_000_000_000,
    status_byte: int = 1,
    registered_at_block: int = 100,
) -> bytes:
    """Build a SCALE-encoded Issue struct for testing."""
    buf = bytearray()
    buf += struct.pack('<Q', issue_id)
    buf += url_hash
    name_bytes = repo_name.encode('utf-8')
    name_len = len(name_bytes)
    if name_len < 64:
        buf += bytes([name_len << 2])
    else:
        buf += struct.pack('<H', (name_len << 2) | 1)
    buf += name_bytes
    buf += struct.pack('<I', issue_number)
    buf += struct.pack('<QQ', bounty_amount & ((1 << 64) - 1), bounty_amount >> 64)
    buf += struct.pack('<QQ', target_bounty & ((1 << 64) - 1), target_bounty >> 64)
    buf += bytes([status_byte])
    buf += struct.pack('<I', registered_at_block)
    return bytes(buf)


class TestDecodeIssueBytes:
    def test_round_trip(self):
        data = _build_issue_bytes()
        result = decode_issue_bytes(data)
        assert result['id'] == 1
        assert result['github_url_hash'] == b'\x00' * 32
        assert result['repository_full_name'] == 'entrius/gittensor'
        assert result['issue_number'] == 42
        assert result['bounty_amount'] == 1_000_000_000
        assert result['target_bounty'] == 2_000_000_000
        assert result['status_byte'] == 1
        assert result['registered_at_block'] == 100

    def test_long_repo_name_two_byte_compact(self):
        long_name = 'org/' + 'a' * 100
        data = _build_issue_bytes(repo_name=long_name)
        result = decode_issue_bytes(data)
        assert result['repository_full_name'] == long_name

    def test_empty_repo_name(self):
        data = _build_issue_bytes(repo_name='')
        result = decode_issue_bytes(data)
        assert result['repository_full_name'] == ''

    def test_large_u128_bounty(self):
        large = (1 << 127) + 42
        data = _build_issue_bytes(bounty_amount=large)
        result = decode_issue_bytes(data)
        assert result['bounty_amount'] == large

    def test_all_status_values(self):
        for status in range(4):
            data = _build_issue_bytes(status_byte=status)
            result = decode_issue_bytes(data)
            assert result['status_byte'] == status

    def test_url_hash_preserved(self):
        url_hash = hashlib.sha256(b'https://github.com/entrius/gittensor/issues/42').digest()
        data = _build_issue_bytes(url_hash=url_hash)
        result = decode_issue_bytes(data)
        assert result['github_url_hash'] == url_hash


class TestComputeInk5LazyKey:
    def test_deterministic(self):
        key1 = compute_ink5_lazy_key('52789899', struct.pack('<Q', 1))
        key2 = compute_ink5_lazy_key('52789899', struct.pack('<Q', 1))
        assert key1 == key2

    def test_different_ids_produce_different_keys(self):
        key1 = compute_ink5_lazy_key('52789899', struct.pack('<Q', 1))
        key2 = compute_ink5_lazy_key('52789899', struct.pack('<Q', 2))
        assert key1 != key2

    def test_starts_with_0x(self):
        key = compute_ink5_lazy_key('52789899', struct.pack('<Q', 1))
        assert key.startswith('0x')

    def test_0x_prefix_in_root_key_stripped(self):
        key1 = compute_ink5_lazy_key('0x52789899', struct.pack('<Q', 1))
        key2 = compute_ink5_lazy_key('52789899', struct.pack('<Q', 1))
        assert key1 == key2
