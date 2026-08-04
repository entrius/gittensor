# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving loadout: which model/backend the fleet should be running.

Beta stub of the validator-published loadout schedule. For now the schedule is
a repo-pinned JSON file (same distribution rail as master_repositories.json)
that miner and validator both load, so both sides agree on the model and
backend by construction. Replacing this loader with a validator-signed,
traffic-EMA-driven schedule is the planned upgrade path.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import bittensor as bt

DEFAULT_LOADOUT_PATH = Path(__file__).parent.parent / 'validator' / 'weights' / 'serving_loadout.json'


@dataclass
class ServingLoadout:
    model_id: str
    backend: str
    max_tokens: int = 64
    base_url: Optional[str] = None
    runtime_pin: Optional[str] = None


def load_serving_loadout(path: Optional[Path] = None) -> ServingLoadout:
    loadout_path = path or DEFAULT_LOADOUT_PATH
    with open(loadout_path) as f:
        raw = json.load(f)
    loadout = ServingLoadout(
        model_id=raw['model_id'],
        backend=raw['backend'],
        max_tokens=int(raw.get('max_tokens', 64)),
        base_url=raw.get('base_url'),
        runtime_pin=raw.get('runtime_pin'),
    )
    bt.logging.info(f'Serving loadout: model={loadout.model_id} backend={loadout.backend} pin={loadout.runtime_pin}')
    return loadout
