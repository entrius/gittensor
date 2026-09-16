# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The pay ledger (vault ``24`` §3 WS-F, ``23`` §7, §7a): per card, per block, from the state the controller recorded.

**Accrual** (``accrue``, every ``SETTLEMENT_TICK_S`` from the daemon's watch loop). For each card on an IDLE box:

* **idle** seconds while the card is IDLE and its box's last proof passed and is fresh (``IDLE_PROOF_MAX_AGE_S``; a box
  SSH could not reach last round has no passing proof), from the moment the card became IDLE;
* **leased** seconds from its instance's pay span (``InstanceRecord.pay_from`` .. ``pay_through``), which the watch
  keeps: it starts at the first passing probe after the canary, runs through the older of the last passing heartbeat
  and health probe while all four pay conditions hold, closes at any failure or miss (so pay stops at the last passing
  check) and never runs past the controller's stop (``stopped_at``). A span can confirm seconds a little after they
  happened, so each instance keeps its own cursor and every second is paid once. A multi-card instance's cards share
  one span and are paid only while every one of them is LEASED (or DRAINING after our stop): all or nothing;
* nothing in STARTING, CHECKING, or on an ADMIT or BENCHED box;
* **withheld**: leased seconds of a box whose ``withheld_from`` falls in [its UTC day − 1 day, its UTC day + 1 day)
  are recorded but never paid. The window is re-applied when a window is settled, so a hard failure also forfeits the
  leased seconds already accrued that day and the day before.

Rows go to ``<ledger>/<UTC date>.jsonl``, one per card per tick (the row schema is ``LedgerRow``), with a per-day rollup
beside them (``<UTC date>.rollup.json``) and the cursors in ``cursor.json``.

**The window** (``settle_window``): over the trailing ``SETTLEMENT_WINDOW_S``, each hotkey's USD is
``Σ cards (idle_s × idle_rate + leased_s × leased_rate) / 3600``, rates from ``fleet_pay.json``. One weighted pool:

* per GPU type, above ``target_fleet`` accruing cards (card-seconds over the window) everyone of that type dilutes by
  ``target_fleet / cards``; at or below it each card is paid its target rate;
* the pool is the compute share of the miners' alpha over the window, priced by the oracle; if the fleet's target pay
  is worth more than that, everyone scales down together; what is not paid is ``recycle_share``.

A hotkey's weight is its share of the compute pool (the weights and ``recycle_share`` sum to 1). Every scaling applies
to idle and leased alike, so leased > idle always holds.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gittensor.constants import OSS_EMISSION_SHARE
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.state import DRAINING, IDLE, LEASED, BoxState
from gittensor.controller.manifest import gpu_type_of
from gittensor.controller.pay.oracle import Quote
from gittensor.controller.pay.rates import GpuRate

DAY_S = 86_400.0
COMPUTE_SHARE = 1.0 - OSS_EMISSION_SHARE  # the part of miner weights the compute pool pays (0.10)


def utc_day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d')


def withheld_window(
    withheld_from: float, days_before: int = cfg.WITHHELD_DAYS_BEFORE, days_after: int = cfg.WITHHELD_DAYS_AFTER
) -> tuple[float, float]:
    day = math.floor(withheld_from / DAY_S) * DAY_S
    return day - days_before * DAY_S, day + days_after * DAY_S


def is_withheld(box: BoxState | None, t: float) -> bool:
    if box is None or box.withheld_from is None:
        return False
    start, end = withheld_window(box.withheld_from)
    return start <= t < end


@dataclass
class LedgerRow:
    """One card over one settlement tick: the seconds that tick confirmed, not a sample."""

    t0: float  # the previous settlement
    t1: float  # this settlement
    hotkey: str
    uuid: str
    gpu: str  # the rate-table row ("RTX5090"), from the pinned card name
    state: str  # the card's state at t1
    instance: str  # the instance on the card, '' when none
    idle_s: float
    leased_s: float
    withheld: bool  # the box's withheld window covered t1: leased_s is recorded, never paid


