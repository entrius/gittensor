# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``public/fleet.json``: the pool's state as the website may show it.

The controller's state files carry what must never leave the host (a box's address and ports, host keys, container
and image ids, raw GPU UUIDs, error text that embeds an address). This module builds a **sanitized** document from
the same sources ``gitt controller status`` reads and writes it atomically to ``<state-dir>/public/fleet.json``;
das-gittensor serves that one file through a read-only mount of ``public/`` only. Nothing here is copied through
wholesale: every published field is named below, and every string is either ours (a state, a standing level) or held
to a strict pattern (a check name, an event kind, an entry id), so a new private field on a record cannot leak by
default. The validator does not read this file; pay comes from the signed scorecard alone.

The pay assembly ``status`` and this document share (the last scorecard as the validator would check it, what the
ledger settled since it) lives here too, so both read it one way.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.state import BENCHED, BoxState
from gittensor.controller.manifest import gpu_type_of
from gittensor.controller.pay.ledger import Ledger, is_withheld
from gittensor.controller.pay.rates import RatesError, load_rates
from gittensor.controller.pay.scorecard import LATEST, ScorecardError, read_scorecard
from gittensor.controller.standing import HARD, RELEASED, SOFT, standing

SCHEMA = 1
PUBLIC_DIR = 'public'
FLEET_FILE = 'fleet.json'

_NAME = re.compile(r'^[a-z0-9_]{1,64}$')  # a check name, a standing event kind
_ENTRY = re.compile(r'^[a-z0-9][a-z0-9._-]{0,127}@[0-9]{1,9}$')  # a registry entry id, name@version
_IMAGE = re.compile(
    r'^[a-z0-9][a-z0-9._/-]{0,199}(:[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?$'
)  # repo[:tag], no registry port
_GPU = re.compile(r'^[A-Za-z0-9_-]{1,32}$')  # a GPU type, e.g. RTX5090
_BENCH_KINDS = (HARD | SOFT) - {RELEASED}


# ---------------------------------------------------------------- pay, as `status` and the public document read it ---


def scorecard_view(root: Path, now: float) -> dict:
    """The last scorecard, checked the way the validator checks it: ``{}`` when none was written yet."""
    path = root / 'scorecard' / LATEST
    if not path.exists():
        return {}
    try:
        doc, sha = read_scorecard(path, now)
        view = {'valid': True, 'error': '', 'sha256': sha}
    except ScorecardError as e:
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            return {'valid': False, 'error': str(e), 'path': str(path)}
        view = {'valid': False, 'error': str(e), 'sha256': None}
    return {**view, 'path': str(path), 'scorecard': doc}


def live_pay(root: Path, boxes: Mapping[str, BoxState], view: dict, now: float) -> dict[str, dict]:
    """Per hotkey, what the ledger has settled since the last scorecard (or over the trailing window when there is
    none): idle / leased / withheld seconds and the USD they imply at the scorecard's implied per-card-hour rates
    (the table's target rates for a GPU type the scorecard did not price). A card LEASED since the scorecard shows
    its seconds here, not 0 (Kimbo 9/16)."""
    doc = view.get('scorecard') or {}
    since = float(doc['issued_at']) if doc.get('issued_at') is not None else now - cfg.SETTLEMENT_WINDOW_S
    implied = (doc.get('pool') or {}).get('implied_usd_per_card_hour') or {}
    try:
        table = load_rates()
    except RatesError:
        table = {}

    def rate(gpu: str) -> tuple[float, float]:
        if gpu in implied:
            return float(implied[gpu]['idle']), float(implied[gpu]['leased'])
        row = table.get(gpu)
        return (row.idle_usd_per_hr, row.leased_usd_per_hr) if row else (0.0, 0.0)

    out: dict[str, dict] = {}
    for row in Ledger(root / 'ledger').rows(since, now):
        live = out.setdefault(
            row.hotkey, {'since': since, 'idle_s': 0.0, 'leased_s': 0.0, 'withheld_s': 0.0, 'usd': 0.0}
        )
        withheld = row.withheld or (row.leased_s > 0 and is_withheld(boxes.get(row.hotkey), row.t1))
        leased = 0.0 if withheld else row.leased_s
        idle_rate, leased_rate = rate(row.gpu)
        live['idle_s'] += row.idle_s
        live['leased_s'] += leased
        live['withheld_s'] += row.leased_s - leased
        live['usd'] += (row.idle_s * idle_rate + leased * leased_rate) / 3600.0
    for live in out.values():
        for key in ('idle_s', 'leased_s', 'withheld_s'):
            live[key] = round(live[key], 3)
        live['usd'] = round(live['usd'], 6)
    return out


