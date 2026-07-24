# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Payload codec for repo weight preference vectors published to the chain.

Wire format (v1): zlib("v1|owner/repo:weight|...") — names lowercase, weights
u16 relative shares, at most CONSENSUS_MAX_REPOS entries, one BigRaw field.
"""

import re
import zlib
from typing import Dict, Mapping, Optional

from gittensor.validator.utils.config import CONSENSUS_MAX_PAYLOAD_BYTES, CONSENSUS_MAX_REPOS

PAYLOAD_VERSION = 'v1'
U16_MAX = 65535
_MAX_PLAINTEXT_BYTES = 4096
_REPO_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9\-_.]*/[a-z0-9\-_.]+$')


class CodecError(ValueError):
    """Raised when local preferences cannot be encoded into a valid payload."""


def canonicalize_prefs(raw: Mapping[str, float]) -> Dict[str, int]:
    """Normalize local preferences into the canonical on-chain form.

    Lowercases names (merging duplicates), keeps the top CONSENSUS_MAX_REPOS by
    weight, and quantizes to u16 integers summing exactly U16_MAX via largest
    remainder. Deterministic: all ties break lexicographically.
    """
    merged: Dict[str, float] = {}
    for name, weight in raw.items():
        if not isinstance(weight, (int, float)) or weight <= 0:
            continue
        key = str(name).strip().lower()
        merged[key] = merged.get(key, 0.0) + float(weight)

    for name in merged:
        if not _REPO_NAME_RE.match(name):
            raise CodecError(f'Invalid repository name: {name!r}')

    top = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:CONSENSUS_MAX_REPOS]
    if not top:
        raise CodecError('No repositories with positive weight to encode')

    total = sum(w for _, w in top)
    floors = {name: int(w * U16_MAX // total) for name, w in top}
    remainders = sorted(top, key=lambda kv: (-(kv[1] * U16_MAX / total - floors[kv[0]]), kv[0]))
    for name, _ in remainders[: U16_MAX - sum(floors.values())]:
        floors[name] += 1

    return {name: floors[name] for name in sorted(floors) if floors[name] > 0}


def encode_prefs(prefs: Mapping[str, int]) -> bytes:
    """Encode canonical preferences into the compressed wire payload."""
    if not prefs or len(prefs) > CONSENSUS_MAX_REPOS:
        raise CodecError(f'Basket must contain 1-{CONSENSUS_MAX_REPOS} repositories, got {len(prefs)}')
    for name, weight in prefs.items():
        if not _REPO_NAME_RE.match(name):
            raise CodecError(f'Invalid repository name: {name!r}')
        if not isinstance(weight, int) or not 0 < weight <= U16_MAX:
            raise CodecError(f'Weight for {name} must be an int in [1, {U16_MAX}], got {weight!r}')

    plaintext = PAYLOAD_VERSION + '|' + '|'.join(f'{name}:{prefs[name]}' for name in sorted(prefs))
    payload = zlib.compress(plaintext.encode('ascii'), 9)
    if len(payload) > CONSENSUS_MAX_PAYLOAD_BYTES:
        raise CodecError(f'Encoded payload is {len(payload)} bytes, exceeds {CONSENSUS_MAX_PAYLOAD_BYTES}')
    return payload


def decode_prefs(payload: bytes) -> Optional[Dict[str, int]]:
    """Decode a wire payload into canonical preferences.

    Returns None on ANY invalidity — a bad payload disqualifies the voter,
    never raises. Zero weights are dropped; an empty result is invalid.
    """
    try:
        decompressor = zlib.decompressobj()
        plaintext = decompressor.decompress(payload, _MAX_PLAINTEXT_BYTES).decode('ascii')
        if not decompressor.eof:
            return None

        version, *entries = plaintext.split('|')
        if version != PAYLOAD_VERSION or not entries or len(entries) > CONSENSUS_MAX_REPOS:
            return None

        prefs: Dict[str, int] = {}
        seen = set()
        for entry in entries:
            name, _, weight_str = entry.rpartition(':')
            name = name.lower()
            weight = int(weight_str)
            if not _REPO_NAME_RE.match(name) or not 0 <= weight <= U16_MAX or name in seen:
                return None
            seen.add(name)
            if weight > 0:
                prefs[name] = weight

        return {name: prefs[name] for name in sorted(prefs)} if prefs else None
    except Exception:
        return None
