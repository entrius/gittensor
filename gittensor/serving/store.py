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
CREATE TABLE IF NOT EXISTS audit_values (hotkey TEXT, release_id TEXT, seq INTEGER, value REAL,
    PRIMARY KEY (hotkey, release_id, seq));
CREATE TABLE IF NOT EXISTS quarantine (hotkey TEXT, release_id TEXT, until REAL, strikes INTEGER DEFAULT 0,
    PRIMARY KEY (hotkey, release_id));
CREATE TABLE IF NOT EXISTS probe_history (hotkey TEXT, seq INTEGER, tps REAL, PRIMARY KEY (hotkey, seq));
CREATE TABLE IF NOT EXISTS round_history (hotkey TEXT, seq INTEGER, score REAL, PRIMARY KEY (hotkey, seq));
CREATE TABLE IF NOT EXISTS dormant (hotkey TEXT PRIMARY KEY, rounds INTEGER);
CREATE TABLE IF NOT EXISTS attest (hotkey TEXT PRIMARY KEY, status TEXT);
CREATE TABLE IF NOT EXISTS uuid_owner (uuid TEXT PRIMARY KEY, hotkey TEXT, round INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS last_credit (hotkey TEXT PRIMARY KEY, credit REAL);
"""


class ServingStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            self._migrate(db)
            db.executescript(SCHEMA)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Pre-release_id stores keyed the window by model_id; the values carry over under the same key."""
        for table in ('audit_values', 'quarantine'):
            columns = [row[1] for row in db.execute(f'PRAGMA table_info({table})')]
            if 'model_id' in columns:
                db.execute(f'ALTER TABLE {table} RENAME COLUMN model_id TO release_id')
            if table == 'quarantine' and columns and 'strikes' not in columns:
                db.execute('ALTER TABLE quarantine ADD COLUMN strikes INTEGER DEFAULT 0')

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0)
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def save(self, state: ServingState) -> None:
        """Snapshot the audit-thread state in one transaction (the tables are small: a few rows per hotkey)."""
        audits = state.audits.to_dict()
        with self._connect() as db:
            for table in (
                'audit_values',
                'quarantine',
                'probe_history',
                'round_history',
                'dormant',
                'attest',
                'uuid_owner',
                'meta',
                'last_credit',
            ):
                db.execute(f'DELETE FROM {table}')
            db.executemany('INSERT INTO last_credit VALUES (?, ?)', list(state.last_credit.items()))
            db.executemany(
                'INSERT INTO uuid_owner VALUES (?, ?, ?)',
                [(uuid, hk, rnd) for uuid, (hk, rnd) in state.uuid_owner.items()],
            )
            db.execute('INSERT INTO meta VALUES (?, ?)', ('attest_round', str(int(state.attest_round))))
            db.executemany(
                'INSERT INTO audit_values VALUES (?, ?, ?, ?)',
                [(hk, rid, i, x) for hk, rid, xs in audits['values'] for i, x in enumerate(xs)],
            )
            strikes = {(hk, rid): n for hk, rid, n in audits['strikes']}
            keys = {(hk, rid) for hk, rid, _ in audits['quarantine']} | set(strikes)
            until = {(hk, rid): t for hk, rid, t in audits['quarantine']}
            db.executemany(
                'INSERT INTO quarantine VALUES (?, ?, ?, ?)',
                [(hk, rid, until.get((hk, rid), 0.0), strikes.get((hk, rid), 0)) for hk, rid in sorted(keys)],
            )
            db.executemany(
                'INSERT INTO probe_history VALUES (?, ?, ?)',
                [(hk, i, x) for hk, xs in state.probe_history.items() for i, x in enumerate(xs)],
            )
            db.executemany(
                'INSERT INTO round_history VALUES (?, ?, ?)',
                [(hk, i, x) for hk, xs in state._history.items() for i, x in enumerate(xs)],
            )
            db.executemany('INSERT INTO dormant VALUES (?, ?)', list(state.dormant_rounds.items()))
            db.executemany(
                'INSERT INTO attest VALUES (?, ?)', [(hk, json.dumps(st)) for hk, st in state.attest_status.items()]
            )

    def load(self, state: ServingState) -> ServingState:
        """Restore a snapshot into ``state``; an empty or unreadable store leaves it untouched."""
        try:
            with self._connect() as db:
                values = db.execute(
                    'SELECT hotkey, release_id, value FROM audit_values ORDER BY hotkey, release_id, seq'
                )
                for hk, rid, x in values:
                    state.audits.record(hk, rid, x)
                for hk, rid, until, strikes in db.execute('SELECT hotkey, release_id, until, strikes FROM quarantine'):
                    if float(until) > 0.0:
                        state.audits._quarantine[(hk, rid)] = float(until)
                    if int(strikes or 0) > 0:
                        state.audits._strikes[(hk, rid)] = int(strikes)
                for hk, x in db.execute('SELECT hotkey, tps FROM probe_history ORDER BY hotkey, seq'):
                    state.probe_history.setdefault(hk, deque(maxlen=3)).append(float(x))
                for hk, x in db.execute('SELECT hotkey, score FROM round_history ORDER BY hotkey, seq'):
                    state._history.setdefault(hk, deque(maxlen=state.settlement_rounds)).append(float(x))
                for hk, rounds in db.execute('SELECT hotkey, rounds FROM dormant'):
                    state.dormant_rounds[hk] = int(rounds)
                for hk, st in db.execute('SELECT hotkey, status FROM attest'):
                    state.attest_status[hk] = json.loads(st)
                for uuid, hk, rnd in db.execute('SELECT uuid, hotkey, round FROM uuid_owner'):
                    state.uuid_owner[str(uuid)] = (str(hk), int(rnd))
                for key, value in db.execute('SELECT key, value FROM meta'):
                    if key == 'attest_round':
                        state.attest_round = int(value)
                for hk, credit in db.execute('SELECT hotkey, credit FROM last_credit'):
                    state.last_credit[str(hk)] = float(credit)
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
