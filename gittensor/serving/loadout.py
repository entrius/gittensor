# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving loadout: the list of blessed releases the fleet may run.

A *release* is one ``(model_id, runtime_pin)`` pair plus how to talk to it
(backend / base_url) and how a validator verifies it (``reference_url`` for a
live reference runtime on the validator's own GPU, ``audit_bank`` for a
snapshot fallback). Adding a model or runtime to the subnet = running a
conformant copy (see ``docs/serving-runtime-contract.md``) and appending a
release here. The verifier never knows which model it is looking at.

The loadout is a repo-pinned JSON file (same distribution rail as
master_repositories.json) that miner and validator both load, so both sides
agree on the releases by construction. ``SERVING_LOADOUT_PATH`` overrides the
file (``serving_loadout.echo.json`` for a GPU-free localnet);
``SERVING_REFERENCE_URL`` (+ ``SERVING_REFERENCE_API_KEY``) points the
primary release at a reference runtime — on the validator's own GPU or any
reachable conformant copy, e.g. a rented 5090. Replacing this loader with a validator-signed,
traffic-driven schedule (Gepetto-lite) is the planned upgrade path.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import bittensor as bt

WEIGHTS_DIR = Path(__file__).parent.parent / 'validator' / 'weights'
DEFAULT_LOADOUT_PATH = WEIGHTS_DIR / 'serving_loadout.json'
ECHO_LOADOUT_PATH = WEIGHTS_DIR / 'serving_loadout.echo.json'


@dataclass
class ServingRelease:
    model_id: str
    backend: str
    max_tokens: int = 64
    base_url: Optional[str] = None  # miner side: the runtime this miner serves from
    runtime_pin: Optional[str] = None
    model_sha256: Optional[str] = None  # digest of the model file; runtime_pin + model_sha256 = the release
    audit_bank: Optional[str] = None  # validator side: snapshot reference, filename under weights/
    reference_url: Optional[str] = (
        None  # validator side: live reference runtime (own GPU or a rented one); wins over audit_bank
    )
    reference_api_key: Optional[str] = None  # bearer for a remote reference (sparkinfer --api-key)
    request_timeout: float = 60.0

    @classmethod
    def from_dict(cls, raw: dict) -> 'ServingRelease':
        return cls(
            model_id=raw['model_id'],
            backend=raw['backend'],
            max_tokens=int(raw.get('max_tokens', 64)),
            base_url=raw.get('base_url'),
            runtime_pin=raw.get('runtime_pin'),
            model_sha256=raw.get('model_sha256'),
            audit_bank=raw.get('audit_bank'),
            reference_url=raw.get('reference_url'),
            reference_api_key=raw.get('reference_api_key'),
            request_timeout=float(raw.get('request_timeout', 60.0)),
        )


@dataclass
class ServingLoadout:
    releases: List[ServingRelease]

    def __post_init__(self) -> None:
        if not self.releases:
            raise ValueError('serving loadout has no releases')
        ids = [r.model_id for r in self.releases]
        if len(set(ids)) != len(ids):
            raise ValueError(f'duplicate model_id in serving loadout: {ids}')

    @property
    def primary(self) -> ServingRelease:
        """The first release: what the inference API serves and what a miner runs unless SERVING_RELEASE says otherwise."""
        return self.releases[0]

    def get(self, model_id: str) -> ServingRelease:
        for release in self.releases:
            if release.model_id == model_id:
                return release
        raise KeyError(f'release {model_id!r} not in serving loadout: {[r.model_id for r in self.releases]}')


def resolve_loadout_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return path
    env = os.getenv('SERVING_LOADOUT_PATH')
    return Path(env) if env else DEFAULT_LOADOUT_PATH


def load_serving_loadout(path: Optional[Path] = None) -> ServingLoadout:
    loadout_path = resolve_loadout_path(path)
    with open(loadout_path) as f:
        raw = json.load(f)
    entries = raw['releases'] if isinstance(raw, dict) and 'releases' in raw else [raw]
    loadout = ServingLoadout(releases=[ServingRelease.from_dict(entry) for entry in entries])

    reference_override = os.getenv('SERVING_REFERENCE_URL')
    if reference_override:
        loadout.primary.reference_url = reference_override
    key_override = os.getenv('SERVING_REFERENCE_API_KEY')
    if key_override:
        loadout.primary.reference_api_key = key_override

    for release in loadout.releases:
        bt.logging.info(
            f'Serving release: model={release.model_id} backend={release.backend} pin={release.runtime_pin} '
            f'reference={"live " + release.reference_url if release.reference_url else (release.audit_bank or "echo")}'
        )
    return loadout
