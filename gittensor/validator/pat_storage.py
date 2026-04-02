# Entrius 2025

"""Thread-safe JSON storage for miner GitHub PATs.

Validators store PATs received via PatBroadcastSynapse in ~/.gittensor/miner_pats.json.
The scoring loop snapshots the full file once per round via load_all_pats(); mid-round
broadcasts update the file but do not affect the current scoring round.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PATS_FILE = Path.home() / '.gittensor' / 'miner_pats.json'

_lock = threading.Lock()


def load_all_pats() -> list[dict]:
    """Read all stored PAT entries. Returns empty list if file is missing or corrupt."""
    with _lock:
        return _read_file()


def save_pat(hotkey: str, uid: int, pat: str, github_id: str) -> None:
    """Upsert a PAT entry by hotkey. Creates the file and parent directory if needed."""
    with _lock:
        entries = _read_file()

        # Upsert by hotkey
        entry = {
            'hotkey': hotkey,
            'uid': uid,
            'pat': pat,
            'github_id': github_id,
            'stored_at': datetime.now(timezone.utc).isoformat(),
        }

        for i, existing in enumerate(entries):
            if existing.get('hotkey') == hotkey:
                entries[i] = entry
                break
        else:
            entries.append(entry)

        _write_file(entries)


def get_pat_by_hotkey(hotkey: str) -> Optional[dict]:
    """Look up a single PAT entry by hotkey. Returns None if not found."""
    with _lock:
        for entry in _read_file():
            if entry.get('hotkey') == hotkey:
                return entry
        return None


def remove_pat(hotkey: str) -> bool:
    """Remove a PAT entry by hotkey. Returns True if an entry was removed."""
    with _lock:
        entries = _read_file()
        filtered = [e for e in entries if e.get('hotkey') != hotkey]
        if len(filtered) == len(entries):
            return False
        _write_file(filtered)
        return True


def _read_file() -> list[dict]:
    """Read and parse the JSON file. Must be called while holding _lock."""
    if not PATS_FILE.exists():
        return []
    try:
        return json.loads(PATS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _write_file(entries: list[dict]) -> None:
    """Write entries to JSON file. Must be called while holding _lock."""
    PATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATS_FILE.write_text(json.dumps(entries, indent=2))
