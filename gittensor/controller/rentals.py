# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Rentals: a customer's pod on a whole box (vault ``29`` §1, §4). The second lease type beside placement instances,
and for now the only one in use.

A rental is an order (``RentalRecord`` in ``rentals.json``: the GPU type, the box size, the customer's image, SSH
keys, container ports and ``ends_at``) that this reconciler carries through its states:

    requested -> starting -> active -> ending -> ended       failed: no_box_fits | pull_failed | start_failed | box_lost

* **Place** (``requested``): the best rentable box (``standing.box_rentable``: a rent range, not benched, standing
  >= standard) of that type and size with every card IDLE, best standing first, then freshest full check. Its cards go
  STARTING under the rental id and a thread starts the pod: the ``gt-rental`` network and the box firewall (idempotent),
  the image pull, ``docker run --runtime=sysbox-runc`` with every card and the ports published on the box's own
  address, the keys written into the pod, then an SSH banner on the mapped port 22 from here. That banner is
  ``active``: cards LEASED, pay span open, ``started_at``. A failed start undeploys, sends the cards to CHECKING and
  fails the rental (``pull_failed`` / ``start_failed``); a failed start is the ordinary ``start_failed`` standing
  event, a failed pull a neutral ``pull_failed`` one (the customer's image name, not the box's fault).
  An order nothing fits waits ``NO_FIT_GRACE_S`` (a card may be CHECKING between rounds), then fails ``no_box_fits``.
* **Confirm** (every pass, ``starting`` with a container / ``active`` / ``ending``): the pod is inspected. Running
  extends the pay span. Gone or stopped without our stop is ``box_lost``: with the agent answering throughout that is
  the heartbeat failure of ``23`` §4a (bench, pay withheld); after missed visits it is a stop, not a cheat (Kimbo
  9/16). An unreachable box counts a miss; ``LOST_AFTER_MISSES`` of them end the rental the same way.
* **End** (``active`` past ``ends_at``, or ``ending`` ordered by the app / an operator): ``docker stop`` with
  ``STOP_GRACE_S``, remove, cards to CHECKING (the next round re-proves them), ``ended``, the span closed at the stop,
  a ``clean_lease`` standing event with the leased seconds.
* **Pre-pull** (idle rentable boxes): one missing quick-pick image per box per pass, so a rental of the common image
  starts in seconds (Lium measured p50 23 s with the image on the node against 61 s without).

