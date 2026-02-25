# Entrius 2025

"""SQLite storage for merge predictions.

Each validator stores predictions independently. One row per miner per PR.
Thread-safe via WAL mode — the axon handler writes while the scoring loop reads.
DB file lives at repo root (predictions.db) for easy Docker volume mounting.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bittensor as bt

from gittensor.constants import PREDICTIONS_COOLDOWN_SECONDS

# DB at repo root — easy to find in Docker, gitignored
DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[3] / 'gt-merge-preds.db')


class PredictionStorage:
    """Thread-safe SQLite storage for merge predictions."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or DEFAULT_DB_PATH
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS predictions (
                    uid             INTEGER NOT NULL,
                    hotkey          TEXT    NOT NULL,
                    github_id       TEXT    NOT NULL,
                    issue_id        INTEGER NOT NULL,
                    repository      TEXT    NOT NULL,
                    pr_number       INTEGER NOT NULL,
                    prediction      REAL    NOT NULL,
                    timestamp       TEXT    NOT NULL,
                    variance_at_prediction REAL,
                    PRIMARY KEY (uid, hotkey, github_id, issue_id, pr_number)
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_predictions_issue
                ON predictions (issue_id)
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_predictions_miner_issue
                ON predictions (uid, hotkey, issue_id)
            ''')
            conn.commit()
        bt.logging.info(f'Prediction storage initialized at {self._db_path}')

    def check_cooldown(self, uid: int, hotkey: str, issue_id: int, pr_number: int) -> Optional[float]:
        """Return seconds remaining on cooldown, or None if no cooldown active."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT timestamp FROM predictions WHERE uid = ? AND hotkey = ? AND issue_id = ? AND pr_number = ?',
                (uid, hotkey, issue_id, pr_number),
            ).fetchone()

        if row is None:
            return None

        last_ts = datetime.fromisoformat(row['timestamp'])
        elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
        remaining = PREDICTIONS_COOLDOWN_SECONDS - elapsed
        return remaining if remaining > 0 else None

    def get_miner_total_for_issue(self, uid: int, hotkey: str, issue_id: int, exclude_pr: Optional[int] = None) -> float:
        """Get sum of a miner's existing predictions for an issue, optionally excluding a PR being updated."""
        with self._get_connection() as conn:
            if exclude_pr is not None:
                row = conn.execute(
                    'SELECT COALESCE(SUM(prediction), 0.0) as total FROM predictions '
                    'WHERE uid = ? AND hotkey = ? AND issue_id = ? AND pr_number != ?',
                    (uid, hotkey, issue_id, exclude_pr),
                ).fetchone()
            else:
                row = conn.execute(
                    'SELECT COALESCE(SUM(prediction), 0.0) as total FROM predictions '
                    'WHERE uid = ? AND hotkey = ? AND issue_id = ?',
                    (uid, hotkey, issue_id),
                ).fetchone()
        return float(row['total'])

    def compute_current_variance(self, issue_id: int) -> float:
        """Compute avg variance across all PRs for an issue (used for consensus bonus)."""
        with self._get_connection() as conn:
            rows = conn.execute(
                '''
                SELECT pr_number, AVG(prediction) as mean_pred,
                       AVG(prediction * prediction) - AVG(prediction) * AVG(prediction) as var_pred
                FROM predictions
                WHERE issue_id = ?
                GROUP BY pr_number
                ''',
                (issue_id,),
            ).fetchall()

        if not rows:
            return 0.0

        variances = [max(0.0, float(r['var_pred'])) for r in rows]
        return sum(variances) / len(variances)

    def store_prediction(
        self,
        uid: int,
        hotkey: str,
        github_id: str,
        issue_id: int,
        repository: str,
        pr_number: int,
        prediction: float,
        variance_at_prediction: float,
    ) -> None:
        """Insert or replace a single PR prediction. Resets timestamp on that PR only."""
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    '''
                    INSERT INTO predictions (uid, hotkey, github_id, issue_id, repository, pr_number, prediction, timestamp, variance_at_prediction)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (uid, hotkey, github_id, issue_id, pr_number)
                    DO UPDATE SET prediction = excluded.prediction,
                                  timestamp = excluded.timestamp,
                                  variance_at_prediction = excluded.variance_at_prediction
                    ''',
                    (uid, hotkey, github_id, issue_id, repository, pr_number, prediction, now, variance_at_prediction),
                )
                conn.commit()

    def get_predictions_for_issue(self, issue_id: int) -> list[dict]:
        """Get all predictions for an issue (used at settlement)."""
        with self._get_connection() as conn:
            rows = conn.execute(
                'SELECT * FROM predictions WHERE issue_id = ? ORDER BY uid, pr_number',
                (issue_id,),
            ).fetchall()
        return [dict(r) for r in rows]
