# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The reconciler: desired replicas vs running instances, Kubernetes-style, over SSH (vault ``26`` §3, ``23`` §4, §8).

**Desired** = every enabled deployment × its replicas, for entries whose signature verifies on this read (an entry
that does not verify is never run, and what runs of it is drained). **Running** = the instances recorded in
``instances.json`` **and** confirmed on their boxes by label: each pass lists our labelled containers on every box it
visits, so a restarted controller rebuilds its view from the boxes (``docker ps --filter label=io.gittensor.instance``).

One pass, one SSH visit per box:

* **Confirm.** A recorded instance whose container is gone or stopped is lost: its card goes to CHECKING. A running
  labelled container with no record is re-adopted when its card is LEASED to that instance; a container caught
  mid-start (card STARTING) or on a card we did not lease is undeployed.
* **Too many** (or a disabled / unverifiable entry, or a benched box): DRAINING, undeploy with the manifest's drain,
  CHECKING. The next proof round returns the card to IDLE after a pass.
* **Too few**: pick an IDLE card that satisfies ``placement`` (GPU type from the pinned card name, ``min_vram_gb``
  against our spec table, one card per instance), freshest last check first; STARTING, pre-stage, deploy, health within
  ``placement.max_load_s``, the entry canary, then LEASED at the first passing probe after it. A failed start undeploys
  and goes to CHECKING with no bench; ``FAILED_STARTS_BENCH_AFTER`` in a row on one box benches it.

Every step is saved before the next begins and every docker operation is idempotent, so a pass killed anywhere is
finished by the next one. Not here: the gateway (it reads ``instances.json``), the in-lease heartbeat and health watch
(WS-D), standing and rotation (WS-E), pay (WS-F).
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.state import (
    BENCHED,
    BUSY,
    CHECKING,
    DRAINING,
    IDLE,
    LEASED,
    STARTING,
    BoxState,
    StateStore,
    record_start,
    transition_card,
)
from gittensor.controller.manifest import Drain, Manifest, gpu_type_of
from gittensor.controller.registry import DeploymentStore, Registry, RegistryError, VerifiedEntry
from gittensor.controller.runspec import (
    BoxContainer,
    BoxHttp,
    HttpClient,
    PlacementError,
    PrestageReport,
    PullToken,
    build_run_spec,
    deploy,
    list_containers,
    new_instance_id,
    prestage,
    probe_health,
    run_entry_canary,
    undeploy,
    wait_healthy,
)
from gittensor.controller.ssh import SshTransportError
from gittensor.controller.ssh.certs import CertificateError

_TRANSPORT = (SshTransportError, CertificateError)
_SPEC_VRAM_GB = {'RTX5090': cfg.RTX_5090.vram_total_mib_min / 1024}  # our spec table, never the box's self-report


# ---------------------------------------------------------------- instances.json ------------------------------------


@dataclass
class InstanceRecord:
    """One placement instance, as the gateway will read it."""

    id: str
    entry: str
    box: str
    uuid: str
    container_id: str = ''
    host: str = ''
    port: int | None = None  # the port the outside reaches (the box's port map applied)
    healthy: bool = False
    draining: bool = False
    started_at: float | None = None  # docker run returned
    leased_at: float | None = None  # first passing health probe after the canary
    drain_type: str = 'kill'
    drain_max_s: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> InstanceRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def drain(self) -> Drain:
        return Drain(self.drain_type, self.drain_max_s)


class InstanceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.instances: dict[str, InstanceRecord] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text() or '{}')
            self.instances = {k: InstanceRecord.from_dict(v) for k, v in raw.items()}

    def put(self, record: InstanceRecord) -> None:
        self.instances[record.id] = record
        self.save()

    def remove(self, instance_id: str) -> None:
        if self.instances.pop(instance_id, None) is not None:
            self.save()

    def on_box(self, box_id: str) -> list[InstanceRecord]:
        return [r for r in self.instances.values() if r.box == box_id]

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        tmp.write_text(json.dumps({k: asdict(v) for k, v in sorted(self.instances.items())}, indent=1))
        tmp.replace(self.path)


# ---------------------------------------------------------------- placement -----------------------------------------