The ledger pays a rental like an instance: a ``RentalRecord`` carries ``box``, ``uuid`` / ``uuids``, ``pay_from`` /
``pay_through`` / ``pay_open`` and ``stopped_at`` exactly as ``InstanceRecord`` does, and the daemon hands the ledger
both stores in one mapping. Cards held by a rental are the rental reconciler's: the placement reconciler leaves them
alone (``Reconciler.held_cards``). Orders arrive from the app through the poller (PR 3) or from ``gitt controller
rentals order``; status leaves the same way. This module never talks to the app.
"""

from __future__ import annotations

import ipaddress
import json
import math
import secrets
import shlex
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gittensor.agent.config import RENTAL_ENDS_AT_LABEL, RENTAL_LABEL, SYSBOX_RUNTIME, UUID_LABEL
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.state import (
    CHECKING,
    CLEAN_LEASE,
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
    record_start,
    transition_card,
)
from gittensor.controller.checks.vendor import AMD, NVIDIA, amd_attach_args
from gittensor.controller.locks import BoxLocks
from gittensor.controller.manifest import gpu_type_of
from gittensor.controller.runspec import PlacementError, PullToken, image_present_command, pull_command
from gittensor.controller.ssh import SshTransportError
from gittensor.controller.ssh.certs import CertificateError
from gittensor.controller.standing import PROBATION, box_rentable, rank, standing

# -- the states (the app's names, 29 §3) -----------------------------------------------------------------------------
REQUESTED = 'requested'
STARTING_R = 'starting'
ACTIVE = 'active'
ENDING = 'ending'
ENDED = 'ended'
FAILED = 'failed'
OPEN = (REQUESTED, STARTING_R, ACTIVE, ENDING)
# failed reasons
NO_BOX_FITS = 'no_box_fits'
PULL_FAILED = 'pull_failed'
START_FAILED = 'start_failed'
BOX_LOST = 'box_lost'

# -- the pod (29 §4) --------------------------------------------------------------------------------------------------
RENTAL_NETWORK = 'gt-rental'  # one bridge per box for pods; ICC off, so a pod sees neither the agent nor another pod
RENTAL_BRIDGE = 'gt-rental'  # the bridge interface's name on the host: what the firewall rules key on
RENTAL_SSH_PORT = 22
POD_SHM_SIZE = '8g'
POD_PIDS_LIMIT = 8192
PULL_TIMEOUT_S = 900.0  # a 20 GB CUDA image on a slow line
SSHD_PROBE_TIMEOUT_S = 180.0  # from `docker run` to an SSH banner on the mapped port
SSHD_PROBE_INTERVAL_S = 5.0
STOP_GRACE_S = 30
NO_FIT_GRACE_S = 120.0
LOST_AFTER_MISSES = 3
# Images the rent page promises run sshd on :22 (gittensor-app rental-images.ts: keep the lists equal). Pre-pulled on
# idle rentable boxes.
QUICK_PICK_IMAGES = ('daturaai/pytorch:2.12.0-py3.12-cuda12.8-devel-ubuntu24.04-dind',)
# The AMD quick-picks (31 step 4: an SSH-ready ROCm PyTorch, a ROCm terminal, vLLM) are not published yet, so an AMD
# box pre-pulls nothing: the CUDA images above would be dead weight on it, and a customer's own image is pulled at
# order time as on any box.
QUICK_PICK_IMAGES_AMD: tuple[str, ...] = ()
# What a runc dev box can run: the same image family without docker-in-docker (its entrypoint starts dockerd first and
# exits without privileges). The production quick-pick stays the dind image, under Sysbox.
QUICK_PICK_NO_DIND = 'daturaai/pytorch:2.6.0-py3.12-cuda12.6.3-devel-ubuntu24.04'
# What a pod must not reach from a miner's box: the miner's LAN and the host itself (29 §4). Link-local covers the
# cloud metadata address; 100.64/10 is carrier NAT.
PRIVATE_NETS = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10')
SMTP_PORT = 25
NEW_CONNECTIONS_PER_S = 200  # outbound SYNs per second per pod before the rest are dropped (a scanner, not a job)

_TRANSPORT = (SshTransportError, CertificateError)


class RentalError(Exception):
    pass


def new_rental_id() -> str:
    return f'rnt_{secrets.token_hex(8)}'


@dataclass
class RentalRecord:
    """One rental, the order and what became of it. The pay fields are ``InstanceRecord``'s, by name, for the ledger."""

    id: str
    state: str = REQUESTED
    # the order (29 §3)
    gpu_type: str = ''
    gpu_count: int = 1
    image: str = ''
    ssh_pubkeys: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=lambda: [RENTAL_SSH_PORT])  # inside the pod, 22 first
    env: dict[str, str] = field(default_factory=dict)
    ends_at: float = 0.0
    want_box_uid: int | None = None  # the order pinned a box (re-rent the same one); None: our pick
    created_at: float = 0.0
    # the placement
    box: str = ''  # hotkey
    box_uid: int | None = None
    uuid: str = ''  # the first card (``InstanceRecord.uuid``); the ledger reads ``uuids`` as well
    uuids: list[str] = field(default_factory=list)
    vendor: str = NVIDIA  # the box's pinned vendor (30 §1 #2); records from before this field load as nvidia
    render_nodes: dict[str, str] = field(default_factory=dict)  # AMD: {uuid: 'renderD<N>'}, the box's pin at placement
    container_id: str = ''
    host: str = ''
    port_map: dict[str, int] = field(default_factory=dict)  # str(pod port) -> the host port docker publishes it on
    # str(pod port) -> the port the world reaches it on: the same, except on a host that remaps published ports (a
    # Lium pod, BoxState.port_map). What the customer is told; what the probe dials.
    public_map: dict[str, int] = field(default_factory=dict)
    started_at: float | None = None  # active: the SSH banner answered
    ended_at: float | None = None
    reason: str = ''
    # the watch
    last_seen_at: float | None = None  # the pod was inspected running
    misses: int = 0  # consecutive passes the box did not answer
    # pay (the ledger's names)
    pay_from: float | None = None
    pay_through: float | None = None
    pay_open: bool = False
    stopped_at: float | None = None
    # what the app has been told (``rental_seam.RentalPoller``): a report goes out whenever this differs from ``state``
    reported_state: str = ''

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RentalRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def name(self) -> str:
        return f'gt-{self.id}'

    @property
    def open(self) -> bool:
        return self.state in OPEN


class RentalStore:
    """``rentals.json``: every rental this controller knows, open and finished (finished ones are kept for the poller
    to report and the operator to read)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.rentals: dict[str, RentalRecord] = {}
        self.reload()

    def reload(self) -> None:
        """Re-read the file: the poller and the CLI write orders beside the daemon."""
        self.rentals = {}
        if self.path.exists():
            doc = json.loads(self.path.read_text() or '{}')
            self.rentals = {k: RentalRecord.from_dict(v) for k, v in (doc.get('rentals') or {}).items()}

    def put(self, record: RentalRecord) -> None:
        self.rentals[record.id] = record
        self.save()

    def remove(self, rental_id: str) -> None:
        self.rentals.pop(rental_id, None)
        self.save()

    def on_box(self, box_id: str) -> list[RentalRecord]:
        return [r for r in self.rentals.values() if r.box == box_id and r.open]

    def held_cards(self, box_id: str) -> set[str]:
        """The cards open rentals hold on a box: the placement reconciler's hands off them."""
        return {u for r in self.on_box(box_id) for u in r.uuids}

    def containers_on(self, box_id: str) -> set[str]:
        return {r.container_id for r in self.on_box(box_id) if r.container_id}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'rentals': {k: asdict(v) for k, v in self.rentals.items()}}, indent=2))
        tmp.replace(self.path)


# -- the pod's docker lines --------------------------------------------------------------------------------------------


def ensure_rental_network_command(network: str = RENTAL_NETWORK, bridge: str = RENTAL_BRIDGE) -> str:
    """The pods' bridge: ICC off (a pod sees neither the agent container nor another pod), egress on (a customer
    installs things), a fixed interface name so the firewall rules can name it."""
    return (
        f'docker network inspect {shlex.quote(network)} >/dev/null 2>&1 || docker network create '
        f'-o com.docker.network.bridge.enable_icc=false -o com.docker.network.bridge.name={shlex.quote(bridge)} '
        f'--label io.gittensor.network=rental {shlex.quote(network)}'
    )


