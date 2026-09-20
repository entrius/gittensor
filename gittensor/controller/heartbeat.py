# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The in-lease watch (vault ``24`` §3 WS-D, ``23`` §4a, §5, §7): the generic heartbeat and the manifest health probe
on every LEASED card.

**The generic heartbeat** (every ``HEARTBEAT_INTERVAL_S``, one SSH visit per box with a LEASED card) knows nothing
about the workload and asks three questions:

* **Same card?** Every pinned UUID present, each card's power limit and the NVML library md5 unchanged since the last
  passing full check (``BoxState.identity``).
* **Our container running?** ``docker inspect`` on the container ID our ``docker run`` returned: up, the start time
  and image ID recorded at deploy, an image carrying the blessed digest. A restarted or recreated container fails
  even on the right image; a vanished one (gone, or exited without our stop) fails too (Kimbo 9/15), **unless the
  box was unreachable at a heartbeat since the last good one** (Kimbo 9/16: a clean `gitt down` and return, a reboot):
  then it is ``instance_stopped``, not a cheat: the lease ends at the last good heartbeat, nothing is withheld, the
  card goes to CHECKING for the one-box probe. Gone under an agent that answered throughout stays a bench.
* **Card ours alone?** Every GPU process on a leased card belongs to that card's instance: ``nvidia-smi
  --query-compute-apps`` PIDs, each mapped through the host's ``/proc/<pid>/cgroup`` to a container ID. Positive and
  per card: a PID we cannot attribute to our container fails. NVML only lists processes with a CUDA context, so a
  container started with ``--gpus`` that merely sleeps is invisible to it; one more command in the same visit
  (``DEVICE_HOLDERS_COMMAND``) lists every host process with ``/dev/nvidia<N>``, ``/dev/nvidiactl`` or
  ``/dev/nvidia-uvm`` open and maps each to containers the same way. Any holder outside our instances' containers
  fails, bar the driver's own ``nvidia-persistenced`` running on the host (Kimbo 9/15). Never killed: benched. The scan
  is skipped (and a scan the box's lock was taken during is discarded) while a start, drain or proof holds the box:
  their containers are ours but not yet, or no longer, recorded. The round asks the same question of a card with no
  lease on it (``checks.check_card_free``, over the same command and the same judge, ``checks.foreign_holders``): a
  box whose card something else holds must not draw standby pay until a rotation happens to put work on it.

Any failure benches the box on the fraud ladder, withholds its pay from that instant (``BoxState.withheld_from``, which
WS-F consumes) and undeploys every instance on it with a kill. A visit that gets no answer (SSH down, docker erroring)
is not a verdict: the miss is counted on the instance and nothing is paid for that interval (the heartbeat is a pay
condition). A LEASED card whose box the heartbeat cannot reach carries no traffic and does not stay leased (Kimbo
9/16): the **first** miss marks the instance ``healthy: false`` in ``instances.json`` at once, so the gateway stops
routing to it (a passing heartbeat restores it, and the record and pay cursor stay so a returning agent resumes
cleanly); the ``HEARTBEAT_UNREACHABLE_AFTER``-th miss in a row (~3 min) ends the lease: ``stopped_at`` at the last
good heartbeat (pay ends there, nothing withheld: unreachable is not a cheat), the card LEASED -> CHECKING with an
``instance_unreachable`` standing event, the record left draining for the reconciler to undeploy once the box answers
again, then the one-box probe re-proves the card. The box's own unreachable count (proof rounds) and its 12 h bench
are the round's; a miss here does not touch them. A miss waits out the interval like an answer does, so three misses
span three intervals, not three watch ticks.

**Health while leased.** Per instance, every ``manifest.health.interval_s``, the manifest health probe.
``failure_threshold`` failures in a row replace the replica: undeploy with the manifest's drain, card to CHECKING, a
``health_failed`` standing event, and the reconciler starts a replacement on its next pass. Not a bench: a wedged
workload is not a caught cheat.

**The lease accounting check** (``usage_check.py``), on every visit that ran the heartbeat and passed: per instance
whose manifest ``runtime`` has a counters table (``RUNTIME_COUNTERS``), the runtime's own ``/metrics`` (one ``GET``
through the box HTTP client) between two reads of the gateway's ``/healthz``. Every sample is one ``usage_check`` row
in the operator log. A detection drains the instance to IDLE through the reconciler's planned drain (not a bench; the
lease and its pay end at the detection, nothing withheld), writes one ``external_use`` SOFT standing event, and keeps
the box out of placement for ``EXTERNAL_USE_COOLDOWN_S``; the third inside a week benches the box
(``apply_external_use``). The gateway's decode rate against ``profile.decode_tps_single`` is logged as evidence only.