def pay_entry(scored: dict, live: dict | None, age_s: float | None) -> dict:
    """A box's pay as `status` shows it: the last scorecard's window, the ledger since it, and the two summed."""
    entry = {k: scored.get(k) for k in ('weight', 'usd', 'idle_s', 'leased_s', 'withheld_s')}
    entry['scorecard_age_s'] = age_s
    entry['live'] = live or {}
    entry['total'] = {
        k: round(float(scored.get(k) or 0.0) + float((live or {}).get(k) or 0.0), 6 if k == 'usd' else 3)
        for k in ('usd', 'idle_s', 'leased_s', 'withheld_s')
    }
    return entry


# ---------------------------------------------------------------- the public document -------------------------------


def card_hash(uuid: str) -> str:
    """A card's public name: raw GPU UUIDs never appear in published evidence (Kimbo 9/15). Unsalted on purpose, so
    a card keeps one name from one document to the next (the scorecard's per-document salt is for the validator)."""
    return hashlib.sha256(uuid.encode()).hexdigest()[:12]


def public_image(image: str | None) -> str | None:
    """``repo:tag`` of a registry image reference: the digest is dropped, anything that does not look like a plain
    repository (a registry host with a port, say) is not published."""
    ref = (image or '').split('@', 1)[0]
    return ref if _IMAGE.match(ref) else None


def _names(values: Any) -> list[str]:
    return [v for v in (values or []) if isinstance(v, str) and _NAME.match(v)]


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _last_event(events: list[dict], kinds: frozenset[str] | None = None) -> dict | None:
    for event in reversed(events or []):
        kind, at = event.get('kind'), _num(event.get('at'))
        if isinstance(kind, str) and _NAME.match(kind) and (kinds is None or kind in kinds):
            return {'at': at, 'kind': kind}
    return None


def _rates(implied: Mapping[str, Any]) -> dict[str, dict]:
    """Per GPU type, the idle / leased USD per card-hour: what the last scorecard implied (what was really paid),
    else the rate table's targets."""
    try:
        table = load_rates()
    except RatesError:
        table = {}
    out: dict[str, dict] = {}
    for gpu, row in table.items():
        out[gpu] = {
            'idle_usd_per_card_hour': row.idle_usd_per_hr,
            'leased_usd_per_card_hour': row.leased_usd_per_hr,
            'source': 'table',
        }
    for gpu, row in implied.items():
        idle, leased = _num((row or {}).get('idle')), _num((row or {}).get('leased'))
        if _GPU.match(str(gpu)) and idle is not None and leased is not None:
            out[str(gpu)] = {'idle_usd_per_card_hour': idle, 'leased_usd_per_card_hour': leased, 'source': 'scorecard'}
    return out


def _card(uuid: str, card: Any, record: Any, image_of: Callable[[str], str | None], now: float) -> dict:
    out: dict[str, Any] = {'card': card_hash(uuid), 'state': card.state, 'since': card.since}
    if record is None:
        return out
    entry = record.entry if _ENTRY.match(record.entry or '') else None
    leased_at = _num(record.leased_at)
    out.update(
        {
            'workload': entry,
            'image': public_image(image_of(entry)) if entry else None,
            'leased_at': leased_at,
            'uptime_s': round(max(0.0, now - leased_at), 1) if leased_at is not None else None,
            'healthy': bool(record.healthy),
            'draining': bool(record.draining),
            'heartbeat_misses': int(record.heartbeat_misses or 0),
            'last_heartbeat_at': _num(record.last_heartbeat_at),
        }
    )
    return out