def firewall_rules(bridge: str = RENTAL_BRIDGE) -> list[str]:
    """iptables rules for traffic leaving pods (``-i <bridge>``), in the DOCKER-USER chain Docker consults before its
    own: drop anything for a private network (the miner's LAN, the host, the metadata address), drop outbound SMTP,
    and drop new connections past ``NEW_CONNECTIONS_PER_S`` per source (a scanner). Each rule is the ``-A`` form;
    ``firewall_commands`` makes them idempotent."""
    rules = [f'DOCKER-USER -i {bridge} -d {net} -j DROP' for net in PRIVATE_NETS]
    rules.append(f'DOCKER-USER -i {bridge} -p tcp --dport {SMTP_PORT} -j DROP')
    rules.append(
        f'DOCKER-USER -i {bridge} -p tcp --syn -m hashlimit --hashlimit-above {NEW_CONNECTIONS_PER_S}/sec '
        '--hashlimit-mode srcip --hashlimit-name gt-rental -j DROP'
    )
    return rules


def firewall_commands(bridge: str = RENTAL_BRIDGE, host_root: str = cfg.HOST_ROOT) -> str:
    """One shell line that installs every rule once, on the HOST: the agent container has no iptables and its own
    netns, so each rule runs through PID 1's mount and network namespaces (``nsenter``; the agent is ``--pid host``
    and privileged, 26 §4). ``-C`` asks first, so a re-run adds nothing. ``-I`` puts ours ahead of Docker's."""
    parts = []
    for rule in firewall_rules(bridge):
        parts.append(
            f'nsenter -t 1 -m -n -- iptables -w -C {rule} 2>/dev/null || nsenter -t 1 -m -n -- iptables -w -I {rule}'
        )
    return ' && '.join(parts)


def assign_ports(ports: Iterable[int], rent_range: list[int], taken: Iterable[int]) -> dict[str, int]:
    """Public ports for a pod's container ports, lowest free in the box's rent range first; 22 always first."""
    if len(rent_range) != 2:
        raise RentalError('the box has no rent range')
    low, high = int(rent_range[0]), int(rent_range[1])
    busy = set(taken)
    free = (p for p in range(low, high + 1) if p not in busy)
    wanted = [RENTAL_SSH_PORT, *(p for p in dict.fromkeys(ports) if p != RENTAL_SSH_PORT)]
    out: dict[str, int] = {}
    for inside in wanted:
        public = next(free, None)
        if public is None:
            raise RentalError(f'rent range {low}-{high} has no free port left')
        out[str(inside)] = public
    return out


def pod_run_command(r: RentalRecord, network: str = RENTAL_NETWORK, runtime: str = SYSBOX_RUNTIME) -> str:
    """The pod: Sysbox (root, docker and systemd inside, no host root), every card of the box, the ports published on
    every address of the box (the miner's firewall opened the range), caps, labels, never a restart by itself, no
    host mounts, no extra capabilities. ``runtime`` is ``runc`` only on a dev box that cannot run Sysbox (a Lium pod
    is one: it is a Sysbox container itself)."""
    devices = ','.join(r.uuids)
    if r.vendor == AMD:
        # every card of the box by its pinned render node (30 §1 #5): the nodes the scrape saw, nothing else
        missing = [u for u in r.uuids if not r.render_nodes.get(u)]
        if missing:
            raise RentalError(f'{START_FAILED}: no render node pinned for {len(missing)} of {len(r.uuids)} card(s)')
        attach = amd_attach_args([r.render_nodes[u] for u in r.uuids])
        if runtime == SYSBOX_RUNTIME:
            # sysbox-fs emulates /sys/devices/virtual and serves the KFD topology ROCr reads as empty files; the
            # host's is mounted read-only at a side path and bound over the emulated one right after start
            # (``kfd_topology_bind_command``), the one host mount a pod ever gets (vault 30 §10, measured 10/9)
            attach.append(f'-v {KFD_TOPOLOGY_HOST}:{KFD_TOPOLOGY_SIDE}:ro')
    else:
        # every card of the box by --gpus (docker reads the value as CSV: quoted, commas survive)
        attach = [f'--gpus {shlex.quote(f'"device={devices}"')}']
    parts = [
        'docker run -d',
        f'--name {shlex.quote(r.name)}',
        f'--runtime={runtime}',
        f'--label {shlex.quote(f"{RENTAL_LABEL}={r.id}")}',
        f'--label {shlex.quote(f"{RENTAL_ENDS_AT_LABEL}={int(r.ends_at)}")}',  # `gitt down` tells the miner how long
        f'--label {shlex.quote(f"{UUID_LABEL}={devices}")}',
        *attach,
        f'--shm-size {POD_SHM_SIZE}',
        f'--pids-limit {POD_PIDS_LIMIT}',
        '--restart no',
        f'--network {shlex.quote(network)}',
    ]
    for inside, public in sorted(r.port_map.items(), key=lambda kv: int(kv[0])):
        parts.append(f'-p {shlex.quote(f"{public}:{inside}")}')
    # The convention Lium's and RunPod's images start sshd by: their start.sh runs sshd only when PUBLIC_KEY is set
    # and appends it to authorized_keys. Set it (every key, newline-separated) so a quick-pick image comes up with
    # sshd; the docker exec after start writes the same keys for an image that runs sshd regardless.
    if r.ssh_pubkeys:
        parts.append(f'-e {shlex.quote("PUBLIC_KEY=" + chr(10).join(k.strip() for k in r.ssh_pubkeys if k.strip()))}')
    parts += [f'-e {shlex.quote(f"{k}={v}")}' for k, v in sorted(r.env.items()) if k != 'PUBLIC_KEY']
    parts.append(shlex.quote(r.image))
    return ' '.join(parts)


