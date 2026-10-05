# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The reconciler: desired replicas vs running instances, Kubernetes-style, over SSH (vault ``26`` §3, ``23`` §4, §8).

**Desired** = every enabled deployment × its replicas, for entries whose signature verifies on this read (an entry
that does not verify is never run, and what runs of it is drained). **Running** = the instances recorded in
``instances.json`` **and** confirmed on their boxes by label: each pass lists our labelled containers on every box it
visits, so a restarted controller rebuilds its view from the boxes (``docker ps --filter label=io.gittensor.instance``).

One pass, one SSH visit per box:

* **Confirm.** A recorded instance whose container is gone or stopped is lost: its card goes to CHECKING. Under a
  LEASED card that is a heartbeat failure (bench, pay withheld), unless the box missed a heartbeat since its last good
  one: then it is ``instance_stopped`` (Kimbo 9/16: a clean leave or a reboot, not a cheat; the lease ends at the
  last good heartbeat, no bench). A running labelled container with no record is re-adopted when its card is LEASED
  to that instance; a container caught mid-start (card STARTING) or on a card we did not lease is undeployed.
* **Too many** (or a disabled / unverifiable entry, or a benched box): DRAINING, undeploy with the manifest's drain,
  CHECKING. The next proof round returns the card to IDLE after a pass. A planned drain of a LEASED card (not a benched
  box's) first waits for the gateway: the record is marked draining, and the container is stopped only once the
  gateway has re-read the table and counts no request in flight on it (``_await_quiet``, bounded).
* **Too many** releases the lowest standing first, then the oldest last full check.
* **Too few**: pick an IDLE card that satisfies ``placement`` (GPU type from the pinned card name, ``min_vram_gb``
  against our spec table, one card per instance), best standing first, then freshest last check; STARTING, pre-stage,
  deploy, health within ``placement.max_load_s``, the entry canary, then LEASED at the first passing probe after it,
  with its lease cap drawn from its box's standing. A failed start undeploys and goes to CHECKING with no bench (a
  ``start_failed`` standing event); ``FAILED_STARTS_BENCH_AFTER`` in a row on one box benches it.
* **Rotation** (``23`` §8, WS-E): a lease past its cap gets a replacement started on the best free card, and is drained
  on a later pass once that replacement is LEASED and healthy: replacement first, never below the replica count, at
  most ``ROTATION_MAX_FRACTION`` of leased cards cycling at once, oldest check first. With no free card that fits (a
  fleet leased to capacity, Kimbo 9/18) the lease is cycled **in place**: drained, its card re-proved from CHECKING,
  and the replica started again by **Too few**; the same budget counts every card already on its way round (STARTING,
  DRAINING, CHECKING), so capacity dips by that fraction at most. Without it a full fleet is never drained: no proof,
  no ``clean_lease``, probation for ever. Cycle time is unpaid by construction: neither card is LEASED-and-paid while
  it moves. A normal drain of a LEASED card records a ``clean_lease`` standing event with its leased seconds, a late
  one ``drain_failed``.
* **Cooldown**: a box whose last ``external_use`` event (the lease accounting check, ``usage_check.py``) is under
  ``EXTERNAL_USE_COOLDOWN_S`` old gets no new lease: its cards stay IDLE, proved and idle-paid, and are not placement
  candidates until then.

A start publishes the workload's port on the box's docker bridge gateway address (``workload_bind`` private, the
default; looked up once per box visit) and records ``bind`` on the instance and on the container's label, so a
re-adopted instance keeps it. The setting applies to new starts only: running instances keep theirs, and a fleet
changes over one card at a time as leases cycle.

