# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Serving loadout: the list of blessed releases the fleet may run.

A *release* is one ``(model_id, runtime_pin)`` pair plus how to talk to it
(backend / base_url) and how a validator verifies it (``reference_url`` for a
live reference runtime on the validator's own GPU, ``audit_bank`` for a
snapshot fallback). Adding a model or runtime to the subnet = running a
conformant copy (see the Serving Runtime Contract in the miner docs) and appending a
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
from typing import Dict, List, Optional

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
    runtime_image: Optional[str] = None  # the blessed image by digest (entrius/sparkinfer:<tag>@sha256:...)
    model_sha256: Optional[str] = None  # digest of the model file; runtime_pin + model_sha256 = the release
    model_file: Optional[str] = None  # HF path of the model file, informational (the digest is what is enforced)
    audit_bank: Optional[str] = None  # validator side: snapshot reference, filename under weights/
    reference_url: Optional[str] = (
        None  # validator side: live reference runtime (own GPU or a rented one); wins over audit_bank
    )
    reference_api_key: Optional[str] = None  # bearer for a remote reference (sparkinfer --api-key)
    request_timeout: float = 60.0
    # Speed of one honest card on this exact runtime, measured at blessing time (scripts/check_serving_runtime.py
    # --speed-json on the conformance GPU) and written by the pin-bump PR. The validator prices capacity and latency
    # against these, so the "one card" bar tracks the runtime as it gets faster. None -> the constants' defaults.
    decode_per_request: Optional[Dict[int, float]] = None  # concurrent requests -> per-request decode tok/s, one card
    # Attestation (docker/attest, image entrius/gt-attest — one container per box, beside the runtime). Miner side:
    # attest_url = that container (default: base_url host, port 8081). Validator side: attest_reference_url = the one
    # beside the reference (default: reference_url host, port 8081).
    attest_image: Optional[str] = None  # the attest container every box runs (entrius/gt-attest:<tag>)
    attest_url: Optional[str] = None
    attest_reference_url: Optional[str] = None
    attest_iters: Optional[int] = None
    vram_model_reserved_bytes: Optional[float] = None
    ttft_full_ms: Optional[float] = None  # validator-observed TTFT up to which latency credit is 1.0
    ttft_zero_ms: Optional[float] = None  # ... and at which it reaches 0.0

    @classmethod
    def from_dict(cls, raw: dict) -> 'ServingRelease':
        speed = raw.get('speed') or {}
        attest = raw.get('attest') or {}
        return cls(
            model_id=raw['model_id'],
            backend=raw['backend'],
            max_tokens=int(raw.get('max_tokens', 64)),
            base_url=raw.get('base_url'),
            runtime_pin=raw.get('runtime_pin'),
            runtime_image=raw.get('runtime_image'),
            model_file=raw.get('model_file'),
            model_sha256=raw.get('model_sha256'),
            audit_bank=raw.get('audit_bank'),
            reference_url=raw.get('reference_url'),
            reference_api_key=raw.get('reference_api_key'),
            request_timeout=float(raw.get('request_timeout', 60.0)),
            decode_per_request={int(k): float(v) for k, v in (speed.get('decode_per_request') or {}).items()} or None,
            attest_image=attest.get('image'),
            attest_url=attest.get('url') or _sidecar_url(raw.get('base_url')),
            attest_reference_url=attest.get('reference_url') or _sidecar_url(raw.get('reference_url')),
            attest_iters=int(attest['iters']) if attest.get('iters') else None,
            vram_model_reserved_bytes=_optional_float(attest.get('vram_model_reserved_bytes')),
            ttft_full_ms=_optional_float(speed.get('ttft_full_ms')),
            ttft_zero_ms=_optional_float(speed.get('ttft_zero_ms')),
        )


def _optional_float(value) -> Optional[float]:
    return float(value) if value is not None else None


def _sidecar_url(base: Optional[str], port: int = 8081) -> Optional[str]:
    """The attestation sidecar next to a runtime URL: same scheme and host, port 8081."""
    if not base:
        return None
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(base)
    host = parts.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    return urlunsplit((parts.scheme or 'http', f'{host}:{port}', '', '', ''))


@dataclass
class ServingLoadout:
    releases: List[ServingRelease]
    tao_usd: Optional[float] = None  # published TAO/USD rate (`pricing.tao_usd`); sizes the serving emission share

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
    pricing = raw.get('pricing') or {} if isinstance(raw, dict) else {}
    tao_usd_env = os.getenv('SERVING_TAO_USD')
    tao_usd = float(tao_usd_env) if tao_usd_env else (float(pricing['tao_usd']) if pricing.get('tao_usd') else None)
    loadout = ServingLoadout(releases=[ServingRelease.from_dict(entry) for entry in entries], tao_usd=tao_usd)

    reference_override = os.getenv('SERVING_REFERENCE_URL')
    if reference_override:
        loadout.primary.reference_url = reference_override
        loadout.primary.attest_reference_url = os.getenv('SERVING_ATTEST_REFERENCE_URL') or _sidecar_url(
            reference_override
        )
    key_override = os.getenv('SERVING_REFERENCE_API_KEY')
    if key_override:
        loadout.primary.reference_api_key = key_override

    lines = [
        f'Serving release: model={release.model_id} backend={release.backend} pin={release.runtime_pin} '
        f'reference={"live " + release.reference_url if release.reference_url else (release.audit_bank or "echo")}'
        for release in loadout.releases
    ]
    if lines != _last_logged:  # the loadout is re-read several times a round; announce it when it changes
        _last_logged[:] = lines
        for line in lines:
            bt.logging.info(line)
    return loadout


_last_logged: List[str] = []