KFD_TOPOLOGY_HOST = '/sys/devices/virtual/kfd'
KFD_TOPOLOGY_SIDE = '/host-kfd'


def kfd_topology_bind_command(container_id: str) -> str:
    """Inside a Sysbox pod on an AMD box, bind the host's KFD topology (mounted at the side path by
    ``pod_run_command``) over the empty one sysbox-fs emulates, as root, before the customer's first ROCr call."""
    return f'docker exec -u 0 {shlex.quote(container_id)} sh -c ' + shlex.quote(
        f'mount --bind {KFD_TOPOLOGY_SIDE}/kfd/topology {KFD_TOPOLOGY_HOST}/kfd/topology'
    )


def authorized_keys_command(container_id: str, keys: Iterable[str]) -> str:
    """Write the customer's keys into the running pod (Lium does the same by ``docker exec``): the image's sshd
    reads /root/.ssh/authorized_keys. Keys arrive on stdin, one per line; nothing of them is on a command line."""
    return f'docker exec -i {shlex.quote(container_id)} sh -c ' + shlex.quote(
        'mkdir -p /root/.ssh && chmod 700 /root/.ssh && cat > /root/.ssh/authorized_keys '
        '&& chmod 600 /root/.ssh/authorized_keys'
    )


def keys_stdin(keys: Iterable[str]) -> bytes:
    return ''.join(k.strip() + '\n' for k in keys if k.strip()).encode()


def pod_remove_command(container_id: str, grace_s: int = STOP_GRACE_S) -> str:
    q = shlex.quote(container_id)
    return f'docker stop --time {int(grace_s)} {q} >/dev/null 2>&1; docker rm -f {q}'


def pod_running_command(container_id: str) -> str:
    return f"docker inspect --format '{{{{.State.Running}}}}' {shlex.quote(container_id)}"