def card_fits(box: BoxState, manifest: Manifest) -> tuple[bool, str]:
    gpu_type = gpu_type_of(box.card_name)
    if not manifest.placement.gpu_types.admits(gpu_type):
        return False, f'gpu type {gpu_type or "?"} not admitted'
    vram_gb = _SPEC_VRAM_GB.get(gpu_type, 0.0)
    if vram_gb < manifest.placement.min_vram_gb:
        return False, f'{gpu_type} spec VRAM {vram_gb:.1f} GB < min_vram_gb {manifest.placement.min_vram_gb}'
    if manifest.placement.cards_per_instance != 1:
        return False, 'cards_per_instance > 1 is not placed yet'
    if manifest.front_door.type == 'batch':
        return False, 'batch front door is reserved, not placed'
    return True, ''


# ---------------------------------------------------------------- the pass ------------------------------------------


@dataclass
class Action:
    kind: str  # start | drain | lost | adopt | orphan
    box: str
    instance: str
    entry: str = ''
    uuid: str = ''
    ok: bool = True
    detail: str = ''
    states: list[str] = field(default_factory=list)  # the card's state sequence through this action
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class ReconcileReport:
    desired: dict[str, int] = field(default_factory=dict)
    running: dict[str, int] = field(default_factory=dict)  # after the pass
    actions: list[Action] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unreachable: dict[str, str] = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unreachable and all(a.ok for a in self.actions)


