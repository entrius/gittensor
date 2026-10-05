# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The signed scorecard (vault ``23`` §8a, ``26`` §1, §10 item 4): what the validator reads, signs, commits and sets
weights from. The controller holds no chain key; it only writes the document.

Every ``SCORECARD_INTERVAL_S`` the daemon writes ``<state-dir>/scorecard/latest.json`` (canonical JSON: sorted keys, no
whitespace) and its sha256 in ``latest.sha256``, plus a dated copy under ``scorecard/<UTC date>/`` as evidence. The
document: the window, the rate table and ``target_fleet``, the oracle price it used, the pool it sized, and per hotkey
its weight (share of the compute pool), idle and leased seconds, withheld seconds, standing, box status, last full check
and GPU type, with each card as ``{uuid_hash, state}``. **GPU UUIDs never appear raw**: each is
``sha256(salt:uuid)`` with a fresh per-scorecard ``salt`` published in the document, so a card cannot be followed from
one scorecard to the next, and inside one it is still counted once.

**TTL (our call, 26 §10 item 4):** ``valid_until = issued_at + SCORECARD_TTL_INTERVALS (2) x SCORECARD_INTERVAL_S``,
i.e. 40 min: one missed write is tolerated, a second is a dead controller. A validator refuses a scorecard past
``valid_until`` (or whose sha256 does not match) and recycles the compute share: a dead controller never keeps paying.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from gittensor.controller.checks import config as cfg

SCHEMA = 'gt-compute-scorecard/1'
LATEST = 'latest.json'
FUTURE_SKEW_S = 300.0  # a scorecard issued further in the future than this is refused (a clock gone wrong)
WEIGHT_TOLERANCE = 1e-6


class ScorecardError(ValueError):
    """The scorecard cannot be used: missing, tampered with, stale, or malformed."""


def canonical_bytes(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def uuid_hash(salt: str, uuid: str) -> str:
    return hashlib.sha256(f'{salt}:{uuid}'.encode()).hexdigest()


def build_scorecard(
    settlement,
    boxes: Mapping,
    rates: Mapping,
    now: float,
    interval_s: float = cfg.SCORECARD_INTERVAL_S,
    ttl_intervals: int = cfg.SCORECARD_TTL_INTERVALS,
    salt: str | None = None,
) -> dict:
    """``settlement`` is a ``pay.ledger.Settlement``; ``boxes`` maps hotkey -> ``BoxState``."""
    from gittensor.controller.manifest import gpu_type_of
    from gittensor.controller.pay.ledger import is_withheld
    from gittensor.controller.standing import standing

    salt = salt or secrets.token_hex(16)
    hotkeys = []
    for hotkey in sorted(set(boxes) | set(settlement.hotkeys)):
        box = boxes.get(hotkey)
        pay = settlement.hotkeys.get(hotkey)
        gpu_type = (gpu_type_of(box.card_name) if box is not None else '') or (pay.gpu_types[0] if pay else '')
        withheld_s = pay.withheld_s if pay else 0.0
        hotkeys.append(
            {
                'hotkey': hotkey,
                'weight': pay.weight if pay else 0.0,
                'usd': round(pay.usd, 6) if pay else 0.0,
                'idle_s': round(pay.idle_s, 3) if pay else 0.0,
                'leased_s': round(pay.leased_s, 3) if pay else 0.0,
                'withheld_s': round(withheld_s, 3),
                'withheld': withheld_s > 0 or is_withheld(box, now),
                'standing': standing(box.standing_events, now) if box is not None else '',
                'status': box.status if box is not None else '',
                'last_check_at': box.last_check_at if box is not None else None,
                'gpu_type': gpu_type,
                'cards': [
                    {'uuid_hash': uuid_hash(salt, uuid), 'state': card.state}
                    for uuid, card in sorted(box.cards.items(), key=lambda item: uuid_hash(salt, item[0]))
                ]
                if box is not None
                else [],
            }
        )
    return {
        'schema': SCHEMA,
        'issued_at': now,
        'valid_until': now + ttl_intervals * interval_s,
        'interval_s': interval_s,
        'window': {'start': settlement.start, 'end': settlement.end, 'seconds': settlement.end - settlement.start},
        'rates': {
            g: {'idle_usd_per_hr': r.idle_usd_per_hr, 'leased_usd_per_hr': r.leased_usd_per_hr}
            for g, r in rates.items()
        },
        'target_fleet': {g: r.target_fleet for g, r in rates.items()},
        'oracle': {
            k: v for k, v in settlement.quote.as_dict().items() if k in ('tao_usd', 'alpha_tao', 'at', 'source', 'held')
        },
        'pool': {
            'compute_share': settlement.compute_share,
            'alpha': settlement.pool_alpha,
            'usd': round(settlement.pool_usd, 6),
            'target_usd': round(settlement.target_usd, 6),
            'paid_usd': round(settlement.paid_usd, 6),
            'afford': settlement.afford,
            'unrated_s': round(settlement.unrated_s, 3),
            'implied_usd_per_card_hour': {
                g.gpu: {
                    'idle': round(g.idle_usd_per_hr, 6),
                    'leased': round(g.leased_usd_per_hr, 6),
                    'cards': round(g.cards, 3),
                    'fleet_scale': g.fleet_scale,
                }
                for g in settlement.gpus.values()
            },
        },
        'salt': salt,
        'hotkeys': hotkeys,
        'recycle_share': settlement.recycle_share,
    }


def write_scorecard(root: str | Path, doc: dict) -> tuple[Path, str]:
    """``latest.json`` + ``latest.sha256`` (written sha last, so a reader never pairs a new body with an old hash that
    matches), and a dated copy. Returns the path and the sha256."""
    root = Path(root)
    body = canonical_bytes(doc)
    sha = hashlib.sha256(body).hexdigest()
    issued = datetime.fromtimestamp(float(doc['issued_at']), timezone.utc)
    archive = root / issued.strftime('%Y-%m-%d')
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f'{issued.strftime("%H%M%S")}-{sha[:12]}.json').write_bytes(body)
    path = root / LATEST
    for target, data in ((path, body), (path.with_suffix('.sha256'), f'{sha}  {LATEST}\n'.encode())):
        tmp = target.with_name(target.name + '.tmp')
        tmp.write_bytes(data)
        tmp.replace(target)
    return path, sha


