# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The order seam with gittensor-app (vault ``29`` §3): the controller PULLS the orders that need it and PUSHES one
status report per transition. Nothing here ever receives a call: the controller keeps no inbound endpoint (``26`` §1).

Each poll asks for ``requested,ending,active`` (the app fixed this reading: an extension moves ``ends_at`` on an
``active`` row, and only a poll that includes ``active`` sees it). A ``requested`` order we do not know becomes a
``RentalRecord`` under the app's id; an ``ending`` order ends ours, or is answered ``ended`` at once when we never
placed it (the customer stopped it while still ``requested``); an ``active`` row with a later ``ends_at`` extends ours.
Then every record whose state the app has not been told is reported: ``PATCH`` with the §3 body. The app treats a
repeated report as a no-op and a report behind its own state as facts only, so a lost answer costs one retry and
nothing else. Reports go out oldest first; a failed one stops the batch (the app is down, not one rental).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from gittensor.controller.rentals import (
    ACTIVE,
    ENDED,
    ENDING,
    REQUESTED,
    STARTING_R,
    RentalError,
    RentalRecord,
    RentalStore,
    order_end,
    place_order,
)

POLL_STATES = (REQUESTED, ENDING, ACTIVE)
TIMEOUT_S = 15.0
APP_ENDING_REASON = 'app_ending'  # the app's own reason (customer_stop / balance / ends_at) stands unless we fail it


class SeamError(Exception):
    pass


class SeamClient:
    """``GET /internal/rentals?state=…`` and ``PATCH /internal/rentals/:id`` with the bearer token (stdlib only)."""

    def __init__(self, base_url: str, token: str, timeout_s: float = TIMEOUT_S, opener=None):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout_s = timeout_s
        self._open = opener or urllib.request.urlopen

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                'Authorization': f'Bearer {self.token}',
                'Accept': 'application/json',
                **({'Content-Type': 'application/json'} if data is not None else {}),
            },
        )
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise SeamError(f'{method} {path}: HTTP {e.code}: {e.read()[:200]!r}') from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SeamError(f'{method} {path}: {type(e).__name__}: {e}') from e
        try:
            return json.loads(raw) if raw else {}
        except ValueError as e:
            raise SeamError(f'{method} {path}: not JSON: {raw[:200]!r}') from e

    def orders(self, states: Sequence[str] = POLL_STATES) -> list[dict]:
        doc = self._call('GET', f'/internal/rentals?state={",".join(states)}')
        rentals = doc.get('rentals') if isinstance(doc, dict) else None
        if not isinstance(rentals, list):
            raise SeamError('GET /internal/rentals: no "rentals" list in the answer')
        return [r for r in rentals if isinstance(r, dict) and isinstance(r.get('id'), str)]

    def report(self, rental_id: str, body: dict) -> dict:
        doc = self._call('PATCH', f'/internal/rentals/{rental_id}', body)
        return doc if isinstance(doc, dict) else {}


def report_body(r: RentalRecord) -> dict:
    """The §3 PATCH body for a record as it stands."""
    return {
        'state': r.state,
        'box_uid': r.box_uid,
        'host': r.host or None,
        'ports': {k: int(v) for k, v in (r.public_map or r.port_map).items()} or None,  # what the world dials
        'gpu_uuids': list(r.uuids) or None,
        'started_at': int(r.started_at) if r.started_at is not None else None,
        'ended_at': int(r.ended_at) if r.ended_at is not None else None,
        'reason': r.reason or '',
    }


@dataclass
class PollReport:
    seen: int = 0
    placed: list[str] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)  # ending orders taken (ours) or answered ended (never placed)
    extended: list[str] = field(default_factory=list)
    reported: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class RentalPoller:
    def __init__(self, store: RentalStore, client: SeamClient, wall: Callable[[], float] = time.time):
        self.store, self.client, self.wall = store, client, wall

    def take_orders(self) -> PollReport:
        """One GET, applied to the store."""
        report = PollReport()
        try:
            orders = self.client.orders()
        except SeamError as e:
            report.errors.append(str(e)[:300])
            return report
        report.seen = len(orders)
        now = self.wall()
        for o in orders:
            rid, state = o['id'], o.get('state')
            r = self.store.rentals.get(rid)
            try:
                if state == REQUESTED and r is None:
                    self._place(o, now)
                    report.placed.append(rid)
                elif state == ENDING:
                    if r is None or not r.box:
                        # never placed here (or withdrawn before placement): nothing to drain, say so at once
                        if r is None:
                            r = RentalRecord(id=rid, state=ENDED, gpu_type=str(o.get('gpu_type') or ''), created_at=now)
                            r.ended_at = now
                            self.store.put(r)
                        else:
                            order_end(self.store, rid, APP_ENDING_REASON)
                        report.ended.append(rid)
                    elif r.state in (STARTING_R, ACTIVE):
                        order_end(self.store, rid, APP_ENDING_REASON)
                        report.ended.append(rid)
                elif state == ACTIVE and r is not None and r.state in (STARTING_R, ACTIVE):
                    ends_at = o.get('ends_at')
                    if isinstance(ends_at, (int, float)) and float(ends_at) != r.ends_at:
                        r.ends_at = float(ends_at)
                        self.store.put(r)
                        report.extended.append(rid)
            except (RentalError, KeyError, TypeError, ValueError) as e:
                report.errors.append(f'{rid}: {type(e).__name__}: {e}'[:300])
        return report

    def _place(self, o: dict, now: float) -> RentalRecord:
        ends_at = float(o.get('ends_at') or 0.0)
        hours = max(0.0, ends_at - now) / 3600.0 if ends_at else 0.0
        if hours <= 0:
            raise RentalError('ends_at is not in the future')
        keys = [str(k) for k in (o.get('ssh_pubkeys') or [])]
        ports = [int(p) for p in (o.get('ports') or [22])]
        env = {str(k): str(v) for k, v in (o.get('env') or {}).items()}
        box_uid = o.get('box_uid')
        r = place_order(
            self.store,
            gpu_type=str(o['gpu_type']),
            gpu_count=int(o['gpu_count']),
            image=str(o['image']),
            ssh_pubkeys=keys,
            hours=hours,
            ports=ports,
            env=env,
            rental_id=o['id'],
            box_uid=int(box_uid) if isinstance(box_uid, int) else None,
            now=now,
        )
        r.ends_at = ends_at  # the app's exact number, not our rounding of it
        self.store.put(r)
        return r

    def send_reports(self) -> PollReport:
        """One PATCH per record the app has not heard the current state of, oldest first."""
        report = PollReport()
        due = sorted(
            (r for r in self.store.rentals.values() if r.state != REQUESTED and r.reported_state != r.state),
            key=lambda r: r.created_at,
        )
        for r in due:
            try:
                self.client.report(r.id, report_body(r))
            except SeamError as e:
                report.errors.append(f'{r.id}: {e}'[:300])
                break  # the app is unreachable: the rest wait for the next poll
            r.reported_state = r.state
            self.store.put(r)
            report.reported.append(r.id)
        return report

    def poll(self) -> dict:
        """Orders in, then reports out. Returns both as one status dict."""
        taken = self.take_orders()
        sent = self.send_reports()
        return {'orders': asdict(taken), 'reports': asdict(sent), 'ok': taken.ok and sent.ok}