def probe_ssh(host: str, port: int, timeout: float = 5.0) -> bool:
    """A TCP connect and an SSH banner: the pod's sshd answers on the box's public port. No login is attempted."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).startswith(b'SSH-')
    except (OSError, ValueError):
        return False


def _public_host(box: BoxState) -> str:
    host = box.host
    try:
        if not ipaddress.ip_address(host).is_global:
            return host  # a dev box: the probe goes where we reach it anyway
    except ValueError:
        pass
    return host


# -- the reconciler ------------------------------------------------------------------------------------------------------


@dataclass
class RentalAction:
    kind: str  # place | active | failed | ending | ended | lost | prepull | miss
    rental: str
    box: str
    detail: str = ''


@dataclass
class RentalReport:
    actions: list[RentalAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)  # rentals a thread is starting or stopping this pass
    open: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


class RentalReconciler:
    """One pass: confirm what runs, end what is due, place what waits, pre-pull where idle. Starts and stops run in a
    thread per box under the box lock (like placement) so a slow pull holds up nothing else; ``background=False``
    runs them inline (tests, ``gitt controller rentals reconcile``)."""

    def __init__(
        self,
        boxes: StateStore,
        rentals: RentalStore,
        make_runner: Callable[[BoxState], HostRunner],
        *,
        box_locks: BoxLocks | None = None,
        lock: threading.RLock | None = None,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        probe: Callable[[str, int], bool] = probe_ssh,
        pull_token: PullToken | None = None,
        background: bool = True,
        prepull: tuple[str, ...] = QUICK_PICK_IMAGES,
        no_fit_grace_s: float = NO_FIT_GRACE_S,
        runtime: str = SYSBOX_RUNTIME,
        firewall: bool = True,
        min_standing: str = PROBATION,
        alive_interval_s: float | None = None,
    ):
        """``runtime`` / ``firewall`` are the dev overrides (`gitt controller run --rental-runtime runc
        --no-rental-firewall`): our own test boxes, never a miner's. ``min_standing`` is the rental gate: probation
        (any admitted box) since issue #1818, the 48 h pay holdback being what a new box has at stake."""
        self.boxes, self.rentals = boxes, rentals
        self.make_runner = make_runner
        self.box_locks = box_locks or BoxLocks()
        self._lock = lock or threading.RLock()
        self.wall, self.sleep, self.probe = wall, sleep, probe
        self.pull_token = pull_token
        self.background = background
        self.no_fit_grace_s = no_fit_grace_s
        self.runtime, self.firewall, self.min_standing = runtime, firewall, min_standing
        # under runc the dind quick-pick cannot start (no privileges for its dockerd); pre-pull what can
        self.prepull_images = (
            (QUICK_PICK_NO_DIND,) if runtime != SYSBOX_RUNTIME and prepull == QUICK_PICK_IMAGES else prepull
        )
        self._threads: dict[str, threading.Thread] = {}  # rental id -> its start / stop thread
        self._prepulling: dict[str, threading.Thread] = {}  # box id -> its pull thread
        # What a start / stop thread decided after its pass had been reported: handed to the next pass's report, so
        # `active`, `failed` (with docker's words) and `ended` reach the operator's log and the status file.
        self._late: list[RentalAction] = []
        # A running pod is confirmed every pass and said nothing about; with an interval, one `alive` line per rental
        # per interval (the daemon passes the heartbeat interval) so a grep of the id shows the lease was watched.
        self.alive_interval_s = alive_interval_s
        self._alive_said: dict[str, float] = {}

    # -- state writes (every change saved before the next step) ----------------------------------------------------

    def _box(self, box_id: str) -> BoxState:
        return self.boxes.boxes[box_id]

    def _put_box(self, box: BoxState) -> None:
        with self._lock:
            self.boxes.put(box)

    def _put(self, r: RentalRecord) -> None:
        with self._lock:
            self.rentals.put(r)

    def _move_cards(self, r: RentalRecord, to: str) -> None:
        """Every card of the rental to ``to`` (LEASED, or CHECKING by way of DRAINING). A box benched meanwhile has no
        cards to move; a card another loop moved first is left where it is."""
        with self._lock:
            if r.box not in self.boxes.boxes:
                return
            box = self._box(r.box)
            for uuid in r.uuids:
                if box.status != IDLE or uuid not in box.cards or box.cards[uuid].instance_id not in (r.id, ''):
                    continue
                steps = [to]
                if to == CHECKING and box.cards[uuid].state == LEASED:
                    steps = [DRAINING, CHECKING]
                for step in steps:
                    if box.cards[uuid].state == step:
                        continue
                    try:
                        box = transition_card(box, uuid, step, self.wall(), r.id if step == STARTING else None)
                    except CardTransitionError:
                        break
            self.boxes.put(box)

    def _finish(self, r: RentalRecord, state: str, reason: str = '', ended_at: float | None = None) -> None:
        now = self.wall()
        r.state = state
        r.reason = reason or r.reason
        r.ended_at = ended_at if ended_at is not None else now
        r.pay_open = False
        if r.pay_from is not None:
            r.stopped_at = r.ended_at
            r.pay_through = min(r.pay_through if r.pay_through is not None else r.ended_at, r.ended_at)
        self._put(r)
        self._move_cards(r, CHECKING)

    # -- the pass ----------------------------------------------------------------------------------------------------

    def run_pass(self) -> RentalReport:
        report = RentalReport()
        now = self.wall()
        with self._lock:
            busy = {rid for rid, t in self._threads.items() if t.is_alive()}
            report.actions.extend(self._late)
            self._late = []
        runners: dict[str, HostRunner] = {}
        try:
            self._confirm(report, busy, runners, now)
            self._end_due(report, busy, now)
            self._place(report, busy, now)
            self._prepull(report, busy, runners)
        finally:
            for runner in runners.values():
                getattr(runner, 'close', lambda: None)()
        report.open = sum(1 for r in self.rentals.rentals.values() if r.open)
        return report

    def _runner(self, box: BoxState, runners: dict[str, HostRunner]) -> HostRunner:
        if box.box_id not in runners:
            runners[box.box_id] = self.make_runner(box)
        return runners[box.box_id]

    def _confirm(self, report: RentalReport, busy: set[str], runners: dict[str, HostRunner], now: float) -> None:
        """Inspect every pod we believe is up. Running: the pay span moves on. Not running: lost (see module doc)."""
        for r in sorted(self.rentals.rentals.values(), key=lambda x: x.created_at):
            if r.id in busy or r.state not in (STARTING_R, ACTIVE, ENDING) or not r.container_id:
                continue
            if r.box not in self.boxes.boxes:
                self._finish(r, FAILED, BOX_LOST)
                report.actions.append(RentalAction('lost', r.id, r.box, 'box removed'))
                continue
            box = self._box(r.box)
            if (
                r.state == STARTING_R
            ):  # its start thread is gone (a controller restart mid-start): nothing will finish it
                try:
                    self._runner(box, runners).run(
                        pod_remove_command(r.container_id, 0), timeout=cfg.SSH_COMMAND_TIMEOUT_S
                    )
                except (*_TRANSPORT, PlacementError):
                    pass
                self._finish(r, FAILED, START_FAILED)
                report.actions.append(RentalAction('failed', r.id, r.box, 'start interrupted'))
                continue
            try:
                result = self._runner(box, runners).run(
                    pod_running_command(r.container_id), timeout=cfg.SSH_COMMAND_TIMEOUT_S
                )
            except (*_TRANSPORT, PlacementError) as e:
                r.misses += 1
                r.pay_open = False  # not paid for a span we could not see; it reopens when the pod answers
                self._put(r)
                report.actions.append(RentalAction('miss', r.id, r.box, f'{r.misses}: {type(e).__name__}: {e}'[:200]))
                if r.misses >= LOST_AFTER_MISSES:
                    self._lost(r, box, answered=False, report=report)
                continue
            running = result.ok and result.stdout.strip() == 'true'
            if running:
                if r.state == ACTIVE:
                    if not r.pay_open:
                        r.pay_from = r.pay_from if r.pay_from is not None else now
                        r.pay_open = True
                    r.pay_through = now
                r.misses, r.last_seen_at = 0, now
                self._put(r)
                if self.alive_interval_s is not None and now - self._alive_said.get(r.id, -math.inf) >= self.alive_interval_s:  # fmt: skip
                    self._alive_said[r.id] = now
                    report.actions.append(RentalAction('alive', r.id, r.box, f'pod {r.container_id[:12]} running on {r.host}; pay open'))  # fmt: skip
                continue
            self._lost(r, box, answered=True, report=report)

    def _lost(self, r: RentalRecord, box: BoxState, answered: bool, report: RentalReport) -> None:
        """The pod is gone (or the box is, after the misses). ``answered``: the agent was reachable, so a container we
        did not stop is gone under us: the heartbeat failure of 23 §4a. Otherwise a stop, not a cheat (Kimbo 9/16)."""
        now = self.wall()
        last_good = r.last_seen_at or r.started_at or now
        detail = 'pod gone with the agent answering' if answered else f'box unreachable {r.misses} passes'
        if r.state == ENDING:  # we were about to stop it anyway
            self._finish(r, ENDED, ended_at=last_good)
            report.actions.append(RentalAction('ended', r.id, r.box, f'pod already gone ({detail})'))
            return
        with self._lock:
            if answered and r.state == ACTIVE:
                self._put_box(
                    apply_heartbeat_failure(self._box(r.box), [OUR_CONTAINER], now, rental=r.id, reason=detail)
                )
            elif r.state == ACTIVE:
                b = self._box(r.box)
                for uuid in r.uuids:
                    b = apply_instance_stopped(b, uuid, now, rental=r.id, missed=r.misses, lease_ended_at=last_good)
                self._put_box(b)
        self._finish(r, FAILED, BOX_LOST, ended_at=last_good)
        report.actions.append(RentalAction('lost', r.id, r.box, detail))

    def _end_due(self, report: RentalReport, busy: set[str], now: float) -> None:
        for r in sorted(self.rentals.rentals.values(), key=lambda x: x.created_at):
            if r.id in busy:
                continue
            if r.state == ACTIVE and r.ends_at and now >= r.ends_at:
                r.state, r.reason = ENDING, r.reason or 'ends_at'
                self._put(r)
                report.actions.append(RentalAction('ending', r.id, r.box, 'ends_at passed'))
            if r.state == ENDING:
                if not r.box or r.box not in self.boxes.boxes or not r.container_id:
                    self._finish(r, ENDED if r.started_at else FAILED, r.reason)
                    report.actions.append(RentalAction('ended', r.id, r.box, 'nothing to stop'))
                    continue
                self._run(r.id, lambda r=r: self._stop(r, report))
                report.launched.append(r.id)
            elif r.state == REQUESTED and r.reason in ('customer_stop',):  # an order withdrawn before placement
                self._finish(r, ENDED)
                report.actions.append(RentalAction('ended', r.id, '', 'withdrawn before placement'))

    def _place(self, report: RentalReport, busy: set[str], now: float) -> None:
        taken_boxes = {r.box for r in self.rentals.rentals.values() if r.open and r.box}
        for r in sorted(self.rentals.rentals.values(), key=lambda x: x.created_at):
            if r.state != REQUESTED or r.id in busy:
                continue
            box = self._pick(r, taken_boxes, now)
            if box is None:
                if now - r.created_at >= self.no_fit_grace_s:
                    self._finish(r, FAILED, NO_BOX_FITS)
                    report.actions.append(RentalAction('failed', r.id, '', NO_BOX_FITS))
                continue
            taken_boxes.add(box.box_id)
            try:
                in_use = {p for other in self.rentals.on_box(box.box_id) for p in other.port_map.values()}
                r.port_map = assign_ports(r.ports, box.rent_ports, in_use)
            except RentalError as e:
                self._finish(r, FAILED, START_FAILED)
                report.actions.append(RentalAction('failed', r.id, box.box_id, str(e)))
                continue
            r.public_map = {inside: box.public_port(host_port) for inside, host_port in r.port_map.items()}
            r.box, r.box_uid, r.state = box.box_id, box.uid, STARTING_R
            r.uuids = sorted(box.cards)
            r.uuid = r.uuids[0]
            r.vendor = box.vendor
            r.render_nodes = {u: box.render_nodes[u] for u in r.uuids if u in box.render_nodes}
            r.host = _public_host(box)
            with self._lock:
                b = self._box(box.box_id)
                for uuid in r.uuids:
                    b = transition_card(b, uuid, STARTING, now, r.id)
                self.boxes.put(b)
                self.rentals.put(r)
            report.actions.append(RentalAction('place', r.id, box.box_id, f'{r.gpu_type} x{r.gpu_count}'))
            self._run(r.id, lambda r=r: self._start(r, report))
            report.launched.append(r.id)

    def _pick(self, r: RentalRecord, taken: set[str], now: float) -> BoxState | None:
        """The best rentable box of the type and size, wholly idle, not already carrying an open rental."""
        fits: list[BoxState] = []
        for box in self.boxes.boxes.values():
            if box.box_id in taken or box.status != IDLE or not box.cards:
                continue
            if not box_rentable(box, now, min_level=self.min_standing):
                continue
            if r.want_box_uid is not None and box.uid != r.want_box_uid:
                continue
            if gpu_type_of(box.card_name) != r.gpu_type or len(box.cards) != r.gpu_count:
                continue
            if any(c.state != IDLE or c.instance_id for c in box.cards.values()):
                continue
            if box.vendor == AMD and any(u not in box.render_nodes for u in box.cards):
                continue  # no render node pinned for a card: the pod could not be given it (30 §1 #5)
            fits.append(box)
        if not fits:
            return None
        return max(fits, key=lambda b: (rank(standing(b.standing_events, now)), b.last_check_at or 0.0))

    def _prepull(self, report: RentalReport, busy: set[str], runners: dict[str, HostRunner]) -> None:
        """One missing quick-pick image per wholly idle rentable box, in the background, one pull per box at a time."""
        if not self.prepull_images:
            return
        now = self.wall()
        for box in list(self.boxes.boxes.values()):
            if box.status != IDLE or not box_rentable(box, now, min_level=self.min_standing) or not box.cards:
                continue
            if any(c.state != IDLE for c in box.cards.values()) or self.rentals.on_box(box.box_id):
                continue
            if box.vendor == AMD and not QUICK_PICK_IMAGES_AMD:
                continue
            t = self._prepulling.get(box.box_id)
            if t is not None and t.is_alive():
                continue
            self._prepulling[box.box_id] = self._thread(
                f'prepull-{box.box_id[:8]}', lambda b=box: self._pull_missing(b)
            )
            report.actions.append(RentalAction('prepull', '', box.box_id, 'checking quick-pick images'))

    def _pull_missing(self, box: BoxState) -> None:
        if not self.box_locks.acquire(box.box_id, timeout=0.0):
            return
        try:
            runner = self.make_runner(box)
            try:
                for image in self.prepull_images:
                    if runner.run(image_present_command(image), timeout=cfg.SSH_COMMAND_TIMEOUT_S).ok:
                        continue
                    result = runner.run(pull_command(image), timeout=PULL_TIMEOUT_S)
                    if not result.ok:  # said once per pass in the log: a box whose docker cannot pull is worth knowing
                        detail = f'{image}: exit {result.exit_code}: {(result.stderr or result.stdout).strip()[-200:]}'
                        self._act(RentalReport(), RentalAction('prepull_failed', '', box.box_id, detail))
                    return  # one per pass
            finally:
                getattr(runner, 'close', lambda: None)()
        except Exception as e:  # a pre-pull that fails costs nothing; the rental's own pull is the one that counts
            self._act(RentalReport(), RentalAction('prepull_failed', '', box.box_id, f'{type(e).__name__}: {e}'[:200]))
        finally:
            self.box_locks.release(box.box_id)

    # -- the threads -----------------------------------------------------------------------------------------------------

    def _run(self, rental_id: str, fn: Callable[[], None]) -> None:
        if self.background:
            self._threads[rental_id] = self._thread(f'rental-{rental_id}', fn)
        else:
            fn()

    def _act(self, report: RentalReport, action: RentalAction) -> None:
        """Record an action from a start / stop: in this pass's report, and (in the background) for the next pass,
        since the one that launched the thread has already been reported."""
        report.actions.append(action)
        if self.background:
            with self._lock:
                self._late.append(action)

    def _thread(self, name: str, fn: Callable[[], None]) -> threading.Thread:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        return t

    def join(self, timeout: float | None = None) -> bool:
        for t in [*self._threads.values(), *self._prepulling.values()]:
            t.join(timeout)
        return not any(t.is_alive() for t in [*self._threads.values(), *self._prepulling.values()])

    def _start(self, r: RentalRecord, report: RentalReport) -> None:
        box = self._box(r.box)
        self.box_locks.acquire(box.box_id)
        runner: HostRunner | None = None
        try:
            runner = self.make_runner(box)
            t = cfg.SSH_COMMAND_TIMEOUT_S
            self._check(runner.run(ensure_rental_network_command(), timeout=t), 'network')
            if self.firewall:
                self._check(runner.run(firewall_commands(), timeout=t), 'firewall')
            if not runner.run(image_present_command(r.image), timeout=t).ok:
                pull = runner.run(
                    pull_command(r.image, self.pull_token.username if self.pull_token else None),
                    timeout=PULL_TIMEOUT_S,
                    stdin=self.pull_token.token.encode() if self.pull_token else None,
                )
                if not pull.ok:
                    raise RentalError(f'{PULL_FAILED}: {(pull.stderr or pull.stdout).strip()[-200:]}')
            run = runner.run(pod_run_command(r, runtime=self.runtime), timeout=t)
            cid = self._check(run, 'docker run').strip().splitlines()[-1].strip() if run.ok else ''
            if len(cid) != 64:
                raise RentalError(f'{START_FAILED}: docker run gave no container id')
            r.container_id = cid
            self._put(r)
            if r.vendor == AMD and self.runtime == SYSBOX_RUNTIME:
                self._check(runner.run(kfd_topology_bind_command(cid), timeout=t), 'kfd topology bind')
            self._check(
                runner.run(authorized_keys_command(cid, r.ssh_pubkeys), timeout=t, stdin=keys_stdin(r.ssh_pubkeys)),
                'authorized_keys',
            )
            public = r.public_map.get(str(RENTAL_SSH_PORT)) or box.public_port(r.port_map[str(RENTAL_SSH_PORT)])
            deadline = self.wall() + SSHD_PROBE_TIMEOUT_S
            while not self.probe(r.host, public):
                if self.wall() >= deadline:
                    raise RentalError(
                        f'{START_FAILED}: no SSH banner on {r.host}:{public} after {SSHD_PROBE_TIMEOUT_S:.0f}s'
                    )
                if not (runner.run(pod_running_command(cid), timeout=t).stdout.strip() == 'true'):
                    raise RentalError(f'{START_FAILED}: the pod exited before its sshd answered')
                self.sleep(SSHD_PROBE_INTERVAL_S)
            now = self.wall()
            r.state, r.started_at, r.last_seen_at = ACTIVE, now, now
            r.pay_from, r.pay_through, r.pay_open = now, now, True
            self._put(r)
            self._move_cards(r, LEASED)
            self._put_box(record_start(self._box(r.box), True, now, rental=r.id))
            self._act(report, RentalAction('active', r.id, r.box, f'ssh {r.host}:{public}'))
        except Exception as e:  # any failure: undeploy what may run, fail the rental, free the cards
            reason = PULL_FAILED if str(e).startswith(PULL_FAILED) else START_FAILED
            if runner is not None and r.container_id:
                try:
                    runner.run(pod_remove_command(r.container_id, 0), timeout=cfg.SSH_COMMAND_TIMEOUT_S)
                except Exception:
                    pass
            self._finish(r, FAILED, reason)
            with self._lock:
                if r.box in self.boxes.boxes:
                    b, detail = self._box(r.box), str(e)[:200]
                    if reason == PULL_FAILED:  # the customer's image, not the box: recorded, neutral for standing
                        b = add_event(b, PULL_FAILED, self.wall(), rental=r.id, reason=detail)
                    else:
                        b = record_start(b, False, self.wall(), rental=r.id, reason=detail)
                    self._put_box(b)
            self._act(report, RentalAction('failed', r.id, r.box, f'{reason}: {e}'[:300]))
        finally:
            if runner is not None:
                getattr(runner, 'close', lambda: None)()
            self.box_locks.release(box.box_id)

    def _stop(self, r: RentalRecord, report: RentalReport) -> None:
        box = self._box(r.box)
        self.box_locks.acquire(box.box_id)
        runner: HostRunner | None = None
        try:
            runner = self.make_runner(box)
            runner.run(pod_remove_command(r.container_id), timeout=STOP_GRACE_S + cfg.SSH_COMMAND_TIMEOUT_S)
            now = self.wall()
            self._finish(r, ENDED, ended_at=now)
            if r.started_at is not None:
                with self._lock:
                    if r.box in self.boxes.boxes and self._box(r.box).status == IDLE:
                        leased_s = round(max(0.0, now - r.started_at), 1)
                        self._put_box(add_event(self._box(r.box), CLEAN_LEASE, now, rental=r.id, leased_s=leased_s))
            self._act(report, RentalAction('ended', r.id, r.box, r.reason))
        except Exception as e:
            report.errors.append(f'{r.id}: stop: {type(e).__name__}: {e}'[:300])
            self._act(report, RentalAction('failed', r.id, r.box, f'stop: {type(e).__name__}: {e}'[:300]))
        finally:
            if runner is not None:
                getattr(runner, 'close', lambda: None)()
            self.box_locks.release(box.box_id)

    @staticmethod
    def _check(result: Any, what: str) -> str:
        if not result.ok:
            raise RentalError(
                f'{START_FAILED}: {what}: exit {result.exit_code}: {(result.stderr or result.stdout).strip()[-200:]}'
            )
        return result.stdout