The watch records each answer in ``instances.json`` (``last_heartbeat_at``, ``heartbeat_ok``, ``heartbeat``: the three
answers and the four pay conditions of ``23`` §7) and takes no box lock (see ``locks.py``).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.checks import foreign_holders
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import (
    CONTAINER_ID,
    DEVICE_HOLDERS_COMMAND,
    NVML_MD5_COMMAND,
    nvidia_smi_command,
    parse_device_holders,
    parse_md5,
    parse_nvidia_smi,
)
from gittensor.controller.checks.state import (
    BENCHED,
    CARD_OURS_ALONE,
    CHECKING,
    DRAINING,
    HEALTH_FAILED,
    IDLE,
    LEASED,
    OUR_CONTAINER,
    SAME_CARD,
    BoxState,
    CardTransitionError,
    StateStore,
    add_event,
    apply_external_use,
    apply_heartbeat_failure,
    apply_instance_stopped,
    apply_instance_unreachable,
    mark_reachable,
    transition_card,
)
from gittensor.controller.locks import BoxLocks
from gittensor.controller.manifest import Drain, Manifest
from gittensor.controller.reconcile import InstanceRecord, InstanceStore
from gittensor.controller.registry import Registry, RegistryError
from gittensor.controller.runspec import (
    BoxHttp,
    HttpClient,
    PlacementError,
    host_port_client,
    inspect_container,
    probe_health,
    repo_digests_command,
    undeploy,
)
from gittensor.controller.ssh import SshTransportError
from gittensor.controller.ssh.certs import CertificateError
from gittensor.controller.usage_check import (
    THROUGHPUT_LOW,
    Sample,
    Track,
    gateway_view,
    judge,
    output_ceiling,
    runtime_counters,
    throughput_evidence,
)

_TRANSPORT = (SshTransportError, CertificateError)
COMPUTE_APPS_COMMAND = 'nvidia-smi --query-compute-apps=pid,gpu_uuid --format=csv,noheader'
_APP_LINE = re.compile(r'^\s*(\d+)\s*,\s*(GPU-[0-9A-Za-z-]+)\s*$')


def cgroup_command(pids: list[int]) -> str:
    """Each PID's cgroup file through PID 1's root: the host's procfs, whatever the agent container's own is."""
    listed = ' '.join(str(int(p)) for p in pids)
    return (
        f'for p in {listed}; do printf \'== %s\\n\' "$p"; '
        f'cat {cfg.HOST_ROOT}/proc/"$p"/cgroup 2>/dev/null || echo MISSING; done'
    )


def parse_compute_apps(stdout: str) -> tuple[list[tuple[int, str]], list[str]]:
    """``[(pid, uuid)]`` and the lines that did not parse (a PID nvidia-smi could not resolve prints ``[N/A]``)."""
    apps, odd = [], []
    for line in stdout.splitlines():
        if not line.strip() or line.strip().lower().startswith('no running'):
            continue
        m = _APP_LINE.match(line)
        if m:
            apps.append((int(m.group(1)), m.group(2)))
        else:
            odd.append(line.strip()[:120])
    return apps, odd


def parse_cgroups(stdout: str) -> dict[int, set[str] | None]:
    """``{pid: container IDs in its cgroup paths}``; None for a PID with no cgroup file (not visible on the host)."""
    out: dict[int, set[str] | None] = {}
    pid = None
    for line in stdout.splitlines():
        if line.startswith('== '):
            value = line[3:].strip()
            pid = int(value) if value.isdigit() else None
            if pid is not None:
                out[pid] = set()
            continue
        if pid is None:
            continue
        if line.strip() == 'MISSING':
            out[pid] = None
        elif (ids := out.get(pid)) is not None:
            ids.update(CONTAINER_ID.findall(line))
    return out


# ---------------------------------------------------------------- one heartbeat ---------------------------------------


@dataclass
class Answer:
    ok: bool
    detail: str
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {'ok': self.ok, 'detail': self.detail, **self.evidence}


class NoAnswer(Exception):
    """The visit could not ask a question (docker or nvidia-smi errored in a way that says nothing about the box)."""


@dataclass
class HeartbeatResult:
    at: float
    same_card: Answer
    containers: dict[str, Answer]  # instance id -> our container running?
    alone: dict[str, Answer]  # uuid -> card ours alone? (NVML processes)
    recorded: dict[str, tuple[str, str]] = field(default_factory=dict)  # instance -> (StartedAt, image id) filled now
    devices: Answer | None = None  # card ours alone? (open device handles, box-wide); None: not scanned this visit

    @property
    def failed(self) -> list[str]:
        names = []
        if not self.same_card.ok:
            names.append(SAME_CARD)
        if any(not a.ok for a in self.containers.values()):
            names.append(OUR_CONTAINER)
        if any(not a.ok for a in self.alone.values()) or (self.devices is not None and not self.devices.ok):
            names.append(CARD_OURS_ALONE)
        return names

    @property
    def ok(self) -> bool:
        return not self.failed

    def reasons(self) -> list[str]:
        out = [] if self.same_card.ok else [f'{SAME_CARD}: {self.same_card.detail}']
        out += [f'{OUR_CONTAINER} {i}: {a.detail}' for i, a in self.containers.items() if not a.ok]
        out += [f'{CARD_OURS_ALONE} {u[:12]}…: {a.detail}' for u, a in self.alone.items() if not a.ok]
        if self.devices is not None and not self.devices.ok:
            out.append(f'{CARD_OURS_ALONE} (device handles): {self.devices.detail}')
        return out

    def gone(self) -> list[str]:
        """The instances whose container this visit found gone or stopped (not restarted, not another image)."""
        return [i for i, a in self.containers.items() if not a.ok and a.evidence.get('gone')]

    def without(self, instance_ids: set[str], uuids: set[str]) -> HeartbeatResult:
        """This result judged without those instances (stopped, not cheated): their answers are dropped."""
        return HeartbeatResult(
            self.at,
            self.same_card,
            {i: a for i, a in self.containers.items() if i not in instance_ids},
            {u: a for u, a in self.alone.items() if u not in uuids},
            self.recorded,
            self.devices,
        )

    def evidence_for(self, record: InstanceRecord) -> dict:
        container = self.containers.get(record.id, Answer(False, 'not asked'))
        alone = self.alone.get(record.uuid, Answer(False, 'not asked'))
        devices = (
            self.devices.as_dict() if self.devices is not None else {'ok': None, 'detail': 'not scanned: box busy'}
        )
        return {
            'at': self.at,
            'ok': self.ok,
            SAME_CARD: self.same_card.as_dict(),
            OUR_CONTAINER: container.as_dict(),
            CARD_OURS_ALONE: {
                **alone.as_dict(),
                'ok': alone.ok and devices['ok'] is not False,
                'device_handles': devices,
            },
            # The four pay conditions (23 §7) as this visit saw them; WS-F pays a block only when all four hold.
            'pay': {
                'we_started': bool(container.evidence.get('we_started')),
                'blessed_digest': bool(container.evidence.get('blessed_digest')),
                'healthy': bool(record.health_ok),
                'heartbeat': self.ok,
            },
        }


