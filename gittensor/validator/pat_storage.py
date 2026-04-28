# Entrius 2025

"""Thread-safe JSON storage for miner GitHub PATs.

Validators store PATs received via PatBroadcastSynapse in miner_pats.json at the project root.
The scoring loop snapshots the full file once per round via load_all_pats(); mid-round
broadcasts update the file but do not affect the current scoring round.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PATS_FILE = Path(__file__).resolve().parents[2] / 'data' / 'miner_pats.json'

_lock = threading.Lock()
_logger = logging.getLogger(__name__)


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
    """Upsert a PAT entry by UID. Creates the file if needed.

    Raises json.JSONDecodeError / OSError if PATS_FILE exists but is unreadable;
    we refuse to overwrite a corrupt file so a partial-write or on-disk corruption
    cannot permanently destroy stored PATs.
    """
    with _lock:
        entries = _read_file(raise_on_corrupt=True)

        entry = {
            'uid': uid,
            'hotkey': hotkey,
            'pat': pat,
            'github_id': github_id,
            'stored_at': datetime.now(timezone.utc).isoformat(),
        }

        for i, existing in enumerate(entries):
            if existing.get('uid') == uid:
                entries[i] = entry
                break
        else:
            entries.append(entry)

        _write_file(entries)


def get_pat_by_uid(uid: int) -> Optional[dict]:
    """Look up a single PAT entry by UID. Returns None if not found."""
    with _lock:
        for entry in _read_file():
            if entry.get('uid') == uid:
                return entry
        return None


def remove_pat(uid: int) -> bool:
    """Remove a PAT entry by UID. Returns True if an entry was removed.

    Raises json.JSONDecodeError / OSError if PATS_FILE exists but is unreadable
    (same refuse-to-overwrite invariant as save_pat).
    """
    with _lock:
        entries = _read_file(raise_on_corrupt=True)
        filtered = [e for e in entries if e.get('uid') != uid]
        if len(filtered) == len(entries):
            return False
        _write_file(filtered)
        return True


def _read_file(*, raise_on_corrupt: bool = False) -> list[dict]:
    """Read and parse the JSON file. Must be called while holding _lock.

    Read paths (load_all_pats, get_pat_by_uid) keep the default and degrade to
    an empty list with a warning, so a single corrupt file does not crash the
    validator scoring round. Write paths (save_pat, remove_pat) pass
    raise_on_corrupt=True so they surface the error rather than overwriting
    an unreadable file with a fresh entry list.
    """
    if not PATS_FILE.exists():
        return []
    try:
        return json.loads(PATS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        if raise_on_corrupt:
            _logger.error('PATS_FILE %s is unreadable; refusing to overwrite: %s', PATS_FILE, e)
            raise
        _logger.warning('PATS_FILE %s is unreadable; treating as empty for read path: %s', PATS_FILE, e)
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