def read_scorecard(path: str | Path, now: float) -> tuple[dict, str]:
    """The document and its sha256, or ``ScorecardError``: the sha256 beside it must match the bytes, the schema must
    be ours, ``valid_until`` must not have passed, and the weights plus ``recycle_share`` must be a split of one pool."""
    path = Path(path)
    try:
        body = path.read_bytes()
        expected = path.with_suffix('.sha256').read_text().split()[0].lower()
    except (OSError, IndexError) as e:
        raise ScorecardError(f'{path}: {e}') from e
    sha = hashlib.sha256(body).hexdigest()
    if sha != expected:
        raise ScorecardError(f'{path}: sha256 {sha[:16]}… does not match latest.sha256 {expected[:16]}…')
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise ScorecardError(f'{path}: not JSON: {e}') from e
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA:
        raise ScorecardError(f'{path}: not a {SCHEMA} document')
    try:
        issued_at, valid_until = float(doc['issued_at']), float(doc['valid_until'])
        recycle = float(doc['recycle_share'])
        weights = {str(h['hotkey']): float(h['weight']) for h in doc['hotkeys']}
    except (KeyError, TypeError, ValueError) as e:
        raise ScorecardError(f'{path}: malformed: {e!r}') from e
    if now >= valid_until:
        raise ScorecardError(f'{path}: stale: valid until {valid_until:.0f}, now {now:.0f}')
    if issued_at > now + FUTURE_SKEW_S:
        raise ScorecardError(f'{path}: issued in the future ({issued_at:.0f} > {now:.0f})')
    values = [*weights.values(), recycle]
    if any(not math.isfinite(v) or v < 0 or v > 1 for v in values) or abs(sum(values) - 1.0) > WEIGHT_TOLERANCE:
        raise ScorecardError(f'{path}: weights + recycle_share are not a split of one pool (sum {sum(values):.6f})')
    return doc, sha