@dataclass
class Cursors:
    settled_at: float | None = None
    leased: dict[str, float] = field(default_factory=dict)  # instance id -> paid through


def _paid_span(record: Any, cursor: float | None, now: float) -> tuple[float, float]:
    """The part of an instance's pay span not paid yet: (start, end); empty when end <= start."""
    if record.pay_from is None or record.pay_through is None:
        return 0.0, 0.0
    start = max(record.pay_from, cursor if cursor is not None else -math.inf)
    end = min(record.pay_through, record.stopped_at if record.stopped_at is not None else math.inf, now)
    return start, end


def accrue(
    boxes: Iterable[BoxState],
    instances: Mapping[str, Any],
    cursors: Cursors,
    now: float,
    idle_proof_max_age_s: float = cfg.IDLE_PROOF_MAX_AGE_S,
) -> list[LedgerRow]:
    """One settlement tick over every card: the rows, with ``cursors`` advanced. ``instances`` maps instance id to its
    ``InstanceRecord`` (anything with the same fields)."""
    t0 = cursors.settled_at if cursors.settled_at is not None else now
    rows: list[LedgerRow] = []
    live: set[str] = set()
    for box in sorted(boxes, key=lambda b: b.box_id):
        if box.status != IDLE:
            continue
        gpu = gpu_type_of(box.card_name)
        withheld = is_withheld(box, now)
        proof_good_until = (
            box.last_check_at + idle_proof_max_age_s
            if box.last_check_at is not None and box.unreachable_count == 0
            else -math.inf
        )
        leased_by_instance: dict[str, float] = {}
        for instance_id in sorted({c.instance_id for c in box.cards.values() if c.instance_id}):
            record = instances.get(instance_id)
            if record is None or record.box != box.box_id:
                continue
            live.add(instance_id)
            # The instance's cards: its record's card(s) and every card bound to it. All or nothing: one of them not
            # (or no longer) bound to it and LEASED, and none is paid. A card that left keeps no instance id, so the
            # record's own list is what catches it (``uuids`` once placement runs multi-card instances).
            declared = {record.uuid, *(getattr(record, 'uuids', None) or ())}
            declared |= {u for u, c in box.cards.items() if c.instance_id == instance_id}
            if any(
                box.card(u).instance_id != instance_id or box.card(u).state not in (LEASED, DRAINING) for u in declared
            ):
                continue
            start, end = _paid_span(record, cursors.leased.get(instance_id), now)
            if end > start:
                leased_by_instance[instance_id] = end - start
                cursors.leased[instance_id] = end
        for uuid, card in sorted(box.cards.items()):
            idle_s = 0.0
            if card.state == IDLE:
                start = max(t0, card.since if card.since is not None else t0)
                idle_s = max(0.0, min(now, proof_good_until) - start)
            leased_s = leased_by_instance.get(card.instance_id, 0.0) if card.instance_id else 0.0
            rows.append(
                LedgerRow(
                    t0, now, box.box_id, uuid, gpu, card.state, card.instance_id,
                    round(idle_s, 3), round(leased_s, 3), withheld and leased_s > 0,
                )
            )  # fmt: skip
    cursors.leased = {k: v for k, v in cursors.leased.items() if k in live or k in instances}
    cursors.settled_at = now
    return rows