def _same_card(runner: HostRunner, box: BoxState) -> Answer:
    smi = runner.run(nvidia_smi_command(), timeout=cfg.NVIDIA_SMI_TIMEOUT_S)
    if not smi.ok:
        return Answer(False, f'nvidia-smi exit {smi.exit_code}: {(smi.stderr or smi.stdout).strip()[:200]}')
    try:
        gpus = {g.uuid: g for g in parse_nvidia_smi(smi.stdout)}
    except ValueError as e:
        return Answer(False, str(e)[:200])
    missing = [u for u in box.pinned_uuids if u not in gpus]
    if missing:
        return Answer(False, 'pinned card(s) missing: ' + ', '.join(missing), {'observed': sorted(gpus)})
    problems, notes = [], []
    recorded_power = box.identity.get('power_limits')
    baseline_power: dict[str, Any] = dict(recorded_power) if isinstance(recorded_power, dict) else {}
    power = {u: gpus[u].power_limit_w for u in box.pinned_uuids}
    for uuid, limit in power.items():
        was = baseline_power.get(uuid)
        if was is None:
            notes.append(f'{uuid[:12]}… power limit: no baseline')
        elif limit is None or abs(limit - float(was)) > cfg.POWER_LIMIT_TOLERANCE_W:
            problems.append(f'{uuid[:12]}… power limit {limit} W, was {was} W at the last full check')
    md5_result = runner.run(NVML_MD5_COMMAND, timeout=cfg.SSH_COMMAND_TIMEOUT_S)
    md5 = parse_md5(md5_result.stdout) if md5_result.ok else ''
    baseline_md5 = str(box.identity.get('nvml_md5') or '')
    if not md5:
        problems.append('libnvidia-ml.so.1 not found or unhashed')
    elif not baseline_md5:
        notes.append('nvml md5: no baseline')
    elif md5 != baseline_md5:
        problems.append(f'NVML lib md5 {md5}, was {baseline_md5} at the last full check')
    evidence = {'power_limits': power, 'nvml_md5': md5, 'notes': notes}
    if problems:
        return Answer(False, '; '.join(problems), evidence)
    return Answer(True, f'{len(box.pinned_uuids)} pinned present, power + NVML unchanged', evidence)


def _our_container(runner: HostRunner, record: InstanceRecord, manifest: Manifest | None) -> tuple[Answer, tuple]:
    """The answer, and ``(StartedAt, image id)`` when this visit had to record them (a record from before WS-D)."""
    try:
        info = inspect_container(runner, record.container_id)
    except PlacementError as e:
        raise NoAnswer(str(e)) from e
    if info is None:
        return Answer(False, f'container {record.container_id[:12]} vanished (not stopped by us)', {'gone': True}), ()
    evidence: dict[str, Any] = {'status': info.status, 'started_at': info.started_at, 'image_id': info.image_id}
    if not info.up:
        return Answer(False, f'container {info.status} (not stopped by us)', {**evidence, 'gone': True}), ()
    recorded: tuple = ()
    if not record.docker_started_at or not record.image_id:
        recorded = (record.docker_started_at or info.started_at, record.image_id or info.image_id)
        started_at, image_id = recorded
    else:
        started_at, image_id = record.docker_started_at, record.image_id
    problems = []
    we_started = info.container_id == record.container_id and info.started_at == started_at
    if info.started_at != started_at:
        problems.append(f'restarted: StartedAt {info.started_at}, ours {started_at}')
    digest = manifest.image_digest if manifest is not None else ''
    blessed = info.image_id == image_id
    if not blessed:
        problems.append(f'image {info.image_id[:19]} is not the image we deployed ({image_id[:19]})')
    elif digest:
        digests = runner.run(repo_digests_command(info.image_id), timeout=cfg.SSH_COMMAND_TIMEOUT_S)
        if not digests.ok:
            raise NoAnswer(f'docker image inspect: exit {digests.exit_code}')
        blessed = any(d.strip().endswith('@' + digest) for d in digests.stdout.split(','))
        if not blessed:
            problems.append(f'image does not carry the blessed digest {digest[:19]}')
    evidence.update(we_started=we_started, blessed_digest=blessed)
    if recorded:
        evidence['recorded_now'] = True
    if problems:
        return Answer(False, '; '.join(problems), evidence), recorded
    return Answer(True, f'{info.status}, started {info.started_at}', evidence), recorded


