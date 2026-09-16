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
  even on the right image; a vanished one (gone, or exited without our stop) fails too (Kimbo 9/15).
* **Card ours alone?** Every GPU process on a leased card belongs to that card's instance: ``nvidia-smi
  --query-compute-apps`` PIDs, each mapped through the host's ``/proc/<pid>/cgroup`` to a container ID. Positive and
  per card: a PID we cannot attribute to our container fails. NVML only lists processes with a CUDA context, so a
  container started with ``--gpus`` that merely sleeps is invisible to it; one more command in the same visit
  (``DEVICE_HOLDERS_COMMAND``) lists every host process with ``/dev/nvidia<N>``, ``/dev/nvidiactl`` or
  ``/dev/nvidia-uvm`` open and maps each to containers the same way. Any holder outside our instances' containers
  fails, bar the driver's own ``nvidia-persistenced`` running on the host (Kimbo 9/15). Never killed: benched. The scan
  is skipped (and a scan the box's lock was taken during is discarded) while a start, drain or proof holds the box:
  their containers are ours but not yet, or no longer, recorded.

Any failure benches the box on the fraud ladder, withholds its pay from that instant (``BoxState.withheld_from``, which
WS-F consumes) and undeploys every instance on it with a kill. A visit that gets no answer (SSH down, docker erroring)
is not a verdict: the miss is counted on the instance and nothing is paid for that interval (the heartbeat is a pay
condition). It also counts on the box's ``unreachable_count``, the counter unreachable proof rounds use: the
``UNREACHABLE_BENCH_AFTER``-th miss in a row benches the box for the flat ``UNREACHABLE_BENCH_S``, off the fraud ladder,
and undeploys its instances (Kimbo 9/15). Any answered heartbeat or verdict resets it. A miss waits out the interval
like an answer does, so three misses span three intervals, not three watch ticks.

**Health while leased.** Per instance, every ``manifest.health.interval_s``, the manifest health probe.
``failure_threshold`` failures in a row replace the replica: undeploy with the manifest's drain, card to CHECKING, a
``health_failed`` standing event, and the reconciler starts a replacement on its next pass. Not a bench: a wedged
workload is not a caught cheat. ``manifest.profile`` is not judged here (out of scope for WS-D).

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
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import NVML_MD5_COMMAND, nvidia_smi_command, parse_md5, parse_nvidia_smi
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
    apply_heartbeat_failure,
    apply_unreachable,
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

_TRANSPORT = (SshTransportError, CertificateError)
_CONTAINER_ID = re.compile(r'[0-9a-f]{64}')
COMPUTE_APPS_COMMAND = 'nvidia-smi --query-compute-apps=pid,gpu_uuid --format=csv,noheader'
_APP_LINE = re.compile(r'^\s*(\d+)\s*,\s*(GPU-[0-9A-Za-z-]+)\s*$')
# Every host process with an NVIDIA device node open (one `find` over the host's /proc/*/fd), then each holder's comm
# and cgroup. Exit 3 when the host procfs is not where we look; a holder that exits mid-scan prints MISSING.
DEVICE_HOLDERS_COMMAND = (
    rf'H={cfg.HOST_ROOT}/proc; [ -r "$H/1/cgroup" ] || {{ echo "no host procfs at $H" >&2; exit 3; }}; '
    r"""L=$(find "$H"/[0-9]*/fd -maxdepth 1 -lname '/dev/nvidia*' -printf '%h %l\n' 2>/dev/null); printf '%s\n' "$L"; """
    r"""for p in $(printf '%s\n' "$L" | sed -n 's#^.*/proc/\([0-9]*\)/fd .*#\1#p' | sort -un); do """
    r"""printf '== %s %s\n' "$p" "$(cat "$H/$p/comm" 2>/dev/null)"; cat "$H/$p/cgroup" 2>/dev/null || echo MISSING; """
    r'done; exit 0'
)
_HOLDER_FD = re.compile(r'/proc/(\d+)/fd (/dev/\S+)$')
_GPU_DEVICE = re.compile(r'^/dev/nvidia(\d+|ctl|-uvm)$')  # the nodes a CUDA or `--gpus` process holds
PERSISTENCED_COMM = 'nvidia-persiste'  # /proc/<pid>/comm stops at 15 bytes: nvidia-persistenced


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
            ids.update(_CONTAINER_ID.findall(line))
    return out


@dataclass
class DeviceHolder:
    pid: int
    devices: list[str] = field(default_factory=list)
    comm: str = ''
    read: bool = False  # its comm + cgroup block came back
    containers: set[str] | None = field(default_factory=set)  # IDs in its cgroup paths; None: exited mid-scan


def parse_device_holders(stdout: str) -> dict[int, DeviceHolder]:
    """``DEVICE_HOLDERS_COMMAND``'s output: the fd lines (only the GPU nodes we judge), then a block per holder."""
    holders: dict[int, DeviceHolder] = {}
    current: DeviceHolder | None = None
    in_blocks = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith('== '):
            in_blocks = True
            pid_text, _, comm = line[3:].partition(' ')
            current = holders.get(int(pid_text)) if pid_text.isdigit() else None
            if current is not None:
                current.comm, current.read, current.containers = comm.strip(), True, set()
        elif not in_blocks:
            m = _HOLDER_FD.search(line)
            if m and _GPU_DEVICE.match(m.group(2)):
                holder = holders.setdefault(int(m.group(1)), DeviceHolder(int(m.group(1))))
                if m.group(2) not in holder.devices:
                    holder.devices.append(m.group(2))
        elif current is not None:
            if line == 'MISSING':
                current.containers = None
            elif current.containers is not None:
                current.containers.update(_CONTAINER_ID.findall(line))
    return holders


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
        return Answer(False, f'container {record.container_id[:12]} vanished (not stopped by us)'), ()
    evidence: dict[str, Any] = {'status': info.status, 'started_at': info.started_at, 'image_id': info.image_id}
    if not info.up:
        return Answer(False, f'container {info.status} (not stopped by us)', evidence), ()
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
    foreign, exited = [], []
    for holder in sorted(holders.values(), key=lambda h: h.pid):
        devices = ', '.join(holder.devices)
        if not holder.read:
            foreign.append(f'pid {holder.pid} holds {devices}: its cgroup was not read')
        elif holder.containers is None:
            exited.append(holder.pid)  # gone between the fd scan and its cgroup read: it holds nothing now
        elif holder.containers & ours or (not holder.containers and holder.comm == PERSISTENCED_COMM):
            continue
        else:
            where = ', '.join(sorted(i[:12] for i in holder.containers)) or 'no container'
            foreign.append(f'pid {holder.pid} ({holder.comm or "?"}) in {where} holds {devices}')
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
    kind: str  # heartbeat | health | bench | replace | miss
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
        for box_id, records in self.leased().items():
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
            if self._heartbeat_due(records, self.wall()):
                if not self._heartbeat(box_id, box, runner, records, manifests, report):
                    return
            for record in records:
                current = self.instances.instances.get(record.id)
                if current is None or current.draining:
                    continue
                manifest = manifests.get(current.entry)
                if manifest is not None and self._health_due(current, manifest, self.wall()):
                    if not self._health(box_id, box, runner, current, manifest, report):
                        return
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
            ours = {r.container_id for r in self.instances.on_box(box_id) if r.container_id} if scan else None
        try:
            result = run_heartbeat(runner, box, records, manifests, now, ours)
        except (*_TRANSPORT, NoAnswer) as e:
            self._miss(box_id, runner, records, f'{type(e).__name__}: {e}'[:300], now, report)
            return False
        if scan and self._box_busy(box_id):
            result.devices = None  # a start, drain or proof took the box mid-scan: its containers are no verdict
        took = {'heartbeat': round((self.clock() - started) * 1000.0, 1)}
        with self.lock:
            if box_id in self.boxes.boxes:
                reached = mark_reachable(self.boxes.boxes[box_id])
                if reached is not self.boxes.boxes[box_id]:
                    self.boxes.put(reached)
            for record in records:
                current = self.instances.instances.get(record.id)
                if current is None or current.draining:
                    continue
                if record.id in result.recorded:
                    current.docker_started_at, current.image_id = result.recorded[record.id]
                current.last_heartbeat_at, current.heartbeat_ok, current.heartbeat_misses = now, result.ok, 0
                current.heartbeat = result.evidence_for(current)
                observe_pay(current, now)
                self.instances.put(current)
                report.actions.append(
                    WatchAction(
                        'heartbeat', box_id, record.id, record.uuid, result.ok,
                        'ok' if result.ok else '; '.join(result.reasons())[:500], [LEASED], took,
                    )
                )  # fmt: skip
        if result.ok:
            return True
        self._bench(box_id, runner, records, result, report)
        return False

    def _miss(self, box_id, runner, records, why: str, now: float, report: WatchReport) -> None:
        """No answer: a miss on every instance (no pay for the interval) and on the box's unreachable count; the
        ``UNREACHABLE_BENCH_AFTER``-th in a row benches the box for a flat 12 h and undeploys its instances."""
        with self.lock:
            report.unreachable[box_id] = why
            for record in records:
                current = self.instances.instances.get(record.id)
                if current is not None and not current.draining:
                    current.heartbeat_misses += 1
                    current.heartbeat_ok, current.pay_open = None, False  # no answer, no pay
                    current.heartbeat = {'at': now, 'ok': None, 'error': why}
                    self.instances.put(current)
            before = self.boxes.boxes.get(box_id)
            if before is None:
                return
            after = apply_unreachable(before, now)
            self.boxes.put(after)
            benched = after.status == BENCHED and before.status != BENCHED
            in_a_row = f'{after.unreachable_count}/{cfg.UNREACHABLE_BENCH_AFTER} in a row'
            report.actions.append(
                WatchAction('miss', box_id, ok=False, detail=f'no answer, no verdict ({in_a_row}): {why}')
            )
            victims = self._mark_for_kill(box_id, now) if benched else []
        if benched:
            hours = cfg.UNREACHABLE_BENCH_S / 3600
            detail = f'{in_a_row} without an answer: BENCHED for {hours:.0f} h (off the ladder)'
            self._kill(runner, victims, WatchAction('bench', box_id, ok=False, detail=detail, states=[BENCHED]), report)

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