class Ledger:
    """The append-only JSON-lines ledger under ``<state-dir>/ledger``."""

    def __init__(self, root: str | Path, tick_s: float = cfg.SETTLEMENT_TICK_S):
        self.root, self.tick_s = Path(root), tick_s
        self._lock = threading.Lock()
        self.cursors = Cursors()
        path = self.root / 'cursor.json'
        if path.exists():
            try:
                raw = json.loads(path.read_text() or '{}')
                self.cursors = Cursors(raw.get('settled_at'), dict(raw.get('leased') or {}))
            except (OSError, ValueError):
                pass  # a torn cursor file: start a new tick, pay nothing for the gap
        self._rollups: dict[str, dict] = {}

    def due(self, now: float) -> bool:
        return self.cursors.settled_at is None or now - self.cursors.settled_at >= self.tick_s

    def settle(self, boxes: Iterable[BoxState], instances: Mapping[str, Any], now: float) -> list[LedgerRow]:
        """One tick: accrue, append the rows, update the day's rollup and the cursors. The first tick ever (or after a
        lost cursor file) only sets the clock."""
        with self._lock:
            first = self.cursors.settled_at is None
            rows = accrue(boxes, instances, self.cursors, now)
            self.root.mkdir(parents=True, exist_ok=True)
            if rows and not first:
                day = utc_day(now)
                with open(self.root / f'{day}.jsonl', 'a') as handle:
                    for row in rows:
                        handle.write(json.dumps(asdict(row), separators=(',', ':')) + '\n')
                self._roll(day, rows, now)
            self._write(self.root / 'cursor.json', asdict(self.cursors))
            return [] if first else rows

    def _roll(self, day: str, rows: list[LedgerRow], now: float) -> None:
        path = self.root / f'{day}.rollup.json'
        rollup = self._rollups.get(day)
        if rollup is None:
            try:
                rollup = json.loads(path.read_text()) if path.exists() else None
            except (OSError, ValueError):
                rollup = None
            rollup = rollup or {'day': day, 'hotkeys': {}}
            self._rollups = {day: rollup}  # only today's stays in memory
        for row in rows:
            card = (
                rollup['hotkeys']
                .setdefault(row.hotkey, {})
                .setdefault(row.uuid, {'gpu': row.gpu, 'idle_s': 0.0, 'leased_s': 0.0, 'withheld_s': 0.0})
            )
            card['idle_s'] = round(card['idle_s'] + row.idle_s, 3)
            card['withheld_s' if row.withheld else 'leased_s'] = round(
                card['withheld_s' if row.withheld else 'leased_s'] + row.leased_s, 3
            )
        rollup['updated_at'] = now
        self._write(path, rollup)

    @staticmethod
    def _write(path: Path, doc: dict) -> None:
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.replace(path)

    def rows(self, start: float, end: float) -> list[LedgerRow]:
        """Rows settled in (start, end]."""
        out = []
        day = math.floor(start / DAY_S) * DAY_S
        while day <= end:
            path = self.root / f'{utc_day(day)}.jsonl'
            if path.exists():
                for line in path.read_text().splitlines():
                    try:
                        row = LedgerRow(**json.loads(line))
                    except (TypeError, ValueError):
                        continue  # a torn last line after a crash
                    if start < row.t1 <= end:
                        out.append(row)
            day += DAY_S
        return out


# ---------------------------------------------------------------- the window ----------------------------------------


@dataclass
class HotkeyPay:
    hotkey: str
    idle_s: float = 0.0
    leased_s: float = 0.0
    withheld_s: float = 0.0
    usd: float = 0.0  # what the window paid, after dilution and the pool bound
    weight: float = 0.0  # share of the compute pool
    gpu_types: list[str] = field(default_factory=list)


@dataclass
class GpuPay:
    gpu: str
    idle_s: float = 0.0
    leased_s: float = 0.0
    cards: float = 0.0  # average accruing cards over the window
    target_usd: float = 0.0  # at the table's rates, before any scaling
    fleet_scale: float = 1.0  # target_fleet / cards above target, else 1
    idle_usd_per_hr: float = 0.0  # implied, after every scaling
    leased_usd_per_hr: float = 0.0