def _alone(runner: HostRunner, records: list[InstanceRecord]) -> dict[str, Answer]:
    apps_result = runner.run(COMPUTE_APPS_COMMAND, timeout=cfg.NVIDIA_SMI_TIMEOUT_S)
    if not apps_result.ok:
        failure = Answer(False, f'cannot list GPU processes: nvidia-smi exit {apps_result.exit_code}')
        return {r.uuid: failure for r in records}
    apps, odd = parse_compute_apps(apps_result.stdout)
    ours = {r.uuid: r.container_id for r in records}
    pids = sorted({pid for pid, uuid in apps if uuid in ours})
    cgroups = parse_cgroups(runner.run(cgroup_command(pids), timeout=cfg.SSH_COMMAND_TIMEOUT_S).stdout) if pids else {}
    out = {}
    for uuid, container_id in ours.items():
        foreign = []
        mine = [pid for pid, u in apps if u == uuid]
        for pid in mine:
            ids = cgroups.get(pid)
            if ids is None:
                foreign.append(f'pid {pid} not visible on the host')
            elif container_id not in ids:
                where = ', '.join(sorted(i[:12] for i in ids)) or 'no container'
                foreign.append(f'pid {pid} in {where}')
        if odd:
            foreign.append('unattributable nvidia-smi line(s): ' + '; '.join(odd))
        evidence = {'pids': mine}
        if foreign:
            out[uuid] = Answer(False, '; '.join(foreign)[:400], evidence)
        else:
            out[uuid] = Answer(True, f'{len(mine)} GPU process(es), all ours', evidence)
    return out


def _device_holders(runner: HostRunner, ours: set[str]) -> Answer:
    """Every open NVIDIA device handle on the host must sit in one of ``ours`` (our instances' container IDs on this
    box), or be the host's own persistence daemon."""
    result = runner.run(DEVICE_HOLDERS_COMMAND, timeout=cfg.SSH_COMMAND_TIMEOUT_S)
    if not result.ok:
        why = (result.stderr or result.stdout).strip()[:200]
        return Answer(False, f'cannot scan device handles: exit {result.exit_code}: {why}')
    holders = parse_device_holders(result.stdout)
    # The buckets are the round's public phrase; a heartbeat bench has its own.
    foreign, exited, _ = foreign_holders(holders, ours)
    evidence = {'holders': sorted(holders), 'exited_mid_scan': exited}
    if foreign:
        return Answer(False, 'foreign device holder(s): ' + '; '.join(foreign)[:400], evidence)
    return Answer(True, f'{len(holders)} device holder(s), none foreign', evidence)


def run_heartbeat(
    runner: HostRunner,
    box: BoxState,
    records: list[InstanceRecord],
    manifests: dict[str, Manifest | None],
    now: float,
    ours: set[str] | None = None,
) -> HeartbeatResult:
    """One heartbeat over one box's leased instances. With ``ours`` (every container ID of our instances on the box)
    the open device handles are scanned too. Raises a transport error or ``NoAnswer`` when it gets no answer."""
    same_card = _same_card(runner, box)
    containers, recorded = {}, {}
    for record in records:
        answer, filled = _our_container(runner, record, manifests.get(record.entry))
        containers[record.id] = answer
        if filled:
            recorded[record.id] = filled
    alone = _alone(runner, records)
    devices = _device_holders(runner, ours) if ours is not None else None
    return HeartbeatResult(now, same_card, containers, alone, recorded, devices)


def observe_pay(record: InstanceRecord, now: float) -> None:
    """Fold one check's outcome into the record's pay span (``23`` §7). The four conditions hold when the last heartbeat
    passed (it answers "we started it" and "the blessed digest") and the last health probe passed. While they hold the
    span runs through the older of the two checks; any failure or miss closes it, so pay stops at the last passing
    check; the next check that finds all four holding opens a new span at that instant."""
    pay = (record.heartbeat or {}).get('pay') or {}
    holds = (
        record.heartbeat_ok is True
        and record.health_ok is True
        and bool(pay.get('we_started'))
        and bool(pay.get('blessed_digest'))
    )
    if not holds:
        record.pay_open = False
        return
    if not record.pay_open or record.pay_from is None:
        record.pay_from = record.pay_through = now
        record.pay_open = True
        return
    confirmed = min(record.last_heartbeat_at or 0.0, record.last_health_at or 0.0)
    record.pay_through = max(record.pay_through or record.pay_from, confirmed)


# ---------------------------------------------------------------- the watch -------------------------------------------


@dataclass
class WatchAction:
    kind: str  # heartbeat | health | bench | replace | miss | stopped | unreachable | external_use
    box: str
    instance: str = ''
    uuid: str = ''
    ok: bool = True
    detail: str = ''
    states: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class WatchReport:
    actions: list[WatchAction] = field(default_factory=list)
    unreachable: dict[str, str] = field(default_factory=dict)
    visited: list[str] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)  # the lease accounting check's rows, one per instance sampled

    @property
    def ok(self) -> bool:
        return not self.unreachable and all(a.ok for a in self.actions)


