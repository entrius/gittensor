# Entrius 2025

"""Thread-safe JSON storage for miner GitHub PATs.

Validators store PATs received via PatBroadcastSynapse in miner_pats.json at the project root.
The scoring loop snapshots the full file once per round via load_all_pats(); mid-round
broadcasts update the file but do not affect the current scoring round.

The store location defaults to data/miner_pats.json at the project root and can be
relocated by setting the GITTENSOR_MINER_PATS_FILE env var to an exact file path.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bittensor as bt

# Where the PAT store lives. Defaults to data/miner_pats.json at the project root;
# validators who keep subnet data elsewhere can override the exact file path via
# the GITTENSOR_MINER_PATS_FILE env var.
_DEFAULT_PATS_FILE = Path(__file__).resolve().parents[2] / 'data' / 'miner_pats.json'
PATS_FILE = (
    Path(os.environ['GITTENSOR_MINER_PATS_FILE']) if os.environ.get('GITTENSOR_MINER_PATS_FILE') else _DEFAULT_PATS_FILE
)

_lock = threading.Lock()
_last_known_good_pats: Optional[list[dict]] = None


def _snapshot_entries(entries: list[dict]) -> list[dict]:
    """Return a defensive copy and remember it as the last successful read."""
    global _last_known_good_pats
    copied = [entry.copy() for entry in entries]
    _last_known_good_pats = copied
    return copied


def ensure_pats_file() -> None:
    """Create the PATs file with an empty list if it doesn't exist. Called on validator boot."""
    with _lock:
        if not PATS_FILE.exists():
            _write_file([])
            _snapshot_entries([])


def load_all_pats() -> list[dict]:
    """Snapshot all stored PAT entries for a scoring round.

    Read-only and deliberately tolerant: an unreadable store here must not crash
    the round (an unhandled error would stop the validator) nor wipe anything.
    On a transient read failure, returns the last successful snapshot so miners
    are not all treated as PAT-less for the round. The *write* path (save_pat)
    is the one that fails closed.
    """
    with _lock:
        try:
            return _snapshot_entries(_read_file())
        except (json.JSONDecodeError, OSError) as e:
            if _last_known_good_pats is not None:
                bt.logging.warning(
                    f'miner_pats.json unreadable this round; using last known good PAT snapshot '
                    f'({len(_last_known_good_pats)} miners) until the store recovers: {e}'
                )
                return [entry.copy() for entry in _last_known_good_pats]
            bt.logging.error(
                f'miner_pats.json unreadable and no prior PAT snapshot exists; '
                f'scoring with no stored PATs until it recovers: {e}'
            )
            return []


def save_pat(uid: int, hotkey: str, pat: str, github_id: str) -> None:
    """Upsert a PAT entry by UID, failing closed on an unreadable store.

    If the existing store cannot be read (corrupt file or a transient I/O error),
    this raises *without writing*, so a single failed read can never erase every
    other miner's stored PAT (the read-then-overwrite wipe). The broadcast handler
    catches this and rejects the broadcast, so the miner retries and nothing is lost.
    """
    with _lock:
        entries = _read_file()  # fail closed: a failed read raises here, before any write

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
        _snapshot_entries(entries)


def get_pat_by_uid(uid: int) -> Optional[dict]:
    """Look up a single PAT entry by UID. Returns None if not found.

    Propagates (does not swallow) a read error, so callers never mistake an
    unreadable store for 'no PAT stored for this miner'.
    """
    with _lock:
        for entry in _read_file():
            if entry.get('uid') == uid:
                return entry
        return None


def _read_file() -> list[dict]:
    """Read and parse the JSON store. Must be called while holding _lock.

    Returns [] only when the file genuinely does not exist. Raises
    (json.JSONDecodeError / OSError) on a corrupt or unreadable file so the write
    path never mistakes a failed read for an empty store and overwrites it. The
    read paths that must tolerate a transient failure (load_all_pats) catch it.
    """
    if not PATS_FILE.exists():
        return []
    return json.loads(PATS_FILE.read_text())


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
