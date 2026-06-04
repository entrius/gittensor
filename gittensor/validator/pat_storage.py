# Entrius 2025

"""Thread-safe JSON storage for miner GitHub PATs.

Validators store PATs received via PatBroadcastSynapse in miner_pats.json at the project root.
The scoring loop snapshots the full file once per round via load_all_pats(); mid-round
broadcasts update the file but do not affect the current scoring round.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PATS_FILE = Path(__file__).resolve().parents[2] / 'data' / 'miner_pats.json'

_lock = threading.Lock()


def ensure_pats_file() -> None:
    """Create the PATs file with an empty list if it doesn't exist. Called on validator boot."""
    with _lock:
        if not PATS_FILE.exists():
            _write_file([])


def load_all_pats() -> list[dict]:
    """Read all stored PAT entries. Returns empty list if file is missing or corrupt."""
    with _lock:
        return _read_file()


def save_pat(uid: int, hotkey: str, pat: str, github_id: str) -> None:
    """Upsert a PAT entry, keyed by HOTKEY (one record per hotkey).

    The record is keyed by the stable hotkey rather than the reusable UID slot
    so a hotkey's GitHub identity pin survives UID churn. When this hotkey takes
    over a UID that another hotkey used to occupy, the previous occupant's UID is
    released (set to None) but its record — and therefore its identity pin — is
    retained, so a displaced hotkey stays locked to its original GitHub account.
    """
    with _lock:
        entries = _read_file()

        entry = {
            'uid': uid,
            'hotkey': hotkey,
            'pat': pat,
            'github_id': github_id,
            'stored_at': datetime.now(timezone.utc).isoformat(),
        }

        # The UID slot now belongs to `hotkey`; release it from any other hotkey
        # that previously occupied it, but keep that hotkey's identity pin.
        for existing in entries:
            if existing.get('uid') == uid and existing.get('hotkey') != hotkey:
                existing['uid'] = None

        for i, existing in enumerate(entries):
            if existing.get('hotkey') == hotkey:
                entries[i] = entry
                break
        else:
            entries.append(entry)

        _write_file(entries)


def get_pat_by_uid(uid: int) -> Optional[dict]:
    """Look up the current occupant of a UID slot. Returns None if not found.

    Released (detached) records carry uid=None, so they are never returned here.
    """
    with _lock:
        for entry in _read_file():
            if entry.get('uid') == uid:
                return entry
        return None


def get_pat_by_hotkey(hotkey: str) -> Optional[dict]:
    """Look up a hotkey's stored record, regardless of which UID slot it holds.

    This is the source of truth for GitHub identity pinning: the binding follows
    the stable hotkey, not the UID slot, so a hotkey cannot shed its pin by
    cycling through deregistration and re-registration onto a fresh UID.
    """
    with _lock:
        latest: Optional[dict] = None
        for entry in _read_file():
            if entry.get('hotkey') == hotkey and entry.get('github_id'):
                # Defensive against legacy files with multiple records per hotkey:
                # prefer the most recent one.
                if latest is None or entry.get('stored_at', '') >= latest.get('stored_at', ''):
                    latest = entry
        return latest


def _read_file() -> list[dict]:
    """Read and parse the JSON file. Must be called while holding _lock."""
    if not PATS_FILE.exists():
        return []
    try:
        return json.loads(PATS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _write_file(entries: list[dict]) -> None:
    """Atomically write entries to JSON file. Must be called while holding _lock."""
    PATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write to temp file then atomically replace to avoid partial reads
    fd, tmp_path = tempfile.mkstemp(dir=PATS_FILE.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp_path, PATS_FILE)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