@dataclass
class Watch:
    boxes: StateStore
    instances: InstanceStore
    registry: Registry
    make_runner: Callable[[BoxState], HostRunner]
    http_for: Callable[[HostRunner, BoxState], HttpClient] = lambda runner, box: BoxHttp(runner)
    heartbeat_interval_s: float = cfg.HEARTBEAT_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)  # the shared state-write lock
    box_locks: BoxLocks | None = None  # `run`'s per-box locks: never taken here, only looked at (the device scan)
    # The gateway's /healthz as a dict, or None when it cannot be read (``reconcile.gateway_healthz``). None here: no
    # gateway to ask, and the lease accounting check makes no judgement.
    gateway_state: Callable[[], dict | None] | None = None
    usage_tracks: dict[str, Track] = field(default_factory=dict, repr=False)  # instance id -> the check's baseline

    def _box_busy(self, box_id: str) -> bool:
        return self.box_locks is not None and self.box_locks.held(box_id)

    def leased(self) -> dict[str, list[InstanceRecord]]:
        """Instances on LEASED cards, by box: the ones the watch looks after."""
        out: dict[str, list[InstanceRecord]] = {}
        with self.lock:
            for record in sorted(self.instances.instances.values(), key=lambda r: r.id):
                box = self.boxes.boxes.get(record.box)
                if record.draining or box is None or box.status != IDLE or not box.host:
                    continue
                card = box.card(record.uuid)
                if card.state == LEASED and card.instance_id == record.id:
                    out.setdefault(record.box, []).append(record)
        return out

    def _heartbeat_due(self, records: list[InstanceRecord], now: float) -> bool:
        """Due ``heartbeat_interval_s`` after the last visit that asked, answered or missed (``heartbeat['at']``)."""

        def due(r: InstanceRecord) -> bool:
            last = (r.heartbeat or {}).get('at', r.last_heartbeat_at)
            return last is None or now - last >= self.heartbeat_interval_s

        return any(due(r) for r in records)

    @staticmethod
    def _health_due(record: InstanceRecord, manifest: Manifest | None, now: float) -> bool:
        if manifest is None:
            return False
        last = record.last_health_at or record.leased_at
        return last is None or now - last >= manifest.health.interval_s

    def _manifests(self, records: list[InstanceRecord]) -> dict[str, Manifest | None]:
        out: dict[str, Manifest | None] = {}
        for entry in {r.entry for r in records}:
            try:
                out[entry] = self.registry.read(entry).manifest  # re-verified; one that fails is drained by reconcile
            except RegistryError:
                out[entry] = None
        return out

    def run_pass(self) -> WatchReport:
        """Visit every box with a heartbeat or health probe due, all boxes in parallel."""
        report = WatchReport()
        now = self.wall()
        due: dict[str, tuple[list[InstanceRecord], dict[str, Manifest | None]]] = {}
        leased = self.leased()
        with self.lock:
            live = {r.id for records in leased.values() for r in records}
            for instance_id in [i for i in self.usage_tracks if i not in live]:
                del self.usage_tracks[instance_id]  # no longer leased: a lease that comes back starts a new baseline
        for box_id, records in leased.items():
            manifests = self._manifests(records)
            if self._heartbeat_due(records, now) or any(self._health_due(r, manifests[r.entry], now) for r in records):
                due[box_id] = (records, manifests)
        report.visited = sorted(due)
        if due:
            with ThreadPoolExecutor(max_workers=len(due)) as pool:
                list(pool.map(lambda box_id: self._visit(box_id, *due[box_id], report), sorted(due)))
        return report

    # -- one box ----------------------------------------------------------------------------------------------------

    def _visit(
        self, box_id: str, records: list[InstanceRecord], manifests: dict[str, Manifest | None], report: WatchReport
    ) -> None:
        box = self.boxes.boxes[box_id]
        runner = self.make_runner(box)
        try:
            beat = self._heartbeat_due(records, self.wall())
            if beat and not self._heartbeat(box_id, box, runner, records, manifests, report):
                return
            for record in records:
                current = self.instances.instances.get(record.id)
                if current is None or current.draining:
                    continue
                manifest = manifests.get(current.entry)
                if manifest is not None and self._health_due(current, manifest, self.wall()):
                    if not self._health(box_id, box, runner, current, manifest, report):
                        return
            if beat:
                self._usage(box_id, box, runner, records, manifests, report)
        except Exception as e:  # a bug must not kill the watch loop; the next tick retries
            with self.lock:
                report.unreachable[box_id] = f'{type(e).__name__}: {e}'[:300]
        finally:
            getattr(runner, 'close', lambda: None)()

    def _heartbeat(self, box_id, box, runner, records, manifests, report) -> bool:
        """True when the visit may go on to health probes."""
        started, now = self.clock(), self.wall()
        scan = not self._box_busy(box_id)
        with self.lock:
            ours = self.instances.containers_on(box_id) if scan else None
        try:
            result = run_heartbeat(runner, box, records, manifests, now, ours)
        except (*_TRANSPORT, NoAnswer) as e:
            self._miss(box_id, runner, records, f'{type(e).__name__}: {e}'[:300], now, report)
            return False
        if scan and self._box_busy(box_id):
            result.devices = None  # a start, drain or proof took the box mid-scan: its containers are no verdict
        took = {'heartbeat': round((self.clock() - started) * 1000.0, 1)}
        with self.lock:
            # A container gone after a missed heartbeat is a stop, not a cheat (Kimbo 9/16): judged apart, below.
            stopped = [
                r for r in records
                if r.id in result.gone() and (self.instances.instances.get(r.id) or r).heartbeat_misses > 0
            ]  # fmt: skip
            result = result.without({r.id for r in stopped}, {r.uuid for r in stopped})
            if box_id in self.boxes.boxes:
                reached = mark_reachable(self.boxes.boxes[box_id], now)
                if reached is not self.boxes.boxes[box_id]:
                    self.boxes.put(reached)
            for record in records:
                current = self.instances.instances.get(record.id)
                if current is None or current.draining or record.id in {r.id for r in stopped}:
                    continue
                if record.id in result.recorded:
                    current.docker_started_at, current.image_id = result.recorded[record.id]
                current.last_heartbeat_at, current.heartbeat_ok, current.heartbeat_misses = now, result.ok, 0
                current.healthy = current.health_ok is not False  # routable again after a miss made it not
                current.heartbeat = result.evidence_for(current)
                observe_pay(current, now)
                self.instances.put(current)
                report.actions.append(
                    WatchAction(
                        'heartbeat', box_id, record.id, record.uuid, result.ok,
                        'ok' if result.ok else '; '.join(result.reasons())[:500], [LEASED], took,
                    )
                )  # fmt: skip
        for record in stopped:
            self._stop(box_id, runner, record, 'heartbeat', report)
        if result.ok:
            return True
        self._bench(box_id, runner, records, result, report)
        return False

    def _stop(self, box_id: str, runner: HostRunner, record: InstanceRecord, via: str, report: WatchReport) -> None:
        """Our container was gone once the agent answered again after missed heartbeats: the lease ended at the last
        good heartbeat (pay through there, nothing withheld), ``instance_stopped`` on the box, the card to CHECKING for
        the one-box probe, and whatever the container left behind removed. No bench."""
        now = self.wall()
        with self.lock:
            current = self.instances.instances.get(record.id)
            if current is None:
                return
            last_good = current.last_heartbeat_at or current.leased_at or now
            misses = current.heartbeat_misses
            current.draining, current.healthy, current.pay_open = True, False, False
            current.stopped_at = min(current.stopped_at, last_good) if current.stopped_at is not None else last_good
            current.drain_type, current.drain_max_s = 'kill', 0
            current.heartbeat = {'at': now, 'ok': False, 'stopped': True, 'missed_heartbeats': misses}
            self.instances.put(current)
            box = self.boxes.boxes.get(box_id)
            if box is not None:
                self.boxes.put(
                    apply_instance_stopped(
                        box, record.uuid, now, instance=record.id, missed_heartbeats=misses,
                        lease_ended_at=last_good, via=via,
                    )
                )  # fmt: skip
        action = WatchAction(
            'stopped', box_id, record.id, record.uuid, False,
            f'container gone after {misses} missed heartbeat(s): instance stopped, not a cheat; lease ended at the '
            f'last good heartbeat ({now - last_good:.0f} s ago), nothing withheld; card CHECKING for the re-prove',
            [LEASED, CHECKING],
        )  # fmt: skip
        try:
            undeploy(runner, record.id, Drain('kill'), self.clock)  # a stopped container left behind is removed
        except (*_TRANSPORT, PlacementError) as e:
            action.detail += f'; undeploy failed ({type(e).__name__}), reconcile retries'
        else:
            with self.lock:
                self.instances.remove(record.id)
        with self.lock:
            report.actions.append(action)

    def _miss(self, box_id, runner, records, why: str, now: float, report: WatchReport) -> None:
        """No answer: a miss on every instance (no pay for the interval, ``healthy: false`` at once so the gateway
        stops routing to it); the ``HEARTBEAT_UNREACHABLE_AFTER``-th in a row ends the lease (card CHECKING, the record
        left draining for the reconciler, no bench, nothing withheld)."""
        with self.lock:
            report.unreachable[box_id] = why
            ended = []
            for record in records:
                current = self.instances.instances.get(record.id)
                if current is None or current.draining:
                    continue
                current.heartbeat_misses += 1
                current.heartbeat_ok, current.pay_open, current.healthy = (
                    None,
                    False,
                    False,
                )  # no answer: no pay, no traffic
                current.heartbeat = {'at': now, 'ok': None, 'error': why}
                misses = current.heartbeat_misses
                in_a_row = f'{misses}/{cfg.HEARTBEAT_UNREACHABLE_AFTER} in a row'
                if misses < cfg.HEARTBEAT_UNREACHABLE_AFTER:
                    self.instances.put(current)
                    report.actions.append(
                        WatchAction(
                            'miss', box_id, record.id, record.uuid, False,
                            f'no answer, no verdict ({in_a_row}): unroutable until a heartbeat passes: {why}',
                        )
                    )  # fmt: skip
                    continue
                last_good = current.last_heartbeat_at or current.leased_at or now
                current.draining, current.stopped_at = True, last_good  # the lease ended at the last good heartbeat
                current.drain_type, current.drain_max_s = 'kill', 0
                self.instances.put(current)
                ended.append((record, misses, last_good))
            box = self.boxes.boxes.get(box_id)
            for record, misses, last_good in ended:
                if box is not None:
                    box = apply_instance_unreachable(
                        box, record.uuid, now, instance=record.id, failed_heartbeats=misses, lease_ended_at=last_good
                    )
                    self.boxes.put(box)
                report.actions.append(
                    WatchAction(
                        'unreachable', box_id, record.id, record.uuid, False,
                        f'no answer {misses} heartbeats in a row: lease ended at the last good heartbeat '
                        f'({now - last_good:.0f} s ago), nothing withheld, no bench; card CHECKING, the reconciler '
                        f'undeploys the instance once the box answers: {why}',
                        [LEASED, CHECKING],
                    )
                )  # fmt: skip

    def _bench(self, box_id, runner, records, result: HeartbeatResult, report: WatchReport) -> None:
        """BENCHED + pay withheld at once (one short state write), then every instance on the box undeployed with a
        kill. An undeploy that fails leaves its record draining; the reconciler finishes it."""
        now = self.wall()
        with self.lock:
            self.boxes.put(
                apply_heartbeat_failure(self.boxes.boxes[box_id], result.failed, now, reasons=result.reasons())
            )
            victims = self._mark_for_kill(box_id)
        action = WatchAction('bench', box_id, ok=False, detail='; '.join(result.reasons())[:500], states=[BENCHED])
        self._kill(runner, victims, action, report)

    def _mark_for_kill(self, box_id: str, now: float | None = None) -> list[InstanceRecord]:
        """Under ``self.lock``: every instance on the box draining with a kill, so the gateway stops routing at once
        and pay closes at this moment (``pay_open`` off, ``stopped_at`` set for the ledger)."""
        now = time.time() if now is None else now
        victims = self.instances.on_box(box_id)
        for record in victims:
            record.draining, record.healthy, record.pay_open = True, False, False
            record.stopped_at = record.stopped_at or now
            record.drain_type, record.drain_max_s = 'kill', 0
            self.instances.put(record)
        return victims

    def _kill(self, runner, victims: list[InstanceRecord], action: WatchAction, report: WatchReport) -> None:
        undeployed = []
        for record in victims:
            try:
                undeploy(runner, record.id, Drain('kill'), self.clock)
            except (*_TRANSPORT, PlacementError) as e:
                action.detail += f'; undeploy {record.id} failed ({type(e).__name__}), reconcile retries'
                continue
            with self.lock:
                self.instances.remove(record.id)
            undeployed.append(record.id)
        action.detail += f'; undeployed {", ".join(undeployed) or "nothing"}'
        with self.lock:
            report.actions.append(action)

    def _health(self, box_id, box, runner, record: InstanceRecord, manifest: Manifest, report: WatchReport) -> bool:
        started, now = self.clock(), self.wall()
        try:
            client = host_port_client(self.http_for(runner, box), manifest, record.host_port)
            outcome = probe_health(client, manifest, runner, record.container_id)
        except (*_TRANSPORT, PlacementError) as e:
            with self.lock:
                report.unreachable[box_id] = f'{type(e).__name__}: {e}'[:300]
            return False  # no answer is not a failed probe
        took = {'health': round((self.clock() - started) * 1000.0, 1)}
        threshold = manifest.health.failure_threshold
        with self.lock:
            current = self.instances.instances.get(record.id)
            if current is None or current.draining:
                return True
            current.last_health_at, current.health_ok, current.health_detail = now, outcome.ok, outcome.detail
            current.health_failures = 0 if outcome.ok else current.health_failures + 1
            current.healthy = outcome.ok
            observe_pay(current, now)
            self.instances.put(current)
            failures = current.health_failures
            report.actions.append(
                WatchAction(
                    'health', box_id, record.id, record.uuid, outcome.ok,
                    f'{outcome.detail}' + ('' if outcome.ok else f' ({failures}/{threshold})'), [LEASED], took,
                )
            )  # fmt: skip
        if failures >= threshold:
            self._replace(box_id, runner, current, manifest, report)
        return True

    def _move(self, box_id: str, uuid: str, to: str, instance_id: str, states: list[str]) -> None:
        with self.lock:
            box = self.boxes.boxes.get(box_id)
            if box is None or box.status != IDLE or uuid not in box.cards or box.cards[uuid].state == to:
                return
            if box.cards[uuid].instance_id not in (instance_id, ''):
                return  # the card has moved on to another instance
            try:
                self.boxes.put(transition_card(box, uuid, to, self.wall()))
            except CardTransitionError:
                return
            states.append(to)

    def _replace(self, box_id, runner, record: InstanceRecord, manifest: Manifest, report: WatchReport) -> None:
        """``failure_threshold`` probe failures in a row: undeploy with the manifest's drain, card to CHECKING, a
        ``health_failed`` standing event. The reconciler's next pass starts the replacement elsewhere."""
        now = self.wall()
        action = WatchAction('replace', box_id, record.id, record.uuid, False, states=[LEASED])
        with self.lock:
            record.draining, record.healthy, record.pay_open = True, False, False
            record.stopped_at = record.stopped_at or now
            self.instances.put(record)
            self.boxes.put(
                add_event(
                    self.boxes.boxes[box_id], HEALTH_FAILED, now, instance=record.id, uuid=record.uuid,
                    entry=record.entry, failures=record.health_failures, detail=record.health_detail,
                )
            )  # fmt: skip
        self._move(box_id, record.uuid, DRAINING, record.id, action.states)
        try:
            result = undeploy(runner, record.id, manifest.drain, self.clock)
        except (*_TRANSPORT, PlacementError) as e:
            action.detail = (
                f'{record.health_failures} health failures; undeploy failed ({type(e).__name__}), reconcile retries'
            )
            with self.lock:
                report.actions.append(action)
            return
        with self.lock:
            self.instances.remove(record.id)
        self._move(box_id, record.uuid, CHECKING, record.id, action.states)
        action.detail = (
            f'{record.health_failures} health failures in a row ({record.health_detail}): replica replaced, '
            + (f'drained in {result.elapsed_s:.1f} s' if result.found else 'no container left')
        )
        with self.lock:
            report.actions.append(action)

    # -- the lease accounting check ---------------------------------------------------------------------------------

    def _gateway(self) -> Any:
        if self.gateway_state is None:
            return None
        try:
            return self.gateway_state()
        except Exception:
            return None

    def _counters(self, runner, box, record: InstanceRecord, manifest: Manifest) -> tuple[dict | None, str]:
        """The runtime's own counters for one instance, or None and why not."""
        table = cfg.RUNTIME_COUNTERS.get(manifest.runtime)
        if table is None:
            return None, f'runtime {manifest.runtime!r} has no counters table'
        if manifest.front_door.port is None:
            return None, 'no front-door port to read /metrics on'
        try:
            client = host_port_client(self.http_for(runner, box), manifest, record.host_port)
            response = client.request('GET', manifest.front_door.port, '/metrics')
        except (*_TRANSPORT, PlacementError) as e:
            return None, f'/metrics not read: {type(e).__name__}'
        if response.status != 200:
            return None, f'/metrics -> {response.status or response.error or "no response"}'
        counters = runtime_counters(response.body, table)
        if counters is None:
            return None, f'/metrics has no {table["completion_tokens"][0]} completion series'
        return counters, ''

    def _usage(self, box_id, box, runner, records: list[InstanceRecord], manifests, report: WatchReport) -> None:
        """One sample per instance still leased after this visit: the gateway, the runtime's counters, the gateway
        again. Every sample is a ``usage_check`` row; a detection ends the lease (``_external_use``)."""
        with self.lock:
            current = [c for r in records if (c := self.instances.instances.get(r.id)) is not None and not c.draining]
        current = [r for r in current if manifests.get(r.entry) is not None]
        if not current:
            return
        before = gateway_view(self._gateway())
        read = {r.id: self._counters(runner, box, r, manifests[r.entry]) for r in current}  # type: ignore[arg-type]
        after = gateway_view(self._gateway()) if before is not None else None
        why_gateway = (
            'no gateway to ask' if self.gateway_state is None else 'gateway /healthz not read, or without totals'
        )
        now = self.wall()
        for record in current:
            manifest = manifests[record.entry]
            assert manifest is not None
            counters, why = read[record.id]
            sample = Sample(
                now,
                counters,
                before,
                after,
                output_ceiling(manifest),
                why or ('' if after is not None else why_gateway),
            )
            with self.lock:
                track, judgement = judge(self.usage_tracks.get(record.id, Track()), sample, record.id)
                self.usage_tracks[record.id] = track
                if judgement.log:
                    report.usage.append(
                        {
                            'kind': judgement.kind, 'box': box_id, 'instance': record.id, 'entry': record.entry,
                            'uuid': record.uuid, 'detail': judgement.detail, **judgement.numbers,
                        }
                    )  # fmt: skip
                low = (
                    throughput_evidence(after.of(record.id), manifest.profile.get('decode_tps_single'))
                    if after
                    else None
                )
                if low is not None:  # evidence only: never a strike, a drain or a standing event
                    report.usage.append(
                        {'kind': THROUGHPUT_LOW, 'box': box_id, 'instance': record.id, 'entry': record.entry, **low}
                    )
            if judgement.detected:
                self._external_use(box_id, record, judgement.numbers, report)

    def _external_use(self, box_id: str, record: InstanceRecord, numbers: dict, report: WatchReport) -> None:
        """A detection: the lease ends now (pay through now, nothing withheld), the record is marked draining so the
        reconciler drains it through the planned drain (it waits for the gateway), an ``external_use`` SOFT event goes
        on the box, which takes no new lease for ``EXTERNAL_USE_COOLDOWN_S``. The third inside a week benches the box:
        every instance on it is then drained as a benched box's are."""
        now = self.wall()
        action = WatchAction('external_use', box_id, record.id, record.uuid, False, states=[LEASED])
        with self.lock:
            current = self.instances.instances.get(record.id)
            box = self.boxes.boxes.get(box_id)
            if current is None or current.draining or box is None:
                return
            current.draining, current.healthy, current.pay_open = True, False, False
            current.stopped_at = min(current.stopped_at, now) if current.stopped_at is not None else now
            current.ended_by = 'external_use'
            self.instances.put(current)
            summary = {
                k: numbers[k] for k in ('runtime_delta', 'gateway_delta', 'surplus', 'threshold') if k in numbers
            }
            after = apply_external_use(box, record.uuid, now, instance=record.id, entry=record.entry, **numbers)
            self.boxes.put(after)
            action.detail = f'{cfg.EXTERNAL_USE_REASON}: ' + ', '.join(f'{k} {v:.0f}' for k, v in summary.items())
            if after.status == BENCHED:
                for other in self.instances.on_box(box_id):
                    other.draining, other.healthy, other.pay_open = True, False, False
                    other.stopped_at = other.stopped_at or now
                    self.instances.put(other)
                action.states.append(BENCHED)
                action.detail += (
                    f'; the {cfg.EXTERNAL_USE_BENCH_AFTER}rd inside {cfg.EXTERNAL_USE_WINDOW_S / 86_400:.0f} days: box '
                    f'BENCHED until {after.bench_until:.0f}, every instance on it drained'
                )
            else:
                action.detail += (
                    '; lease ended, the planned drain returns the card to IDLE; no new lease on the box for '
                    f'{cfg.EXTERNAL_USE_COOLDOWN_S:.0f} s'
                )
            self.usage_tracks.pop(record.id, None)
            report.actions.append(action)