def build_fleet(
    root: Path,
    boxes: Mapping[str, BoxState],
    instances: Mapping[str, Any],
    status: Mapping[str, Any],
    running: bool,
    now: float,
    image_of: Callable[[str], str | None] | None = None,
    network: str | None = None,
    netuid: int | None = None,
    publish_interval_s: float = cfg.PUBLISH_INTERVAL_S,
) -> dict:
    """The public document. ``boxes`` / ``instances`` are the controller's records, ``status`` its
    ``controller.json``, ``image_of(entry_id)`` the registry's image reference (None: unknown)."""
    image_of = image_of or (lambda entry: None)
    view = scorecard_view(root, now)
    doc = view.get('scorecard') or {}
    paid = {h['hotkey']: h for h in doc.get('hotkeys', []) if isinstance(h, dict) and 'hotkey' in h}
    issued_at = _num(doc.get('issued_at'))
    age_s = round(now - issued_at, 1) if issued_at is not None else None
    live = live_pay(root, boxes, view, now)
    by_state: dict[str, int] = {}
    rows = []
    for box in sorted(boxes.values(), key=lambda b: b.box_id):
        cards = []
        for uuid, card in sorted(box.cards.items(), key=lambda item: card_hash(item[0])):
            record = instances.get(card.instance_id) if card.instance_id else None
            if record is not None and (record.box != box.box_id or record.uuid != uuid):
                record = None
            cards.append(_card(uuid, card, record, image_of, now))
            by_state[card.state] = by_state.get(card.state, 0) + 1
        scored, since = paid.get(box.box_id) or {}, live.get(box.box_id)
        pay = None
        if scored or since:
            total = pay_entry(scored, since, age_s)['total']
            pay = {
                'weight': _num(scored.get('weight')) or 0.0,
                'idle_h': round(total['idle_s'] / 3600.0, 4),
                'leased_h': round(total['leased_s'] / 3600.0, 4),
                'usd_window': total['usd'],
            }
        benched = box.status == BENCHED
        bench_event = _last_event(box.standing_events, _BENCH_KINDS) if benched else None
        rows.append(
            {
                'hotkey': box.box_id,
                'uid': None,  # the controller reads no UIDs off the chain; the API may fill it from its own data
                'status': box.status,
                'standing': standing(box.standing_events, now),
                'gpu_type': gpu_type_of(box.card_name) if box.card_name else None,
                'card_count': len(cards),
                'last_check_at': box.last_check_at,
                'last_failed': _names(box.last_failed),
                'bench_until': box.bench_until if benched else None,
                'benched_reason': bench_event['kind'] if bench_event else None,
                'pay': pay,
                'last_event': _last_event(box.standing_events),
                'cards': cards,
            }
        )
    last_round = status.get('round') or {}
    intervals = status.get('intervals') or {}
    oracle = doc.get('oracle') or {}
    return {
        'schema': SCHEMA,
        'generated_at': now,
        'network': network,
        'netuid': netuid,
        'controller': {
            'running': bool(running),
            'round_n': last_round.get('n') if isinstance(last_round.get('n'), int) else None,
            'last_round_at': _num(last_round.get('finished_at')),
            'round_interval_s': _num(intervals.get('round_s')),
            'publish_interval_s': publish_interval_s,
        },
        'scorecard': {
            'sha256': view.get('sha256'),
            'issued_at': issued_at,
            'valid_until': _num(doc.get('valid_until')),
            'valid': bool(view.get('valid')),
            'recycle_share': _num(doc.get('recycle_share')),
        }
        if view
        else None,
        'rates': _rates((doc.get('pool') or {}).get('implied_usd_per_card_hour') or {}),
        'oracle': {
            'tao_usd': _num(oracle.get('tao_usd')),
            'alpha_tao': _num(oracle.get('alpha_tao')),
            'held': bool(oracle.get('held')),
        }
        if oracle
        else None,
        'totals': {'boxes': len(rows), 'cards': sum(r['card_count'] for r in rows), 'cards_by_state': by_state},
        'boxes': rows,
    }


def fleet_path(root: Path) -> Path:
    return root / PUBLIC_DIR / FLEET_FILE


def write_fleet(root: Path, doc: dict) -> Path:
    """Write the document atomically (tmp + rename, so a reader never sees half a file). ``public/`` is 0755 and the
    file 0644 inside a 0700 state directory: the API reads it through a bind mount of ``public/`` alone."""
    path = fleet_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    tmp = path.with_name(f'.{FLEET_FILE}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True, allow_nan=False) + '\n')
    os.chmod(tmp, 0o644)
    tmp.replace(path)
    return path


class Publisher:
    """The running controller's writer. The watch tick asks ``due()`` and writes every ``interval_s``, so a card that
    changed state shows within one interval and ``generated_at`` tells a reader the controller is alive; the
    scorecard tick writes at once (``force``), so the hash on the page is never an interval behind."""

    def __init__(self, root: Path, interval_s: float = cfg.PUBLISH_INTERVAL_S, wall: Callable[[], float] = time.time):
        self.root, self.interval_s, self.wall = root, interval_s, wall
        self.last_at: float | None = None

    def due(self, force: bool = False) -> bool:
        return force or self.last_at is None or self.wall() - self.last_at >= self.interval_s

    def write(self, doc: dict) -> Path:
        self.last_at = self.wall()
        return write_fleet(self.root, doc)
