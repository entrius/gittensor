# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Persist each serving audit round so compute miners can see what this validator saw.

The serving loop is otherwise RAM-only: a miner has no way to learn that it is on probation, why its last request
missed, or what its settled score is worth. ``ServingRoundStorage`` writes the round report ``audit_round`` already
builds (``state.last_round``) plus the settled scores into two tables that the das API reads for gittensor.io.

Runs on the audit thread with its own connection (psycopg connections are single-threaded). Any failure is logged
and dropped — persistence must never stall or fail a round.
"""

import datetime as dt
from typing import Any, Dict, Optional

import bittensor as bt

from gittensor.classes import ServingPricing
from gittensor.constants import SERVING_DB_RETENTION_DAYS, SERVING_EMISSION_SHARE_CAP, SERVING_GPU_HOUR_USD
from gittensor.serving.loadout import ServingRelease
from gittensor.serving.state import ServingState
from gittensor.validator.emission_allocation import serving_share
from gittensor.validator.storage.database import create_database_connection
from gittensor.validator.storage.queries import (
    BULK_INSERT_SERVING_MINER_ROUNDS,
    INSERT_SERVING_ROUND,
    PRUNE_SERVING_MINER_ROUNDS,
    PRUNE_SERVING_ROUNDS,
)


def round_rows(
    validator_hotkey: str,
    round_ts: dt.datetime,
    last_round: Dict[str, Any],
    settled: Dict[str, float],
    pricing: Optional[ServingPricing],
    release: Optional[ServingRelease] = None,
    allow_unpriced_cap: bool = False,
) -> tuple[tuple, list[tuple]]:
    """(serving_rounds row, serving_miner_rounds rows) for one audit round.

    ``card_equivalents`` and ``pool_share`` are what the next OSS round will pay on: settled scores (served tokens
    in card-hours) summed, priced through ``serving_share`` exactly as ``blend_emission_pools`` does. The economics
    ($/card-hour, cap) and the enforced release (pin, digest) ride along so readers never hard-code them.
    """
    windows: Dict[int, dict] = last_round.get('windows', {})
    card_equiv = sum(settled.values())
    share = serving_share(card_equiv, pricing, allow_unpriced_cap)
    summary = (
        validator_hotkey,
        round_ts,
        int(last_round.get('served', 0)),
        int(last_round.get('gateway', 0)),
        int(last_round.get('baseline', 0)),
        int(last_round.get('pass', 0)),
        int(last_round.get('miss', 0)),
        int(last_round.get('strike', 0)),
        int(last_round.get('neutral', 0)),
        int(last_round.get('ready', 0)),
        int(last_round.get('probation', 0)),
        int(last_round.get('quarantined', 0)),
        card_equiv,
        share,
        pricing.alpha_per_hour_to_miners if pricing and pricing.usable else None,
        pricing.alpha_usd if pricing and pricing.usable else None,
        SERVING_GPU_HOUR_USD,
        SERVING_EMISSION_SHARE_CAP,
        release.model_id if release else None,
        release.release_id if release else None,
        release.runtime_pin if release else None,
        release.model_sha256 if release else None,
        release.model_file if release else None,
        release.runtime_image if release else None,
        release.attest_image if release else None,
    )
    miners = []
    for uid, w in windows.items():
        until = float(w.get('quarantined_until', 0.0) or 0.0)
        miners.append(
            (
                validator_hotkey,
                round_ts,
                int(uid),
                str(w.get('hotkey', '')),
                str(w.get('model_id', '')),
                str(w.get('release_id', '') or w.get('model_id', '')),
                str(w.get('status', 'probation')),
                float(w.get('mean', 0.0)),
                int(w.get('n_audits', 0)),
                bool(w.get('passed', False)),
                dt.datetime.fromtimestamp(until, dt.timezone.utc) if until > 0 else None,
                int(w.get('served', 0)),
                float(w.get('credit', 0.0)),
                w.get('ttft_ms'),
                w.get('decode_tps'),
                1.0 if w.get('attested') else 0.0,  # the capacity column predates per-token pay: admission only
                float(w.get('score', 0.0)),
                float(settled.get(str(w.get('hotkey', '')), 0.0)),
                (str(w.get('last_miss', '') or '')[:500]) or None,
            )
        )
    return summary, miners


class ServingRoundStorage:
    """Writes one audit round per call; reconnects lazily after a failure."""

    def __init__(self, retention_days: int = SERVING_DB_RETENTION_DAYS):
        self.retention_days = retention_days
        self._conn: Optional[Any] = None

    def _connection(self) -> Optional[Any]:
        if self._conn is None or getattr(self._conn, 'closed', False):
            self._conn = create_database_connection()
        return self._conn

    def store_round(
        self,
        validator_hotkey: str,
        state: ServingState,
        pricing: Optional[ServingPricing],
        release: Optional[ServingRelease] = None,
        now: Optional[dt.datetime] = None,
        allow_unpriced_cap: bool = False,
    ) -> bool:
        last_round = dict(state.last_round)
        if not last_round or not state.last_round_ts:
            return False
        round_ts = now or dt.datetime.fromtimestamp(state.last_round_ts, dt.timezone.utc)
        summary, miners = round_rows(
            validator_hotkey, round_ts, last_round, state.settled_scores(), pricing, release, allow_unpriced_cap
        )
        conn = self._connection()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(INSERT_SERVING_ROUND, summary)
                if miners:
                    cur.executemany(BULK_INSERT_SERVING_MINER_ROUNDS, miners)
                cur.execute(PRUNE_SERVING_ROUNDS, (validator_hotkey, self.retention_days))
                cur.execute(PRUNE_SERVING_MINER_ROUNDS, (validator_hotkey, self.retention_days))
            conn.commit()
            bt.logging.debug(f'Serving: persisted round {round_ts:%H:%M:%S} ({len(miners)} miner rows)')
            return True
        except Exception as e:  # never let persistence fail a round
            bt.logging.warning(f'Serving: could not persist round to the database: {e!r}')
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self._conn = None
            return False

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
