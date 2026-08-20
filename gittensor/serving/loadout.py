# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving loadout: which model/backend the fleet should be running.

Beta stub of the validator-published loadout schedule. The schedule is a
repo-pinned JSON file (same distribution rail as master_repositories.json)
that miner and validator both load, so both sides agree on the model and
backend by construction. ``SERVING_LOADOUT_PATH`` overrides the file (e.g.
``serving_loadout.echo.json`` for a GPU-free localnet). Replacing this loader
with a validator-signed, traffic-driven schedule is the planned upgrade path.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import bittensor as bt

WEIGHTS_DIR = Path(__file__).parent.parent / 'validator' / 'weights'
DEFAULT_LOADOUT_PATH = WEIGHTS_DIR / 'serving_loadout.json'
ECHO_LOADOUT_PATH = WEIGHTS_DIR / 'serving_loadout.echo.json'


@dataclass
class ServingLoadout:
    model_id: str
    backend: str
    max_tokens: int = 64
    base_url: Optional[str] = None
    runtime_pin: Optional[str] = None
    audit_bank: Optional[str] = None  # filename under weights/, or None for echo-derived audits
    request_timeout: float = 60.0


def resolve_loadout_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return path
    env = os.getenv('SERVING_LOADOUT_PATH')
    return Path(env) if env else DEFAULT_LOADOUT_PATH


def load_serving_loadout(path: Optional[Path] = None) -> ServingLoadout:
    loadout_path = resolve_loadout_path(path)
    with open(loadout_path) as f:
        raw = json.load(f)
    loadout = ServingLoadout(
        model_id=raw['model_id'],
        backend=raw['backend'],
        max_tokens=int(raw.get('max_tokens', 64)),
        base_url=raw.get('base_url'),
        runtime_pin=raw.get('runtime_pin'),
        audit_bank=raw.get('audit_bank'),
        request_timeout=float(raw.get('request_timeout', 60.0)),
    )
    bt.logging.info(f'Serving loadout: model={loadout.model_id} backend={loadout.backend} pin={loadout.runtime_pin}')
    return loadout
