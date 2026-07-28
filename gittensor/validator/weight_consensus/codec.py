# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Canonical form for repo weight preference vectors.

Baskets live on-chain as structured (github_id, u16 weight) arrays in the
repos-v0 contract — there is no wire codec. This module quantizes local
preferences into the canonical client-side form and validates structured
baskets read back from the contract.
"""

import re
from typing import Any, Dict, Mapping, Optional

from gittensor.validator.utils.config import CONSENSUS_MAX_REPOS

U16_MAX = 65535
_REPO_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9\-_.]*/[a-z0-9\-_.]+$')


class CodecError(ValueError):
    """Raised when local preferences cannot be canonicalized into a valid basket."""


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


def validate_prefs(prefs: Any) -> Optional[Dict[str, int]]:
    """Validate a structured basket into canonical preferences.

    Returns None on ANY invalidity — a bad basket disqualifies the voter,
    never raises. Zero weights are dropped; an empty result is invalid.
    """
    if not isinstance(prefs, Mapping) or not 0 < len(prefs) <= CONSENSUS_MAX_REPOS:
        return None

    valid: Dict[str, int] = {}
    seen = set()
    for name, weight in prefs.items():
        if not isinstance(name, str) or isinstance(weight, bool) or not isinstance(weight, int):
            return None
        name = name.lower()
        if not _REPO_NAME_RE.match(name) or not 0 <= weight <= U16_MAX or name in seen:
            return None
        seen.add(name)
        if weight > 0:
            valid[name] = weight

    return {name: valid[name] for name in sorted(valid)} if valid else None
