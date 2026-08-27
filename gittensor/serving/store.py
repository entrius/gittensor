# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Durable serving state for the validator: one SQLite file next to ``state.npz``.

Everything the audit loop otherwise keeps only in RAM — audit windows and quarantines, probe readings, the
trailing settlement history, dormancy counters — is written after every round in one transaction, and read
back at startup. A validator restart therefore neither resets every miner to probation nor zeroes settled pay.
SQLite (stdlib, WAL) rather than JSON so a crash mid-write cannot leave a truncated file behind.
"""

import json
import sqlite3
from collections import deque
from pathlib import Path
from typing import Optional

from gittensor.serving.state import ServingState

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_values (hotkey TEXT, model_id TEXT, seq INTEGER, value REAL,
    PRIMARY KEY (hotkey, model_id, seq));
CREATE TABLE IF NOT EXISTS quarantine (hotkey TEXT, model_id TEXT, until REAL, PRIMARY KEY (hotkey, model_id));
CREATE TABLE IF NOT EXISTS probe_history (hotkey TEXT, seq INTEGER, tps REAL, PRIMARY KEY (hotkey, seq));
CREATE TABLE IF NOT EXISTS round_history (hotkey TEXT, seq INTEGER, score REAL, PRIMARY KEY (hotkey, seq));
CREATE TABLE IF NOT EXISTS dormant (hotkey TEXT PRIMARY KEY, rounds INTEGER);
"""


class ServingStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0)
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def save(self, state: ServingState) -> None:
        """Snapshot the audit-thread state in one transaction (the tables are small: a few rows per hotkey)."""
        audits = state.audits.to_dict()
        with self._connect() as db:
            for table in ('audit_values', 'quarantine', 'probe_history', 'round_history', 'dormant'):
                db.execute(f'DELETE FROM {table}')
            db.executemany(
                'INSERT INTO audit_values VALUES (?, ?, ?, ?)',
                [(hk, mid, i, x) for hk, mid, xs in audits['values'] for i, x in enumerate(xs)],
            )
            db.executemany('INSERT INTO quarantine VALUES (?, ?, ?)', audits['quarantine'])
            db.executemany(
                'INSERT INTO probe_history VALUES (?, ?, ?)',
                [(hk, i, x) for hk, xs in state.probe_history.items() for i, x in enumerate(xs)],
            )
            db.executemany(
                'INSERT INTO round_history VALUES (?, ?, ?)',
                [(hk, i, x) for hk, xs in state._history.items() for i, x in enumerate(xs)],
            )
            db.executemany('INSERT INTO dormant VALUES (?, ?)', list(state.dormant_rounds.items()))

    def load(self, state: ServingState) -> ServingState:
        """Restore a snapshot into ``state``; an empty or unreadable store leaves it untouched."""
        try:
            with self._connect() as db:
                values = db.execute('SELECT hotkey, model_id, value FROM audit_values ORDER BY hotkey, model_id, seq')
                for hk, mid, x in values:
                    state.audits.record(hk, mid, x)
                for hk, mid, until in db.execute('SELECT hotkey, model_id, until FROM quarantine'):
                    state.audits._quarantine[(hk, mid)] = float(until)
                for hk, x in db.execute('SELECT hotkey, tps FROM probe_history ORDER BY hotkey, seq'):
                    state.probe_history.setdefault(hk, deque(maxlen=3)).append(float(x))
                for hk, x in db.execute('SELECT hotkey, score FROM round_history ORDER BY hotkey, seq'):
                    state._history.setdefault(hk, deque(maxlen=state.settlement_rounds)).append(float(x))
                for hk, rounds in db.execute('SELECT hotkey, rounds FROM dormant'):
                    state.dormant_rounds[hk] = int(rounds)
        except sqlite3.Error:
            pass
        return state

    def migrate_json(self, legacy: Path) -> bool:
        """One-time import of the pre-SQLite ``serving_audits.json`` window; the file is renamed afterwards."""
        if not legacy.exists():
            return False
        try:
            raw = json.loads(legacy.read_text())
        except (OSError, ValueError):
            return False
        state = ServingState()
        for hk, mid, xs in raw.get('values', []):
            for x in xs:
                state.audits.record(str(hk), str(mid), float(x))
        for hk, mid, until in raw.get('quarantine', []):
            state.audits._quarantine[(str(hk), str(mid))] = float(until)
        self.save(state)
        legacy.rename(legacy.with_suffix('.json.migrated'))
        return True


def serving_store_path(full_path: Optional[str]) -> Optional[Path]:
    """Where serving state lives: next to state.npz in the neuron's state dir, or None when the neuron has none."""
    return Path(full_path) / 'serving.db' if full_path else None
