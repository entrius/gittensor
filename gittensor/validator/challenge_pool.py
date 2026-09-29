# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The challenge pool on the validator: read the challenge evaluator's scorecard and hand its weights to the emission
blend, which pays them out of ``CHALLENGE_EMISSION_SHARE`` (carved from the OSS pool). The evaluator does the running,
the king-of-the-hill and the per-challenge split (``emission_share`` in ``weights/challenges.json``).

* ``CHALLENGE_SCORECARD_PATH`` unset or ``CHALLENGE_EMISSION_SHARE`` 0: nothing here runs and the blend is today's.
* A scorecard that fails ``read_scorecard`` (sha256 mismatch, past ``valid_until``, not a ``SCHEMA`` document,
  malformed) gives an empty pool: the whole challenge share recycles. Never last-known weights.
* A valid one's sha256 is logged, not committed on chain: ``set_commitment`` holds one value per hotkey and that slot
  carries the compute scorecard (v0).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

import bittensor as bt

from gittensor.constants import CHALLENGE_SCORECARD_SCHEMA as SCHEMA
from gittensor.validator.compute_pool import ScorecardPool, pool_from_scorecard

if TYPE_CHECKING:
    from neurons.validator import Validator


def challenge_pool_for(self: 'Validator', path: str, now: Optional[float] = None) -> ScorecardPool:
    now = time.time() if now is None else now
    pool = pool_from_scorecard(path, list(self.metagraph.hotkeys), now, SCHEMA)
    if pool.sha256 is None:
        bt.logging.warning(f'Challenge pool: scorecard refused ({pool.reason}); the challenge share recycles')
    else:
        bt.logging.info(f'Challenge pool: scorecard {pool.sha256} pays {len(pool.rewards)} registered miner(s)')
    return pool