Every step is saved before the next begins and every docker operation is idempotent, so a pass killed anywhere is
finished by the next one. Not here: the gateway (it reads ``instances.json``), the in-lease heartbeat and health watch
(WS-D, which keeps each instance's pay span), pay (WS-F, ``pay/ledger.py``).
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.state import (
    BENCHED,
    BUSY,
    CHECKING,
    CLEAN_LEASE,
    DRAIN_FAILED,
    DRAINING,
    IDLE,
    LEASED,
    OUR_CONTAINER,
    STARTING,
    BoxState,
    CardTransitionError,
    StateStore,
    add_event,
    apply_heartbeat_failure,
    apply_instance_stopped,
    lease_cooldown_until,
    record_start,
    transition_card,
)
from gittensor.controller.locks import BoxLocks
from gittensor.controller.manifest import Drain, Manifest, gpu_type_of
from gittensor.controller.registry import DeploymentStore, Registry, RegistryError, VerifiedEntry
from gittensor.controller.runspec import (
    BIND_PRIVATE,
    BIND_PUBLIC,
    WORKLOAD_BINDS,
    BoxContainer,
    BoxHttp,
    HttpClient,
    PlacementError,
    PrestageReport,
    PullToken,
    bridge_gateway,
    build_run_spec,
    deploy,
    host_port_client,
    inspect_container,
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
from gittensor.controller.standing import lease_cap_s, rank, standing

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
    # host_port with the box's port map applied. Informational for a private instance (the gateway reaches it through
    # its tunnel); only a public-bind instance under the gateway's --allow-direct is addressed at host:port.
    port: int | None = None
    host_port: int | None = None  # the box's port the instance is published on, from its workload range
    bind: str = BIND_PUBLIC  # private: published on the box's docker bridge address; public: the previous form
    healthy: bool = False
    draining: bool = False
    started_at: float | None = None  # docker run returned
    leased_at: float | None = None  # first passing health probe after the canary
    drain_type: str = 'kill'
    drain_max_s: int = 0
    # What `docker inspect` said right after our `docker run`: the heartbeat holds the container to both (a restart
    # changes StartedAt, a recreate changes the ID, a swapped image changes the image ID).
    docker_started_at: str = ''
    image_id: str = ''
    # The in-lease watch (WS-D). heartbeat_ok None: no conclusive heartbeat yet (WS-F pays nothing without one).
    last_heartbeat_at: float | None = None
    heartbeat_ok: bool | None = None
    heartbeat: dict = field(default_factory=dict)  # the three answers and the four pay conditions, last visit
    heartbeat_misses: int = 0  # consecutive visits with no answer (SSH or docker failed); no bench, no pay
    last_health_at: float | None = None
    health_ok: bool | None = None
    health_failures: int = 0  # consecutive; manifest.health.failure_threshold replaces the replica
    health_detail: str = ''
    # Rotation (WS-E). The cap is drawn when the lease starts (standing x jitter). A lease past it gets a replacement
    # started first (`replaces` on the new record, `rotating` = the replacement's box on the old one) and is drained
    # once that replacement is LEASED.
    lease_cap_s: float | None = None
    replaces: str = ''
    rotating: str = ''
    stopped_at: float | None = None  # when the controller marked it draining: pay never runs past it
    ended_by: str = ''  # the check that ended the lease (external_use): its drain writes no clean_lease
    # Pay (WS-F): the current span in which the four pay conditions held at every check, [pay_from, pay_through].
    # The watch extends pay_through as checks pass, closes the span (pay_open False) on any failure or miss, and opens
    # a new one at the next fully passing check. The ledger pays each span once.
    pay_from: float | None = None
    pay_through: float | None = None
    pay_open: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> InstanceRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def drain(self) -> Drain:
        return Drain(self.drain_type, self.drain_max_s)


# One box's planned work: a drain of a record, or a start as (entry id, gpu uuid, replaced instance id, host port).
StartArgs = tuple[str, str, str, int | None]
BoxOp = tuple[Literal['drain'], InstanceRecord] | tuple[Literal['start'], StartArgs]


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

    def containers_on(self, box_id: str) -> set[str]:
        """One box's instances' container IDs: what ``checks.check_card_free`` judges its open NVIDIA device handles
        against. Per box and read afresh at every use, never snapshotted for a whole pass — a rotation mints a new
        container ID, and a set taken before the box's lock can miss the container a start wrote while the caller
        waited for it (the heartbeat rebuilds it per visit for the same reason)."""
        return {r.container_id for r in self.instances.values() if r.box == box_id and r.container_id}

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
    kind: str  # start | drain | lost | stopped | adopt | orphan
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
    launched: list[str] = field(default_factory=list)  # background mode: boxes whose starts / drains went to a thread
    in_flight: list[str] = field(default_factory=list)  # background mode: boxes still busy from an earlier pass
    rotations: list[str] = field(default_factory=list)  # leases past their cap that got a replacement this pass

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unreachable and all(a.ok for a in self.actions)


def gateway_healthz(url: str, timeout: float = 3.0) -> Callable[[], dict | None]:
    """A reader of the gateway's ``/healthz`` (the one route without the key) for ``Reconciler.gateway_state``: the
    JSON as a dict, None when it cannot be read."""
    import urllib.request

    target = url.rstrip('/') + '/healthz'

    def read() -> dict | None:
        try:
            with urllib.request.urlopen(target, timeout=timeout) as response:  # noqa: S310 (operator-given URL)
                data = json.loads(response.read().decode())
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    return read


@dataclass
class Reconciler:
    boxes: StateStore
    instances: InstanceStore
    deployments: DeploymentStore
    registry: Registry
    make_runner: Callable[[BoxState], HostRunner]
    http_for: Callable[[HostRunner, BoxState], HttpClient] = lambda runner, box: BoxHttp(runner)
    pull_token: PullToken | None = None
    workload_bind: str = BIND_PRIVATE  # where new starts publish their port (WORKLOAD_BINDS); running ones keep theirs
    # The gateway's /healthz as a dict ({'refreshed_at': …, 'in_flight': {instance: n}}), or None when it cannot be
    # read. None here: no gateway to ask, a drain takes the fixed grace only.
    gateway_state: Callable[[], dict | None] | None = None
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    visit_all: bool = True  # list containers on every admitted box (a restarted controller); later passes: only busy
    # `gitt controller run`: a box's starts and drains hold its lock (the proof round skips a box mid-start), run on
    # their own thread (`background`), and the pass returns without waiting for a model load; boxes still busy are
    # left alone by later passes until their thread is done.
    box_locks: BoxLocks | None = None
    background: bool = False
    on_background: Callable[[ReconcileReport], None] | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _in_flight: dict[str, threading.Thread] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.workload_bind not in WORKLOAD_BINDS:
            raise ValueError(f'workload_bind {self.workload_bind!r}: one of {", ".join(WORKLOAD_BINDS)}')

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
            try:
                self.boxes.put(transition_card(box, uuid, to, self.wall(), instance_id))
            except CardTransitionError:
                return  # another loop moved the card first (the watch replaced the replica)
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
        with self._lock:
            busy = {box_id for box_id, thread in self._in_flight.items() if thread.is_alive()}
        report.in_flight = sorted(busy)
        entries, report.desired = self._desired(report)
        runners: dict[str, HostRunner] = {}
        try:
            seen = self._confirm(report, entries, runners, busy)
            report.timings_ms['confirm'] = round((self.clock() - started) * 1000.0, 1)
            ops = self._plan(report, entries, seen, busy)
            planned = self.clock()
            boxes = sorted(ops)
            if self.background:
                for box_id in boxes:
                    self._launch(box_id, ops[box_id], entries)
                report.launched = boxes
            elif boxes:
                with ThreadPoolExecutor(max_workers=len(boxes)) as pool:
                    list(pool.map(lambda box_id: self._execute(box_id, ops[box_id], entries, runners, report), boxes))
            report.timings_ms['execute'] = round((self.clock() - planned) * 1000.0, 1)
        finally:
            for runner in runners.values():
                getattr(runner, 'close', lambda: None)()
        self.visit_all = False
        for record in list(self.instances.instances.values()):
            if not record.draining:
                report.running[record.entry] = report.running.get(record.entry, 0) + 1
        report.timings_ms['total'] = round((self.clock() - started) * 1000.0, 1)
        return report

    def _launch(self, box_id: str, box_ops: list[BoxOp], entries: dict[str, VerifiedEntry]) -> None:
        """One box's starts and drains on their own thread, with their own SSH visit. Its actions reach
        ``on_background`` as a report when it is done."""

        def work() -> None:
            sub = ReconcileReport()
            runners: dict[str, HostRunner] = {}
            try:
                self._execute(box_id, box_ops, entries, runners, sub)
            except Exception as e:  # never kill the loop: the state is saved step by step and the next pass retries
                sub.errors.append(f'{box_id}: {type(e).__name__}: {e}'[:300])
            finally:
                for runner in runners.values():
                    getattr(runner, 'close', lambda: None)()
                if self.on_background is not None:
                    self.on_background(sub)

        thread = threading.Thread(target=work, name=f'reconcile-{box_id[:16]}', daemon=True)
        with self._lock:
            self._in_flight[box_id] = thread
        thread.start()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for background starts and drains; True when none is left running."""
        with self._lock:
            threads = list(self._in_flight.values())
        for thread in threads:
            thread.join(timeout)
        return not any(thread.is_alive() for thread in threads)

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
        self,
        report: ReconcileReport,
        entries: dict[str, VerifiedEntry],
        runners: dict[str, HostRunner],
        busy: AbstractSet[str] = frozenset(),
    ) -> dict[str, list[BoxContainer]]:
        """List our containers on every box that has (or may have) any, then settle records against them. A box still
        busy with an earlier pass's starts is not visited: its records are mid-flight, not lost."""
        visit = [
            box
            for box in list(self.boxes.boxes.values())
            if box.host
            and box.box_id not in busy
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
            by_instance = {c.instance_id: c for c in containers}
            for record in self.instances.on_box(box_id):
                box = self._box(box_id)
                container = by_instance.get(record.id)
                if container is not None and container.running and container.container_id == record.container_id:
                    continue
                # A record with no container id on a STARTING card is a start that lost the box before `docker run`
                # answered. The box is not busy, so no thread is still on it: it is lost like any other, or the card
                # stays STARTING, unpaid and never re-proved, until someone edits the state by hand (UID 86, 9/22).
                state = box.card(record.uuid).state if box.status == IDLE else box.status
                action = Action('lost', box_id, record.id, record.entry, record.uuid, False, states=[state])
                if container is not None:
                    action.detail = f'container {container.state} ({container.container_id[:12]})'
                elif record.container_id == '':
                    action.detail = 'start never reached docker run'
                else:
                    action.detail = 'container gone'
                if state == LEASED and not record.draining:
                    if record.heartbeat_misses > 0:
                        action.kind = 'stopped'
                        self._stop_vanished(box_id, record, action)
                    else:
                        self._bench_vanished(box_id, record, action)
                    report.actions.append(action)
                    continue
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
            host_port=container.port,  # the port label carries the host port
            bind=container.bind,
            drain_type=drain.type,
            drain_max_s=drain.max_s,
        )

    def _bench_vanished(self, box_id: str, record: InstanceRecord, action: Action) -> None:
        """A container gone or stopped under a LEASED card, without our stop, is a heartbeat failure, not a restart
        (Kimbo 9/15): BENCHED on the ladder, pay withheld from now, and every instance on the box is marked for a kill
        drain, which this pass's plan carries out."""
        with self._lock:
            self.boxes.put(
                apply_heartbeat_failure(
                    self._box(box_id), [OUR_CONTAINER], self.wall(), instance=record.id, reason=action.detail
                )
            )
            for other in self.instances.on_box(box_id):
                other.draining, other.healthy, other.heartbeat_ok, other.pay_open = True, False, False, False
                other.stopped_at = other.stopped_at or self.wall()
                other.drain_type, other.drain_max_s = 'kill', 0
                self.instances.put(other)
        action.states.append(BENCHED)
        action.detail += ': not stopped by us, a heartbeat failure; box BENCHED, pay withheld'

    def _stop_vanished(self, box_id: str, record: InstanceRecord, action: Action) -> None:
        """A container gone or stopped under a LEASED card after the watch missed a heartbeat on the box (the agent was
        unreachable: a clean leave, a reboot) is a stop, not a cheat (Kimbo 9/16): the lease ends at the last good
        heartbeat, nothing is withheld, ``instance_stopped`` on the box, the card to CHECKING; the record drains with a
        kill this pass, which removes whatever the container left behind."""
        now = self.wall()
        last_good = record.last_heartbeat_at or record.leased_at or now
        with self._lock:
            record.draining, record.healthy, record.heartbeat_ok, record.pay_open = True, False, False, False
            record.stopped_at = min(record.stopped_at, last_good) if record.stopped_at is not None else last_good
            record.drain_type, record.drain_max_s = 'kill', 0
            self.instances.put(record)
            self.boxes.put(
                apply_instance_stopped(
                    self._box(box_id), record.uuid, now, instance=record.id,
                    missed_heartbeats=record.heartbeat_misses, lease_ended_at=last_good, via='reconcile',
                )
            )  # fmt: skip
        action.states.append(CHECKING)
        action.detail += (
            f': gone after {record.heartbeat_misses} missed heartbeat(s): instance stopped, not a cheat; lease ended '
            'at the last good heartbeat, nothing withheld; card CHECKING for the re-prove'
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
        self,
        report: ReconcileReport,
        entries: dict[str, VerifiedEntry],
        seen: dict[str, list[BoxContainer]],
        busy: AbstractSet[str] = frozenset(),
    ) -> dict[str, list[BoxOp]]:
        ops: dict[str, list[BoxOp]] = {}

        def add(box_id: str, op: BoxOp) -> None:
            ops.setdefault(box_id, []).append(op)

        now = self.wall()
        boxes = list(self.boxes.boxes.values())
        levels = {b.box_id: standing(b.standing_events, now) for b in boxes}

        def release_order(record: InstanceRecord) -> tuple:
            """Who goes first when leases are released: lowest standing, then the oldest last full check."""
            box = self.boxes.boxes.get(record.box)
            return (rank(levels.get(record.box, '')), (box.last_check_at if box else None) or 0.0, record.id)

        # A rotation whose replacement is gone (its start failed) is called off; the lease is rotated again later.
        records = sorted(list(self.instances.instances.values()), key=lambda r: r.id)
        replacements = {r.replaces: r for r in records if r.replaces}
        for record in records:
            if record.rotating and record.id not in replacements and record.rotating not in busy:
                record.rotating = ''
                self._put_record(record)

        # Drains: leftovers, benched boxes, disabled or unverifiable entries, then any excess over the replica count.
        # A busy box's starts count as running but nothing on it is touched until its thread is done. A lease being
        # rotated does not count: its replacement does.
        by_entry: dict[str, list[InstanceRecord]] = {}
        rotating: list[InstanceRecord] = []
        for record in records:
            box = self.boxes.boxes.get(record.box)
            if record.box in report.unreachable:
                continue  # never judged on a failed visit; next pass
            if record.box in busy:
                if not record.draining and not record.rotating:
                    by_entry.setdefault(record.entry, []).append(record)
                continue
            if record.draining or box is None or box.status == BENCHED or report.desired.get(record.entry, 0) <= 0:
                add(record.box, ('drain', record))
                continue
            if record.rotating:
                rotating.append(record)
                continue
            by_entry.setdefault(record.entry, []).append(record)
        for entry_id, entry_records in sorted(by_entry.items()):
            excess = len(entry_records) - report.desired.get(entry_id, 0)
            if excess > 0:
                movable = [r for r in entry_records if r.box not in busy]
                for record in sorted(movable, key=lambda r: (r.healthy, *release_order(r)))[:excess]:
                    add(record.box, ('drain', record))
        # A rotated lease is drained only once its replacement is LEASED and healthy: never below the replica count.
        for record in rotating:
            new = replacements.get(record.id)
            if new is not None and self._leased(new):
                add(record.box, ('drain', record))

        # Starts: IDLE cards on reachable IDLE boxes, best standing first, then freshest last check, one instance per
        # card, each with the first host port of its box's workload range that no record or container there holds.
        # A draining instance still holds its port until it is removed: a freed port is given out on a later pass.
        used = {(r.box, r.uuid) for r in records}
        ports_held: dict[str, set[int]] = {}
        for r in records:
            if r.host_port is not None:
                ports_held.setdefault(r.box, set()).add(r.host_port)
        for box_id, containers in seen.items():
            ports_held.setdefault(box_id, set()).update(c.port for c in containers if c.port is not None)
        candidates = [
            (box, uuid)
            for box in sorted(boxes, key=lambda b: (-rank(levels[b.box_id]), -(b.last_check_at or 0.0), b.box_id))
            if box.status == IDLE
            and box.host
            and not box.endpoint_changed
            and box.box_id not in report.unreachable
            and box.box_id not in busy
            and (lease_cooldown_until(box) or 0.0) <= now  # no new lease right after an external_use event
            for uuid in box.pinned_uuids
            if box.card(uuid).state == IDLE and (box.box_id, uuid) not in used
        ]

        port_skips: dict[str, str] = {}

        def take(verified: VerifiedEntry) -> tuple[BoxState, str, int | None] | None:
            """The best free card that fits, with a host port from its box's workload range; a box with no port left
            is skipped and named in ``port_skips``. A deployment pinned to a box (``Deployment.box``, canary runs on
            our own cards) takes cards from that box only."""
            pin = self.deployments.get(verified.entry_id).box
            for box, uuid in candidates:
                if pin and box.box_id != pin:
                    continue
                if not card_fits(box, verified.manifest)[0]:
                    continue
                host_port = None
                if verified.manifest.front_door.port is not None:
                    held = ports_held.setdefault(box.box_id, set())
                    host_port = next((p for p in box.workload_port_range() if p not in held), None)
                    if host_port is None:
                        span = box.workload_port_range()
                        port_skips[box.box_id] = f'workload ports {span[0]}-{span[-1]} all in use'
                        continue
                    held.add(host_port)
                candidates.remove((box, uuid))
                return box, uuid, host_port
            return None

        fresh_starts = 0
        for entry_id, verified in sorted(entries.items()):
            need = report.desired[entry_id] - len(by_entry.get(entry_id, []))
            while need > 0 and (pick := take(verified)) is not None:
                add(pick[0].box_id, ('start', (entry_id, pick[1], '', pick[2])))
                fresh_starts += 1
                need -= 1
            if need > 0:
                why = ''.join(f'; {box_id}: {reason}' for box_id, reason in sorted(port_skips.items()))
                pin = self.deployments.get(entry_id).box
                where = f' on pinned box {pin}' if pin else ''
                report.errors.append(
                    f'{entry_id}: {need} replica(s) short: no IDLE card{where} fits its placement{why}'
                )

        # Rotation (23 §8): a lease past its cap gets a replacement started on the best free card; it is drained on a
        # later pass once the replacement is LEASED. Oldest full check first, at most ROTATION_MAX_FRACTION of leased
        # cards at once (never fewer than one). No free card: the lease is cycled in place (drained now; its card is
        # re-proved from CHECKING and Too few starts the replica again), within the same budget less every card already
        # on its way round, so a fleet leased to capacity is still proved and still earns its clean lease-hours.
        leased = [r for entry_records in by_entry.values() for r in entry_records if self._leased(r)]
        for record in leased:
            if record.lease_cap_s is None:  # adopted, or started by a controller from before rotation
                record.lease_cap_s = lease_cap_s(levels.get(record.box, ''), self.rng)
                self._put_record(record)
        budget = max(1, int(cfg.ROTATION_MAX_FRACTION * (len(leased) + len(rotating)))) - len(rotating)
        expired = [r for r in leased if r.leased_at is not None and now - r.leased_at >= (r.lease_cap_s or 0.0)]
        cycling = fresh_starts + sum(
            1
            for box in boxes
            if box.status == IDLE
            for uuid in box.pinned_uuids
            if box.card(uuid).state in (STARTING, DRAINING, CHECKING)
        )
        for record in sorted(expired, key=release_order):
            if budget <= 0:
                break
            verified = entries.get(record.entry)
            if verified is None:
                continue
            pick = take(verified)
            if pick is None:
                if budget - cycling > 0:
                    add(record.box, ('drain', record))
                    report.rotations.append(record.id)
                    budget -= 1
                continue
            add(pick[0].box_id, ('start', (record.entry, pick[1], record.id, pick[2])))
            record.rotating = pick[0].box_id
            self._put_record(record)
            report.rotations.append(record.id)
            budget -= 1
        return ops

    def _leased(self, record: InstanceRecord) -> bool:
        """LEASED on its card, healthy, and not on its way out."""
        box = self.boxes.boxes.get(record.box)
        if box is None or box.status != IDLE or record.draining or not record.healthy:
            return False
        card = box.card(record.uuid)
        return card.state == LEASED and card.instance_id == record.id

    def _execute(
        self,
        box_id: str,
        box_ops: list[BoxOp],
        entries: dict[str, VerifiedEntry],
        runners: dict[str, HostRunner],
        report: ReconcileReport,
    ) -> None:
        """One box's work, in one visit: drains first (they free cards), then starts. Holds the box's lock throughout
        when there are box locks, so no proof round lands on a card mid-start."""
        box = self._box(box_id)
        runner = self._runner(box, runners)
        found: list[str] = []

        def bind_address() -> str:
            """The box's bridge gateway, looked up at the first start of this visit."""
            if not found:
                found.append(bridge_gateway(runner))
            return found[0]

        with self.box_locks.hold(box_id) if self.box_locks is not None else nullcontext():
            for op in sorted(box_ops, key=lambda o: o[0] != 'drain'):
                if op[0] == 'drain':
                    action = self._drain(box_id, runner, op[1])
                else:
                    entry_id, uuid, replaces, host_port = op[1]
                    action = self._start(box_id, runner, entries[entry_id], uuid, replaces, host_port, bind_address)
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
        record.draining, record.healthy, record.pay_open = True, False, False
        record.stopped_at = record.stopped_at or self.wall()  # the controller's stop: pay ends here at the latest
        self._put_record(record)  # the gateway stops routing before anything is stopped
        if box.status == IDLE and box.card(record.uuid).state in (STARTING, LEASED):
            self._move(box_id, record.uuid, DRAINING, action)
        marks.append(('mark_draining', self.clock()))
        if box.status == IDLE and action.states[0] == LEASED:
            action.detail = self._await_quiet(record.id)  # a benched box is stopped at once: its answers are not wanted
            marks.append(('await_quiet', self.clock()))
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
        waited = f'{action.detail}; ' if action.detail else ''
        action.detail = (
            waited
            + (f'drained in {result.elapsed_s:.1f} s' if result.found else 'no container left')
            + ('' if result.in_time else f' — past drain.max_s {record.drain_max_s} s: failed drain')
        )
        if action.states[0] == LEASED and record.leased_at is not None:
            self._lease_event(box_id, record, result.in_time)
        action.timings_ms = _durations(marks)
        return action

    def _await_quiet(self, instance_id: str) -> str:
        """Before a planned drain stops its container: wait until the gateway has re-read the table (the record is
        already marked draining, so it routes nothing new there) and has no request in flight on the instance. Bounded
        by ``DRAIN_WAIT_MAX_S``; a gateway that cannot be asked gets ``DRAIN_GRACE_S`` instead. Returns what happened,
        for the action's detail."""
        marked, started = self.wall(), self.clock()
        if self.gateway_state is None:
            self.sleep(cfg.DRAIN_GRACE_S)
            return f'no gateway to ask: {cfg.DRAIN_GRACE_S:.0f} s grace'
        while True:
            waited = self.clock() - started
            try:
                state = self.gateway_state()
            except Exception:
                state = None
            if state is not None and 'in_flight' not in state:
                state = None  # an older gateway: routable counts only. Unknown is not quiet
            if state is None:
                if waited >= cfg.DRAIN_GRACE_S:
                    return f'gateway did not answer with its in-flight counts: {waited:.0f} s grace'
            elif float(state.get('refreshed_at') or 0.0) >= marked:
                left = int((state.get('in_flight') or {}).get(instance_id, 0))
                if left <= 0:
                    return f'quiet after {waited:.0f} s'
                if waited >= cfg.DRAIN_WAIT_MAX_S:
                    return f'{left} request(s) still in flight after {waited:.0f} s: stopped anyway'
            elif waited >= cfg.DRAIN_WAIT_MAX_S:
                return f'gateway never re-read the table in {waited:.0f} s: stopped anyway'
            self.sleep(cfg.DRAIN_WAIT_POLL_S)

    def _lease_event(self, box_id: str, record: InstanceRecord, in_time: bool) -> None:
        """A drained lease's standing event: ``clean_lease`` with its leased seconds, or ``drain_failed``. A box benched
        meanwhile gets neither (its bench wrote its own), and a lease the accounting check ended gets no
        ``clean_lease`` (its ``external_use`` event stands for it)."""
        with self._lock:
            box = self._box(box_id)
            if box.status != IDLE or (in_time and record.ended_by):
                return
            detail: dict = {'instance': record.id, 'uuid': record.uuid}
            if in_time:
                detail['leased_s'] = round(max(0.0, (record.stopped_at or self.wall()) - (record.leased_at or 0.0)), 1)
            self.boxes.put(add_event(box, CLEAN_LEASE if in_time else DRAIN_FAILED, self.wall(), **detail))

    def _start(
        self,
        box_id: str,
        runner: HostRunner,
        verified: VerifiedEntry,
        uuid: str,
        replaces: str = '',
        host_port: int | None = None,
        bind_address: Callable[[], str] | None = None,
    ) -> Action:
        manifest = verified.manifest
        instance_id = new_instance_id()
        action = Action('start', box_id, instance_id, verified.entry_id, uuid, False, states=[IDLE])
        if replaces:
            action.detail = f'replacing {replaces}; '
        box = self._box(box_id)
        marks = [('begin', self.clock())]
        self._move(box_id, uuid, STARTING, action, instance_id)
        if manifest.front_door.port is None:
            host_port = None
        elif host_port is None:
            host_port = manifest.front_door.port
        record = InstanceRecord(
            id=instance_id,
            entry=verified.entry_id,
            box=box_id,
            uuid=uuid,
            host=box.host,
            port=box.public_port(host_port) if host_port else None,
            host_port=host_port,
            bind=self.workload_bind,
            drain_type=manifest.drain.type,
            drain_max_s=manifest.drain.max_s,
            replaces=replaces,
        )
        self._put_record(record)
        http = self.http_for(runner, box)
        staged = PrestageReport()
        try:
            address = ''
            if self.workload_bind == BIND_PRIVATE:
                address = (bind_address or (lambda: bridge_gateway(runner)))()
                if isinstance(http, BoxHttp):
                    http.use_gateway(address)  # the same lookup serves the probes
            client = host_port_client(http, manifest, host_port)
            spec = build_run_spec(verified.entry_id, manifest, uuid, instance_id, host_port, address)
            prestage(runner, spec, manifest, self.pull_token, self.clock, report=staged)
            marks.append(('prestage', self.clock()))
            record.container_id = deploy(runner, spec)
            record.started_at = self.wall()
            try:
                info = inspect_container(runner, record.container_id)
            except PlacementError:
                info = None  # the first heartbeat records it instead
            if info is not None:
                record.docker_started_at, record.image_id = info.started_at, info.image_id
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
            # reaches the box settles it: a leftover container is undeployed, a record with none behind it is lost.
            action.detail = f'transport: {type(e).__name__}: {e}'[:300]
            action.timings_ms = _durations(marks)
            return action
        except PlacementError as e:
            action.detail += f'failed start: {e}'[:700]
            self._fail_start(box_id, runner, record, action, manifest)
            action.timings_ms = {
                **_durations(marks + [('undeploy', self.clock())]),
                **{f'prestage.{k}': v for k, v in staged.timings_ms.items()},  # a failed start still measured these
            }
            return action
        record.healthy, record.leased_at = True, self.wall()
        record.health_ok, record.last_health_at, record.health_detail = True, record.leased_at, first.detail
        # Pay starts at the first passing probe after the canary; the heartbeat and later probes confirm it onward.
        record.pay_from = record.pay_through = record.leased_at
        record.pay_open = True
        record.lease_cap_s = lease_cap_s(standing(self._box(box_id).standing_events, record.leased_at), self.rng)
        self._put_record(record)
        self._move(box_id, uuid, LEASED, action)
        self._put_box(record_start(self._box(box_id), True, self.wall()))
        action.ok = True
        action.detail += (
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
        after = record_start(
            self._box(box_id), False, self.wall(), instance=record.id, uuid=record.uuid, reason=action.detail[:200]
        )
        if after.status == BENCHED:
            action.detail += f'; {cfg.FAILED_STARTS_BENCH_AFTER} failed starts in a row: box BENCHED'
            action.states.append(BENCHED)
        self._put_box(after)


def _durations(marks: list[tuple[str, float]]) -> dict[str, float]:
    return {name: round((b - a) * 1000.0, 1) for (_, a), (name, b) in zip(marks, marks[1:])}
