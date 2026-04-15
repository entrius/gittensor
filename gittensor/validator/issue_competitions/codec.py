# The MIT License (MIT)
# Copyright 2025 Entrius

"""SCALE codec utilities for Issue Bounty contract storage."""

import hashlib
import struct
from typing import Any, Dict


def compute_ink5_lazy_key(root_key_hex: str, encoded_key: bytes) -> str:
    """Compute Ink! 5 lazy mapping storage key using blake2_128concat."""
    root_key = bytes.fromhex(root_key_hex.replace('0x', ''))
    data = root_key + encoded_key
    h = hashlib.blake2b(data, digest_size=16).digest()
    return '0x' + (h + data).hex()


def decode_issue_bytes(data: bytes) -> Dict[str, Any]:
    """Decode a SCALE-encoded Issue struct from contract child storage.

    Layout:
        id: u64 (8 bytes)
        github_url_hash: [u8; 32] (32 bytes)
        repository_full_name: String (compact len + bytes)
        issue_number: u32 (4 bytes)
        bounty_amount: u128 (16 bytes)
        target_bounty: u128 (16 bytes)
        status: IssueStatus enum (1 byte)
        registered_at_block: u32 (4 bytes)

    Args:
        data: Raw bytes from contract child storage.

    Returns:
        Dict with all decoded fields.
    """
    offset = 0
    issue_id = struct.unpack_from('<Q', data, offset)[0]
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

    return {
        'id': issue_id,
        'github_url_hash': github_url_hash,
        'repository_full_name': repo_name,
        'issue_number': issue_number,
        'bounty_amount': int(bounty_amount),
        'target_bounty': int(target_bounty),
        'status_byte': status_byte,
        'registered_at_block': registered_at_block,
    }