@dataclass
class Settlement:
    start: float
    end: float
    quote: Quote
    compute_share: float
    pool_alpha: float
    pool_usd: float
    target_usd: float  # the fleet's pay at the table's rates, after dilution above target_fleet
    paid_usd: float
    afford: float  # pool_usd / target_usd, capped at 1: everyone's scale when the pool cannot pay the targets
    recycle_share: float
    hotkeys: dict[str, HotkeyPay]
    gpus: dict[str, GpuPay]
    unrated_s: float = 0.0  # card-seconds on a GPU type the table has no row for: unpaid

    def as_dict(self) -> dict:
        doc = asdict(self)
        doc['quote'] = self.quote.as_dict()
        return doc


def settle_window(
    rows: Iterable[LedgerRow],
    boxes: Mapping[str, BoxState],
    rates: Mapping[str, GpuRate],
    quote: Quote,
    start: float,
    end: float,
    compute_share: float = COMPUTE_SHARE,
    miner_alpha_per_block: float = cfg.MINER_ALPHA_PER_BLOCK,
    block_s: float = cfg.BLOCK_S,
) -> Settlement:
    seconds = max(end - start, 1e-9)
    hotkeys: dict[str, HotkeyPay] = {}
    gpus: dict[str, GpuPay] = {}
    target_by: dict[tuple[str, str], float] = {}  # (hotkey, gpu) -> USD at the table's rates
    unrated = 0.0
    for row in rows:
        pay = hotkeys.setdefault(row.hotkey, HotkeyPay(row.hotkey))
        rate = rates.get(row.gpu)
        if rate is None:
            unrated += row.idle_s + row.leased_s
            continue
        if row.gpu not in pay.gpu_types:
            pay.gpu_types.append(row.gpu)
        withheld = row.withheld or (row.leased_s > 0 and is_withheld(boxes.get(row.hotkey), row.t1))
        leased_s = 0.0 if withheld else row.leased_s
        pay.idle_s += row.idle_s
        pay.leased_s += leased_s
        pay.withheld_s += row.leased_s - leased_s
        gpu = gpus.setdefault(row.gpu, GpuPay(row.gpu))
        gpu.idle_s += row.idle_s
        gpu.leased_s += leased_s
        usd = (row.idle_s * rate.idle_usd_per_hr + leased_s * rate.leased_usd_per_hr) / 3600.0
        gpu.target_usd += usd
        target_by[(row.hotkey, row.gpu)] = target_by.get((row.hotkey, row.gpu), 0.0) + usd

    for gpu in gpus.values():
        gpu.cards = (gpu.idle_s + gpu.leased_s) / seconds
        target_fleet = rates[gpu.gpu].target_fleet
        gpu.fleet_scale = min(1.0, target_fleet / gpu.cards) if gpu.cards > 0 else 1.0

    pool_alpha = miner_alpha_per_block * seconds / block_s * compute_share
    pool_usd = pool_alpha * quote.alpha_usd
    target_usd = sum(g.target_usd * g.fleet_scale for g in gpus.values())
    afford = min(1.0, pool_usd / target_usd) if target_usd > 0 else 1.0
    for gpu in gpus.values():
        rate = rates[gpu.gpu]
        gpu.idle_usd_per_hr = rate.idle_usd_per_hr * gpu.fleet_scale * afford
        gpu.leased_usd_per_hr = rate.leased_usd_per_hr * gpu.fleet_scale * afford
    for (hotkey, gpu_type), usd in target_by.items():
        paid = usd * gpus[gpu_type].fleet_scale * afford
        hotkeys[hotkey].usd += paid
        hotkeys[hotkey].weight += paid / pool_usd if pool_usd > 0 else 0.0
    paid_usd = target_usd * afford
    recycle = max(0.0, 1.0 - sum(h.weight for h in hotkeys.values()))
    return Settlement(
        start, end, quote, compute_share, pool_alpha, pool_usd, target_usd, paid_usd, afford, recycle,
        dict(sorted(hotkeys.items())), gpus, unrated,
    )  # fmt: skip