@dataclass
class Reconciler:
    boxes: StateStore
    instances: InstanceStore
    deployments: DeploymentStore
    registry: Registry
    make_runner: Callable[[BoxState], HostRunner]
    http_for: Callable[[HostRunner, BoxState], HttpClient] = lambda runner, box: BoxHttp(runner)
    pull_token: PullToken | None = None
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    visit_all: bool = True  # list containers on every admitted box (a restarted controller); later passes: only busy
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- state writes (single writer; every change saved before the next step) -----------------------------------

    def _box(self, box_id: str) -> BoxState:
        return self.boxes.boxes[box_id]

    def _put_box(self, box: BoxState) -> None:
        with self._lock:
            self.boxes.put(box)

    def _move(self, box_id: str, uuid: str, to: str, action: Action, instance_id: str | None = None) -> None:
        with self._lock:
            box = self._box(box_id)
            if box.status != IDLE or uuid not in box.cards:
                return  # benched meanwhile: the cards are gone with the pin
            if box.cards[uuid].state == to:
                return
            self.boxes.put(transition_card(box, uuid, to, self.wall(), instance_id))
            action.states.append(to)

    def _put_record(self, record: InstanceRecord) -> None:
        with self._lock:
            self.instances.put(record)

    def _drop_record(self, instance_id: str) -> None:
        with self._lock:
            self.instances.remove(instance_id)

    # -- the pass ---------------------------------------------------------------------------------------------------

    def run_pass(self) -> ReconcileReport:
        report = ReconcileReport()
        started = self.clock()
        entries, report.desired = self._desired(report)
        runners: dict[str, HostRunner] = {}
        try:
            seen = self._confirm(report, entries, runners)
            report.timings_ms['confirm'] = round((self.clock() - started) * 1000.0, 1)
            ops = self._plan(report, entries, seen)
            planned = self.clock()
            boxes = sorted(ops)
            if boxes:
                with ThreadPoolExecutor(max_workers=len(boxes)) as pool:
                    list(pool.map(lambda box_id: self._execute(box_id, ops[box_id], entries, runners, report), boxes))
            report.timings_ms['execute'] = round((self.clock() - planned) * 1000.0, 1)
        finally:
            for runner in runners.values():
                getattr(runner, 'close', lambda: None)()
        self.visit_all = False
        for record in self.instances.instances.values():
            if not record.draining:
                report.running[record.entry] = report.running.get(record.entry, 0) + 1
        report.timings_ms['total'] = round((self.clock() - started) * 1000.0, 1)
        return report

    def _desired(self, report: ReconcileReport) -> tuple[dict[str, VerifiedEntry], dict[str, int]]:
        entries: dict[str, VerifiedEntry] = {}
        desired: dict[str, int] = {}
        for entry_id, deployment in sorted(self.deployments.deployments.items()):
            desired[entry_id] = 0
            if deployment.desired <= 0:
                continue
            try:
                verified = self.registry.read(entry_id)  # re-verified on every read
            except RegistryError as e:
                report.errors.append(f'{entry_id}: not run: {e}')
                continue
            entries[entry_id] = verified
            desired[entry_id] = deployment.desired
        return entries, desired

    def _runner(self, box: BoxState, runners: dict[str, HostRunner]) -> HostRunner:
        with self._lock:
            if box.box_id not in runners:
                runners[box.box_id] = self.make_runner(box)
            return runners[box.box_id]

    def _confirm(
        self, report: ReconcileReport, entries: dict[str, VerifiedEntry], runners: dict[str, HostRunner]
    ) -> dict[str, list[BoxContainer]]:
        """List our containers on every box that has (or may have) any, then settle records against them."""
        visit = [
            box
            for box in self.boxes.boxes.values()
            if box.host
            and (
                self.instances.on_box(box.box_id)
                or any(c.state in BUSY for c in box.cards.values())
                or (self.visit_all and box.status == IDLE)
            )
        ]
        seen: dict[str, list[BoxContainer]] = {}

        def list_box(box: BoxState) -> None:
            try:
                containers = list_containers(self._runner(box, runners))
            except (*_TRANSPORT, PlacementError) as e:
                with self._lock:
                    report.unreachable[box.box_id] = f'{type(e).__name__}: {e}'[:300]
                return
            with self._lock:
                seen[box.box_id] = containers

        if visit:
            with ThreadPoolExecutor(max_workers=len(visit)) as pool:
                list(pool.map(list_box, visit))

        for box_id, containers in sorted(seen.items()):
            box = self._box(box_id)
            by_instance = {c.instance_id: c for c in containers}
            for record in self.instances.on_box(box_id):
                container = by_instance.get(record.id)
                if container is not None and container.running and container.container_id == record.container_id:
                    continue
                if record.container_id == '' and box.card(record.uuid).state == STARTING and container is None:
                    continue  # recorded before docker run; the start is resolved below as a mid-start leftover
                action = Action('lost', box_id, record.id, record.entry, record.uuid, False)
                action.states.append(box.card(record.uuid).state)
                action.detail = (
                    f'container {container.state} ({container.container_id[:12]})' if container else 'container gone'
                )
                self._drop_record(record.id)
                self._to_checking(box_id, record.uuid, action)
                report.actions.append(action)
            recorded = {r.id for r in self.instances.on_box(box_id)}
            for container in containers:
                if container.instance_id in recorded:
                    continue
                card = self._box(box_id).card(container.uuid)
                wanted = container.entry_id in entries and self._box(box_id).status == IDLE
                if container.running and wanted and card.state == LEASED and card.instance_id == container.instance_id:
                    record = self._record_for(self._box(box_id), container, entries[container.entry_id].manifest)
                    record.healthy = True
                    self._put_record(record)
                    report.actions.append(
                        Action('adopt', box_id, container.instance_id, container.entry_id, container.uuid, True,
                               f'running {container.container_id[:12]}, card LEASED', [LEASED])
                    )  # fmt: skip
                    continue
                # A leftover: a start we crashed in, a stopped container, or one on a card we did not lease to it.
                record = self._record_for(self._box(box_id), container, None)
                record.draining = True
                self._put_record(record)
                report.actions.append(
                    Action('orphan', box_id, container.instance_id, container.entry_id, container.uuid, True,
                           f'{container.state} container, card {card.state}: undeploy')
                )  # fmt: skip
            # Busy cards with nothing behind them (the instance vanished before any record or container existed).
            live = {r.uuid for r in self.instances.on_box(box_id)}
            for uuid, card in sorted(self._box(box_id).cards.items()):
                if card.state in BUSY and uuid not in live:
                    action = Action('lost', box_id, card.instance_id, '', uuid, False, 'no instance behind a busy card')
                    action.states.append(card.state)
                    self._to_checking(box_id, uuid, action)
                    report.actions.append(action)
        return seen

    def _record_for(self, box: BoxState, container: BoxContainer, manifest: Manifest | None) -> InstanceRecord:
        drain = manifest.drain if manifest else Drain('requests', 30)
        return InstanceRecord(
            id=container.instance_id,
            entry=container.entry_id,
            box=box.box_id,
            uuid=container.uuid,
            container_id=container.container_id,
            host=box.host,
            port=box.public_port(container.port) if container.port else None,
            drain_type=drain.type,
            drain_max_s=drain.max_s,
        )

    def _to_checking(self, box_id: str, uuid: str, action: Action) -> None:
        box = self._box(box_id)
        if box.status != IDLE or uuid not in box.cards:
            return
        state = box.cards[uuid].state
        if state == LEASED:
            self._move(box_id, uuid, DRAINING, action)
        if self._box(box_id).card(uuid).state in (STARTING, DRAINING):
            self._move(box_id, uuid, CHECKING, action)

    def _plan(
        self, report: ReconcileReport, entries: dict[str, VerifiedEntry], seen: dict[str, list[BoxContainer]]
    ) -> dict[str, list[tuple[str, object]]]:
        ops: dict[str, list[tuple[str, object]]] = {}

        def add(box_id: str, op: str, arg: object) -> None:
            ops.setdefault(box_id, []).append((op, arg))

        # Drains: leftovers, benched boxes, disabled or unverifiable entries, then any excess over the replica count.
        by_entry: dict[str, list[InstanceRecord]] = {}
        for record in sorted(self.instances.instances.values(), key=lambda r: r.id):
            box = self.boxes.boxes.get(record.box)
            if record.box in report.unreachable:
                continue  # never judged on a failed visit; next pass
            if record.draining or box is None or box.status == BENCHED or report.desired.get(record.entry, 0) <= 0:
                add(record.box, 'drain', record)
                continue
            by_entry.setdefault(record.entry, []).append(record)
        for entry_id, records in sorted(by_entry.items()):
            excess = len(records) - report.desired.get(entry_id, 0)
            if excess > 0:
                victims = sorted(records, key=lambda r: (r.healthy, -(r.started_at or 0.0)))[
                    :excess
                ]  # unhealthy, newest
                for record in victims:
                    add(record.box, 'drain', record)

        # Starts: IDLE cards on reachable IDLE boxes, freshest last check first, one instance per card.
        used = {(r.box, r.uuid) for r in self.instances.instances.values()}
        candidates = [
            (box, uuid)
            for box in sorted(self.boxes.boxes.values(), key=lambda b: (-(b.last_check_at or 0.0), b.box_id))
            if box.status == IDLE and box.host and box.box_id not in report.unreachable
            for uuid in box.pinned_uuids
            if box.card(uuid).state == IDLE and (box.box_id, uuid) not in used
        ]
        for entry_id, verified in sorted(entries.items()):
            running = len(by_entry.get(entry_id, []))
            need = report.desired[entry_id] - running
            for box, uuid in list(candidates):
                if need <= 0:
                    break
                fits, _ = card_fits(box, verified.manifest)
                if not fits:
                    continue
                candidates.remove((box, uuid))
                add(box.box_id, 'start', (entry_id, uuid))
                need -= 1
            if need > 0:
                report.errors.append(f'{entry_id}: {need} replica(s) short: no IDLE card fits its placement')
        return ops

    def _execute(
        self,
        box_id: str,
        box_ops: list[tuple[str, object]],
        entries: dict[str, VerifiedEntry],
        runners: dict[str, HostRunner],
        report: ReconcileReport,
    ) -> None:
        """One box's work, in one visit: drains first (they free cards), then starts."""
        box = self._box(box_id)
        runner = self._runner(box, runners)
        for op, arg in sorted(box_ops, key=lambda o: o[0] != 'drain'):
            if op == 'drain':
                action = self._drain(box_id, runner, arg)
            else:
                entry_id, uuid = arg
                action = self._start(box_id, runner, entries[entry_id], uuid)
            with self._lock:
                report.actions.append(action)
            if action.detail.startswith('transport:'):
                with self._lock:
                    report.unreachable[box_id] = action.detail
                return

    def _drain(self, box_id: str, runner: HostRunner, record: InstanceRecord) -> Action:
        action = Action('drain', box_id, record.id, record.entry, record.uuid)
        box = self._box(box_id)
        action.states.append(box.card(record.uuid).state if box.status == IDLE else box.status)
        marks = [('begin', self.clock())]
        record.draining, record.healthy = True, False
        self._put_record(record)  # the gateway stops routing before anything is stopped
        if box.status == IDLE and box.card(record.uuid).state in (STARTING, LEASED):
            self._move(box_id, record.uuid, DRAINING, action)
        marks.append(('mark_draining', self.clock()))
        try:
            result = undeploy(runner, record.id, record.drain, self.clock)
        except _TRANSPORT as e:
            action.ok, action.detail = False, f'transport: {type(e).__name__}: {e}'[:300]
            return action
        except PlacementError as e:
            action.ok, action.detail = False, str(e)[:300]
            return action
        marks.append(('undeploy', self.clock()))
        self._drop_record(record.id)
        if self._box(box_id).card(record.uuid).state == DRAINING:
            self._move(box_id, record.uuid, CHECKING, action)
        action.ok = result.in_time
        action.detail = (f'drained in {result.elapsed_s:.1f} s' if result.found else 'no container left') + (
            '' if result.in_time else f' — past drain.max_s {record.drain_max_s} s: failed drain'
        )
        action.timings_ms = _durations(marks)
        return action

    def _start(self, box_id: str, runner: HostRunner, verified: VerifiedEntry, uuid: str) -> Action:
        manifest = verified.manifest
        instance_id = new_instance_id()
        action = Action('start', box_id, instance_id, verified.entry_id, uuid, False, states=[IDLE])
        box = self._box(box_id)
        marks = [('begin', self.clock())]
        self._move(box_id, uuid, STARTING, action, instance_id)
        record = InstanceRecord(
            id=instance_id,
            entry=verified.entry_id,
            box=box_id,
            uuid=uuid,
            host=box.host,
            port=box.public_port(manifest.front_door.port) if manifest.front_door.port else None,
            drain_type=manifest.drain.type,
            drain_max_s=manifest.drain.max_s,
        )
        self._put_record(record)
        client = self.http_for(runner, box)
        staged = PrestageReport()
        try:
            spec = build_run_spec(verified.entry_id, manifest, uuid, instance_id)
            prestage(runner, spec, manifest, self.pull_token, self.clock, report=staged)
            marks.append(('prestage', self.clock()))
            record.container_id = deploy(runner, spec)
            record.started_at = self.wall()
            self._put_record(record)
            marks.append(('docker_run', self.clock()))
            health = wait_healthy(
                client, manifest, runner, record.container_id, manifest.placement.max_load_s, self.clock, self.sleep
            )
            marks.append(('load', self.clock()))
            if not health.ok:
                raise PlacementError(f'health: {health.detail}')
            canary = run_entry_canary(client, manifest, self.rng)
            marks.append(('canary', self.clock()))
            if not canary.ok:
                raise PlacementError(f'entry canary: {canary.detail}')
            first = probe_health(client, manifest, runner, record.container_id)
            marks.append(('first_probe', self.clock()))
            if not first.ok:
                raise PlacementError(f'first probe after the canary: {first.detail}')
        except _TRANSPORT as e:
            # Lost the box mid-start: nothing can be undeployed now. The card stays STARTING and the next pass that
            # reaches the box finds the leftover and undeploys it.
            action.detail = f'transport: {type(e).__name__}: {e}'[:300]
            action.timings_ms = _durations(marks)
            return action
        except PlacementError as e:
            action.detail = f'failed start: {e}'[:700]
            self._fail_start(box_id, runner, record, action, manifest)
            action.timings_ms = {
                **_durations(marks + [('undeploy', self.clock())]),
                **{f'prestage.{k}': v for k, v in staged.timings_ms.items()},  # a failed start still measured these
            }
            return action
        record.healthy, record.leased_at = True, self.wall()
        self._put_record(record)
        self._move(box_id, uuid, LEASED, action)
        self._put_box(record_start(self._box(box_id), True, self.wall()))
        action.ok = True
        action.detail = (
            f'{manifest.image.split("@")[0]} on {uuid[:12]}…, container {record.container_id[:12]}, '
            f'{record.host}:{record.port}; image {"pulled" if staged.pulled else "pre-staged"}'
            + (f'; {len(staged.artifacts)} artifact(s) verified' if staged.artifacts else '')
            + (f'; canary {canary.detail}' if manifest.entry_canary else '')
        )
        action.timings_ms = {**_durations(marks), **{f'prestage.{k}': v for k, v in staged.timings_ms.items()}}
        return action

    def _fail_start(
        self, box_id: str, runner: HostRunner, record: InstanceRecord, action: Action, manifest: Manifest
    ) -> None:
        """Undeploy, CHECKING, count it (no bench unless it is the third in a row on this box)."""
        try:
            undeploy(runner, record.id, Drain('kill'), self.clock)
        except (*_TRANSPORT, PlacementError) as e:
            action.detail += f'; undeploy failed ({type(e).__name__}), next pass retries'
            record.draining = True
            self._put_record(record)
            return
        self._drop_record(record.id)
        self._move(box_id, record.uuid, CHECKING, action)
        after = record_start(self._box(box_id), False, self.wall())
        if after.status == BENCHED:
            action.detail += f'; {cfg.FAILED_STARTS_BENCH_AFTER} failed starts in a row: box BENCHED'
            action.states.append(BENCHED)
        self._put_box(after)


def _durations(marks: list[tuple[str, float]]) -> dict[str, float]:
    return {name: round((b - a) * 1000.0, 1) for (_, a), (name, b) in zip(marks, marks[1:])}