# -- orders (the poller and the CLI call these) -----------------------------------------------------------------------


def place_order(
    store: RentalStore,
    *,
    gpu_type: str,
    gpu_count: int,
    image: str,
    ssh_pubkeys: list[str],
    hours: float,
    ports: Iterable[int] = (RENTAL_SSH_PORT,),
    env: Mapping[str, str] | None = None,
    rental_id: str | None = None,
    box_uid: int | None = None,
    now: float | None = None,
) -> RentalRecord:
    """A new order into the store. ``rental_id`` is the app's id when the poller brings one; the CLI mints one."""
    now = time.time() if now is None else now
    if gpu_count < 1 or hours <= 0 or not image or not ssh_pubkeys:
        raise RentalError('an order needs a GPU count, hours, an image and at least one SSH key')
    r = RentalRecord(
        id=rental_id or new_rental_id(),
        gpu_type=gpu_type,
        gpu_count=int(gpu_count),
        image=image,
        ssh_pubkeys=[k.strip() for k in ssh_pubkeys if k.strip()],
        ports=[RENTAL_SSH_PORT, *(int(p) for p in ports if int(p) != RENTAL_SSH_PORT)],
        env=dict(env or {}),
        ends_at=now + hours * 3600.0,
        want_box_uid=box_uid,
        created_at=now,
    )
    store.put(r)
    return r


def order_end(store: RentalStore, rental_id: str, reason: str = 'customer_stop') -> RentalRecord:
    r = store.rentals.get(rental_id)
    if r is None:
        raise RentalError(f'no rental {rental_id}')
    if r.state in (ACTIVE, STARTING_R):
        r.state, r.reason = ENDING, reason
    elif r.state == REQUESTED:
        r.reason = reason  # the pass ends it without a placement
    store.put(r)
    return r
