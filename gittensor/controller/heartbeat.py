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
  per card: a PID we cannot attribute to our container fails.

Any failure benches the box on the fraud ladder, withholds its pay from that instant (``BoxState.withheld_from``, which
WS-F consumes) and undeploys every instance on it with a kill. A visit that gets no answer (SSH down, docker erroring)
is not a verdict: the miss is counted on the instance and nothing is paid for it (the heartbeat is a pay condition),
but nothing is benched either.

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

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.scrape import NVML_MD5_COMMAND, nvidia_smi_command, parse_md5, parse_nvidia_smi
from gittensor.controller.checks.state import (
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
    transition_card,
)
from gittensor.controller.manifest import Drain, Manifest
from gittensor.controller.reconcile import InstanceRecord, InstanceStore
from gittensor.controller.registry import Registry, RegistryError
from gittensor.controller.runspec import (
    BoxHttp,
    HttpClient,
    PlacementError,
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
        elif out.get(pid) is not None:
            out[pid].update(_CONTAINER_ID.findall(line))
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
    alone: dict[str, Answer]  # uuid -> card ours alone?
    recorded: dict[str, tuple[str, str]] = field(default_factory=dict)  # instance -> (StartedAt, image id) filled now

    @property
    def failed(self) -> list[str]:
        names = []
        if not self.same_card.ok:
            names.append(SAME_CARD)
        if any(not a.ok for a in self.containers.values()):
            names.append(OUR_CONTAINER)
        if any(not a.ok for a in self.alone.values()):
            names.append(CARD_OURS_ALONE)
        return names

    @property
    def ok(self) -> bool:
        return not self.failed

    def reasons(self) -> list[str]:
        out = [] if self.same_card.ok else [f'{SAME_CARD}: {self.same_card.detail}']
        out += [f'{OUR_CONTAINER} {i}: {a.detail}' for i, a in self.containers.items() if not a.ok]
        out += [f'{CARD_OURS_ALONE} {u[:12]}…: {a.detail}' for u, a in self.alone.items() if not a.ok]
        return out

    def evidence_for(self, record: InstanceRecord) -> dict:
        container = self.containers.get(record.id, Answer(False, 'not asked'))
        alone = self.alone.get(record.uuid, Answer(False, 'not asked'))
        return {
            'at': self.at,
            'ok': self.ok,
            SAME_CARD: self.same_card.as_dict(),
            OUR_CONTAINER: container.as_dict(),
            CARD_OURS_ALONE: alone.as_dict(),
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
    baseline_power = dict(box.identity.get('power_limits') or {})
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
    evidence = {'status': info.status, 'started_at': info.started_at, 'image_id': info.image_id}
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


def run_heartbeat(
    runner: HostRunner, box: BoxState, records: list[InstanceRecord], manifests: dict[str, Manifest | None], now: float
) -> HeartbeatResult:
    """One heartbeat over one box's leased instances. Raises a transport error or ``NoAnswer`` when it gets none."""
    same_card = _same_card(runner, box)
    containers, recorded = {}, {}
    for record in records:
        answer, filled = _our_container(runner, record, manifests.get(record.entry))
        containers[record.id] = answer
        if filled:
            recorded[record.id] = filled
    return HeartbeatResult(now, same_card, containers, _alone(runner, records), recorded)


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
        return any(
            r.last_heartbeat_at is None or now - r.last_heartbeat_at >= self.heartbeat_interval_s for r in records
        )

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
        due = {}
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
                if self._health_due(current, manifests.get(current.entry), self.wall()):
                    if not self._health(box_id, box, runner, current, manifests[current.entry], report):
                        return
        except Exception as e:  # a bug must not kill the watch loop; the next tick retries
            with self.lock:
                report.unreachable[box_id] = f'{type(e).__name__}: {e}'[:300]
        finally:
            getattr(runner, 'close', lambda: None)()

    def _heartbeat(self, box_id, box, runner, records, manifests, report) -> bool:
        """True when the visit may go on to health probes."""
        started, now = self.clock(), self.wall()
        try:
            result = run_heartbeat(runner, box, records, manifests, now)
        except (*_TRANSPORT, NoAnswer) as e:
            why = f'{type(e).__name__}: {e}'[:300]
            with self.lock:
                report.unreachable[box_id] = why
                for record in records:
                    current = self.instances.instances.get(record.id)
                    if current is not None and not current.draining:
                        current.heartbeat_misses += 1
                        current.heartbeat_ok = None
                        current.heartbeat = {'at': now, 'ok': None, 'error': why}
                        self.instances.put(current)
                report.actions.append(WatchAction('miss', box_id, ok=False, detail=f'no answer, no verdict: {why}'))
            return False
        took = {'heartbeat': round((self.clock() - started) * 1000.0, 1)}
        with self.lock:
            for record in records:
                current = self.instances.instances.get(record.id)
                if current is None or current.draining:
                    continue
                if record.id in result.recorded:
                    current.docker_started_at, current.image_id = result.recorded[record.id]
                current.last_heartbeat_at, current.heartbeat_ok, current.heartbeat_misses = now, result.ok, 0
                current.heartbeat = result.evidence_for(current)
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

    def _bench(self, box_id, runner, records, result: HeartbeatResult, report: WatchReport) -> None:
        """BENCHED + pay withheld at once (one short state write), then every instance on the box undeployed with a
        kill. An undeploy that fails leaves its record draining; the reconciler finishes it."""
        now = self.wall()
        with self.lock:
            self.boxes.put(
                apply_heartbeat_failure(self.boxes.boxes[box_id], result.failed, now, reasons=result.reasons())
            )
            victims = self.instances.on_box(box_id)
            for record in victims:
                record.draining, record.healthy = True, False
                record.drain_type, record.drain_max_s = 'kill', 0
                self.instances.put(record)
        action = WatchAction('bench', box_id, ok=False, detail='; '.join(result.reasons())[:500], states=['BENCHED'])
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
            outcome = probe_health(self.http_for(runner, box), manifest, runner, record.container_id)
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
            record.draining, record.healthy = True, False
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
