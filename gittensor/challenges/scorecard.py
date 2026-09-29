# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The challenge scorecard: the compute scorecard's mechanics (``latest.json`` in canonical JSON, ``latest.sha256``,
a dated copy; read back with ``read_scorecard(..., schema=SCHEMA)``) with its own schema. Each challenge's king is paid
that challenge's ``emission_share`` of the challenge pool; a challenge without a king, and the registry's slack,
recycle. The document is valid for ``TTL_S``: an evaluator that stops writing stops paying."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping

from gittensor.challenges.leaderboard import Leaderboard, Standing
from gittensor.challenges.registry import Challenge
from gittensor.constants import CHALLENGE_SCORECARD_SCHEMA as SCHEMA

TTL_S = 2 * 60 * 60.0


def build_scorecard(board: Leaderboard, registry: Mapping[str, Challenge], now: float, ttl_s: float = TTL_S) -> dict:
    """The unsigned document: ``attestation`` is added over it by the attestor."""
    standings = {cid: board.challenges.get(cid, Standing()) for cid in sorted(registry)}
    shares: dict[str, list[float]] = defaultdict(list)
    for cid, standing in standings.items():
        if standing.king is not None and registry[cid].emission_share > 0:
            shares[standing.king.hotkey].append(registry[cid].emission_share)
    weights = {hotkey: math.fsum(s) for hotkey, s in shares.items()}
    return {
        'schema': SCHEMA,
        'issued_at': now,
        'valid_until': now + ttl_s,
        'recycle_share': max(0.0, 1.0 - math.fsum(weights.values())),
        'hotkeys': [{'hotkey': h, 'weight': w} for h, w in sorted(weights.items())],
        'challenges': Leaderboard(standings).as_dict()['challenges'],
    }
