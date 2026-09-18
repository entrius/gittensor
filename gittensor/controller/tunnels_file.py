# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``tunnels.json``: written by the tunnel keeper (``tunnels.py``), read by the gateway. Standard library only, so the
gateway reads it without importing anything that deals in SSH or certificates."""

from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA = 1
TUNNELS_FILE = 'tunnels.json'
# The keeper rewrites the file every pass (~3 s) whatever its boxes are doing; a file older than this means no keeper
# is running, and every tunnel in it counts as down.
TUNNELS_STALE_S = 10.0


class TunnelsFileError(ValueError):
    pass


def load_tunnels(path: str | Path) -> dict:
    """The document, or ``TunnelsFileError`` saying why there is none (missing, unreadable, another schema)."""
    path = Path(path)
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        raise TunnelsFileError(f'{path.name}: missing') from None
    except (OSError, ValueError) as e:
        raise TunnelsFileError(f'{path.name}: {type(e).__name__}: {e}') from None
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA:
        found = doc.get('schema') if isinstance(doc, dict) else type(doc).__name__
        raise TunnelsFileError(f'{path.name}: schema {found!r}, expected {SCHEMA}')
    if not isinstance(doc.get('tunnels'), dict):
        raise TunnelsFileError(f'{path.name}: no tunnels object')
    return doc


def read_tunnels(path: str | Path) -> dict | None:
    try:
        return load_tunnels(path)
    except TunnelsFileError:
        return None


def write_atomic(path: Path, doc: dict) -> None:
    """tmp beside the file, then rename: a reader sees the old file or the new one, never a partial one."""
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, path)
