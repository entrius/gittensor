# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The tunnel keeper (``gitt controller tunnels``): workload traffic routed through each box's SSH channel.

One OpenSSH master connection per box that carries an instance, logged in with a freshly minted certificate on every
(re)connect (a certificate only matters at authentication, so a long-lived connection outlives it). For every
instance on the box the master carries one local forward, ``<listen host>:<local port> -> <bridge gateway>:<host
port>``: the box's docker bridge gateway is where the workload answers, the same address the controller's probes use.
Forwards are added and cancelled on the live master (``ssh -O forward`` / ``-O cancel``), never by reconnecting, so
one card cycling leaves the streams of the box's other cards alone.

It is its own process, beside the controller rather than inside it: the controller restarts on every deploy, and the
connections that carry traffic stay up through that. It reads ``boxes.json`` and ``instances.json`` and writes
``tunnels.json`` (schema 1, tmp + rename) every pass and on every change; that file is what the gateway routes by:

    {"schema": 1, "written_at": ..., "listen_host": "<listen host>",
     "tunnels": {"<instance id>": {"box": "<hotkey>", "host": "<listen host>", "port": 21003,
                                   "up": true, "since": ..., "error": ""}}}

``up``: the box's master is alive, the forward is registered and one request through the local port got an HTTP
status line back (any status). ``since``: when ``up`` last changed. An instance keeps its local port for its whole
life, across keeper restarts (the ports are read back from ``tunnels.json``); the port of an instance that is gone is
released. Each box connects, forwards and probes on its own worker, so a box that is slow or unreachable holds up no
other; a failed connect is retried with backoff (1 s doubling, capped at 30 s).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from gittensor.controller.checks.runner import CommandResult
from gittensor.controller.checks.state import BoxState, StateStore
from gittensor.controller.reconcile import InstanceStore
from gittensor.controller.runspec import PlacementError, bridge_gateway
from gittensor.controller.ssh import CertificateAuthority, SshRunner, SshTransportError
from gittensor.controller.ssh.certs import CertificateError
from gittensor.controller.ssh.runner import CONNECT_TIMEOUT_S, _text

SCHEMA = 1
TUNNELS_FILE = 'tunnels.json'
LOCK_FILE = 'tunnels.lock'
DEFAULT_LISTEN_HOST = '127.0.0.1'
DEFAULT_PORT_RANGE = (21000, 21999)
DEFAULT_INTERVAL_S = 3.0
BACKOFF_CAP_S = 30.0
SERVER_ALIVE_COUNT_MAX = 3  # with the runner's ServerAliveInterval=15: a silent peer is dropped after ~45 s
MASTER_WAIT_S = CONNECT_TIMEOUT_S + 5  # login + the master's control socket appearing
CONTROL_TIMEOUT_S = 10.0  # one `ssh -O` request to a local master
PROBE_TIMEOUT_S = 5.0
PROBE_REQUEST = b'GET / HTTP/1.0\r\nHost: tunnel\r\nConnection: close\r\n\r\n'
_STATUS_LINE = re.compile(rb'^HTTP/\d(\.\d)? \d{3}')
_KEY_ID_UNSAFE = re.compile(r'[^A-Za-z0-9_.:@=-]')

Emit = Callable[..., None]
Probe = Callable[[str, int], tuple[bool, str]]


class KeeperRunning(Exception):
    def __init__(self, root: Path):
        super().__init__(f'a tunnel keeper already runs on {root} (see `gitt controller tunnels --status`)')


@contextmanager
def keeper_lock(root: Path) -> Iterator[None]:
    """One keeper per state directory: two would hand out the same local ports."""
    with open(Path(root) / LOCK_FILE, 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise KeeperRunning(Path(root)) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parse_port_range(value: str) -> tuple[int, int]:
    low, sep, high = value.partition('-')
    high = high if sep else low
    if not low.isdigit() or not high.isdigit() or not 1 <= int(low) <= int(high) <= 65535:
        raise ValueError(f'--port-range {value!r}: expected LOW-HIGH (1-65535, LOW <= HIGH)')
    return int(low), int(high)


def key_id_for(box_id: str) -> str:
    return _KEY_ID_UNSAFE.sub('_', f'tun-{box_id}')[:128]


def _addr(host: str) -> str:
    return f'[{host}]' if ':' in host else host


def forward_spec(listen_host: str, local_port: int, bridge_gw: str, host_port: int) -> str:
    return f'{_addr(listen_host)}:{int(local_port)}:{_addr(bridge_gw)}:{int(host_port)}'


# ---------------------------------------------------------------- the SSH layer --------------------------------------


class TunnelRunner(SshRunner):
    """``SshRunner`` for one keeper connection: its certificate, pinned host key and options, over a control socket
    at a fixed path per box. ``master_argv`` is the connection itself (a foreground ``-N`` master the keeper
    supervises); ``run`` (the bridge gateway lookup) and ``control`` (``-O check|forward|cancel|exit``) attach to it
    and never open a login of their own."""

    def __init__(self, *args, control: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.socket = Path(control)
        self._as_master = False

    def control_path(self) -> Path:
        return self.socket

    def _multiplex_options(self) -> list[str]:
        if not self._as_master:
            return ['-o', 'ControlMaster=no', '-o', f'ControlPath={self.socket}']
        return [
            '-N',
            '-o',
            'ControlMaster=yes',
            '-o',
            f'ControlPath={self.socket}',
            '-o',
            'ControlPersist=no',
            '-o',
            f'ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}',
            '-o',
            'ExitOnForwardFailure=yes',
        ]

    def master_argv(self) -> list[str]:
        """The master connection, logging in with the current certificate: no remote command, no forwards yet."""
        self._as_master = True
        try:
            return self.ssh_argv(self.credential(), '')[:-2]  # drop '--' and the empty command
        finally:
            self._as_master = False

    def control(self, op: str, forward: str = '') -> CommandResult:
        """One request to the master: ``check``, ``forward`` / ``cancel`` (with a ``-L`` spec) or ``exit``."""
        argv = [self._ssh, '-o', f'ControlPath={self.socket}', '-O', op]
        if forward:
            argv += ['-L', forward]
        argv += ['-p', str(self.port), f'{self.user}@{self.host}']
        try:
            proc = self._run(argv, capture_output=True, timeout=CONTROL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return CommandResult(255, '', f'ssh -O {op}: no answer from the master in {CONTROL_TIMEOUT_S:.0f}s')
        except OSError as e:
            return CommandResult(255, '', f'{self._ssh}: {e}')
        return CommandResult(proc.returncode, _text(proc.stdout), _text(proc.stderr))

    def close(self) -> None:
        """Discard the certificate. The master is the link's to stop."""
        if self._credential is not None:
            self._credential.discard()
            self._credential = None


MakeRunner = Callable[[BoxState, Path], TunnelRunner]


def ssh_runner_factory(ca_key: Path, known_hosts: Path) -> MakeRunner:
    def make(box: BoxState, control: Path) -> TunnelRunner:
        return TunnelRunner(
            box.host, box.port, CertificateAuthority(ca_key), known_hosts, key_id_for(box.box_id), control=control
        )

    return make


def http_status_probe(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """One request through the local port; any HTTP status line back counts. A forward whose far end does not
    answer is accepted locally and then closed by ssh, so only the status line says the whole path works."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(PROBE_REQUEST)
            line = s.makefile('rb').readline(256)
    except OSError as e:
        return False, f'no answer: {e}'
    if _STATUS_LINE.match(line):
        return True, ''
    return False, 'no HTTP status line: connection closed' if not line else f'no HTTP status line: {line[:40]!r}'


# ---------------------------------------------------------------- one box ---------------------------------------------


@dataclass(frozen=True)
class Target:
    local_port: int | None
    host_port: int | None


class BoxLink:
    """The master connection to one box and the forwards it carries."""

    def __init__(
        self,
        box: BoxState,
        control: Path,
        listen_host: str,
        make_runner: MakeRunner,
        popen: Callable[..., Any],
        probe: Probe,
        emit: Emit,
        clock: Callable[[], float],
        stop: threading.Event,
    ):
        self.box_id, self.host, self.port = box.box_id, box.host, box.port
        self.box = box
        self.control, self.listen_host = control, listen_host
        self._make_runner, self._popen, self._probe = make_runner, popen, probe
        self._emit, self._clock, self._stop = emit, clock, stop
        self.runner: TunnelRunner | None = None
        self.proc: Any = None
        self.bridge_gw = ''
        self.forwards: dict[str, str] = {}  # instance id -> the forward spec registered on the master
        self.failures = 0
        self.next_attempt = 0.0
        self.error = ''
        self.connects = 0
        self._forward_errors: dict[str, str] = {}

    @property
    def stderr_path(self) -> Path:
        return self.control.with_suffix('.err')

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # -- connection -----------------------------------------------------------------------------------------------

    def _clear_socket(self, runner: TunnelRunner) -> None:
        """A socket left at this box's path belongs to an earlier keeper that did not get to close it: stop its
        master (a no-op when none answers) so this one can listen there."""
        if self.control.exists():
            runner.control('exit')
            self.control.unlink(missing_ok=True)

    def connect(self) -> None:
        runner = self._make_runner(self.box, self.control)
        self.control.parent.mkdir(parents=True, exist_ok=True)
        self._clear_socket(runner)
        proc = None
        try:
            argv = runner.master_argv()  # mints the certificate for this connection
            with open(self.stderr_path, 'wb') as err:
                proc = self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
            deadline = time.monotonic() + MASTER_WAIT_S
            while not runner.control('check').ok:
                code = proc.poll()
                if code is not None:
                    raise SshTransportError(f'ssh exited {code}: {self._stderr_tail() or "no output"}')
                if self._stop.is_set() or time.monotonic() >= deadline:
                    raise SshTransportError(f'no connection within {MASTER_WAIT_S:.0f}s')
                self._stop.wait(0.25)
            self.bridge_gw = bridge_gateway(runner)
        except BaseException:
            if proc is not None:
                _terminate(proc)
            self.control.unlink(missing_ok=True)
            runner.close()
            raise
        runner.close()  # the login is done: the certificate is not needed again on this connection
        self.proc, self.runner = proc, runner
        self.forwards, self._forward_errors = {}, {}
        self.connects += 1

    def _stderr_tail(self) -> str:
        try:
            lines = self.stderr_path.read_text(errors='replace').strip().splitlines()
        except OSError:
            return ''
        return ' | '.join(lines[-3:])[:300]

    def close(self) -> None:
        if self.runner is not None and self.alive():
            self.runner.control('exit')
        if self.proc is not None:
            _terminate(self.proc)
        self.control.unlink(missing_ok=True)
        self.stderr_path.unlink(missing_ok=True)
        self.proc, self.runner, self.forwards = None, None, {}

    # -- one pass -------------------------------------------------------------------------------------------------

    def sync(self, targets: dict[str, Target]) -> dict[str, tuple[bool, str]]:
        """Connect if needed, make the master's forwards match ``targets`` and probe each one. Returns ``{instance:
        (up, error)}``."""
        now = self._clock()
        if self.proc is not None and not self.alive():
            code = self.proc.poll()
            self.error = f'connection closed (ssh exit {code}): {self._stderr_tail() or "no output"}'
            self._emit('down', box=self.box_id, instances=sorted(self.forwards), detail=self.error)
            self.proc, self.runner, self.forwards = None, None, {}
            self.next_attempt = now  # reconnect at once; backoff starts if that fails
        if not self.alive():
            if now < self.next_attempt:
                return {i: (False, self.error or 'connecting') for i in targets}
            try:
                self.connect()
            except (SshTransportError, CertificateError, PlacementError, OSError) as e:
                self.failures += 1
                delay = min(BACKOFF_CAP_S, 2.0 ** (self.failures - 1))
                self.next_attempt = self._clock() + delay
                self.error = f'connect failed: {str(e)[:300]} (retry in {delay:.0f}s)'
                self._emit('connect_failed', box=self.box_id, attempt=self.failures, retry_in_s=delay, detail=str(e)[:300])  # fmt: skip
                return {i: (False, self.error) for i in targets}
            self.failures, self.error = 0, ''
            self._emit('connect', box=self.box_id, host=self.host, port=self.port, bridge_gw=self.bridge_gw)
        return self._sync_forwards(targets)

    def _sync_forwards(self, targets: dict[str, Target]) -> dict[str, tuple[bool, str]]:
        assert self.runner is not None
        wanted = {
            i: forward_spec(self.listen_host, t.local_port, self.bridge_gw, t.host_port)
            for i, t in targets.items()
            if t.local_port is not None and t.host_port is not None
        }
        for instance, spec in list(self.forwards.items()):
            if wanted.get(instance) != spec:
                result = self.runner.control('cancel', spec)
                del self.forwards[instance]
                self._emit('cancel', box=self.box_id, instance=instance, forward=spec, ok=result.ok, detail=result.stderr.strip()[:200])  # fmt: skip
        out: dict[str, tuple[bool, str]] = {}
        for instance, target in targets.items():
            if target.local_port is None:
                out[instance] = (False, 'no free local port in --port-range')
                continue
            if target.host_port is None:
                out[instance] = (False, 'no host port on the instance record')
                continue
            spec = wanted[instance]
            if instance not in self.forwards:
                result = self.runner.control('forward', spec)
                if not result.ok:
                    why = f'forward failed: {(result.stderr or result.stdout).strip()[:200] or f"exit {result.exit_code}"}'
                    if self._forward_errors.get(instance) != why:
                        self._emit('forward', box=self.box_id, instance=instance, forward=spec, ok=False, detail=why)
                    self._forward_errors[instance] = why
                    out[instance] = (False, why)
                    continue
                self.forwards[instance] = spec
                self._forward_errors.pop(instance, None)
                self._emit('forward', box=self.box_id, instance=instance, forward=spec, ok=True, detail='')
            out[instance] = self._probe(self.listen_host, target.local_port)
        return out


def _terminate(proc: Any, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


# ---------------------------------------------------------------- the keeper ------------------------------------------


@dataclass
class Tunnel:
    box: str
    host: str
    port: int | None
    up: bool
    since: float
    error: str


class _PortPool:
    """Local ports from ``[low, high]``. New assignments go round the range from after the last one handed out, so a
    port just released is the last to be reused."""

    def __init__(self, low: int, high: int):
        self.low, self.high = low, high
        self.cursor = low

    def take(self, used: set[int]) -> int | None:
        size = self.high - self.low + 1
        for step in range(size):
            port = self.low + (self.cursor - self.low + step) % size
            if port not in used:
                self.cursor = port + 1 if port < self.high else self.low
                return port
        return None


class TunnelKeeper:
    def __init__(
        self,
        state_root: str | Path,
        make_runner: MakeRunner,
        *,
        listen_host: str = DEFAULT_LISTEN_HOST,
        port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
        popen: Callable[..., Any] = subprocess.Popen,
        probe: Probe = http_status_probe,
        emit: Emit | None = None,
        clock: Callable[[], float] = time.time,
        control_root: str | Path | None = None,
        max_workers: int = 64,
    ):
        self.root = Path(state_root)
        self.path = self.root / TUNNELS_FILE
        self.listen_host, self.port_range = listen_host, port_range
        self._make_runner, self._popen, self._probe = make_runner, popen, probe
        self._emit_line = emit or _print_event
        self._clock = clock
        # Unix socket paths are capped near 104 bytes, so the sockets sit under /tmp whatever TMPDIR says; the
        # directory is fixed per state dir so a restarted keeper finds what an earlier one left.
        tag = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:10]
        base = Path('/tmp') if os.path.isdir('/tmp') else Path(tempfile.gettempdir())
        self.control_root = Path(control_root) if control_root else base / f'gt-tun-{os.getuid()}-{tag}'
        self.stop_event = threading.Event()
        self.links: dict[str, BoxLink] = {}
        self.tunnels: dict[str, Tunnel] = {}
        self.ports: dict[str, int] = {}
        self._view: dict[str, tuple[BoxState | None, dict[str, int | None]]] = {}
        self._busy: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='gt-tunnel')
        self._pool_open = True
        self._ports = _PortPool(*port_range)
        self._load_ports()

    # -- events -----------------------------------------------------------------------------------------------------

    def emit(self, kind: str, **fields) -> None:
        self._emit_line({'event': 'tunnel', 'kind': kind, 'at': self._clock(), **fields})

    # -- state files ------------------------------------------------------------------------------------------------

    def _load_ports(self) -> None:
        """The local ports the last keeper handed out: an instance keeps its port across restarts."""
        doc = read_tunnels(self.path)
        low, high = self.port_range
        taken: set[int] = set()
        for instance, row in (doc or {}).get('tunnels', {}).items():
            port = row.get('port') if isinstance(row, dict) else None
            if isinstance(port, int) and low <= port <= high and port not in taken:
                self.ports[instance] = port
                taken.add(port)
        if taken:
            self._ports.cursor = max(taken) + 1 if max(taken) < high else low

    def _read_view(self) -> dict[str, tuple[BoxState | None, dict[str, int | None]]]:
        """``{box: (its record or None, {instance: host port})}`` for every box with an instance. Both files are
        replaced by rename, so a read sees a whole file; one that does not parse keeps the last view."""
        try:
            instances = InstanceStore(self.root / 'instances.json').instances
            boxes = StateStore(self.root / 'boxes.json').boxes
        except (OSError, ValueError, TypeError, KeyError) as e:
            self.emit('read_failed', detail=str(e)[:200])
            return self._view
        view: dict[str, tuple[BoxState | None, dict[str, int | None]]] = {}
        for record in instances.values():
            _, wanted = view.setdefault(record.box, (boxes.get(record.box), {}))
            wanted[record.id] = record.host_port
        return view

    # -- one pass ---------------------------------------------------------------------------------------------------

    def pass_once(self, wait_s: float | None = None) -> None:
        """Read both state files, hand each box to its worker (one still busy from the last pass is left to finish)
        and write ``tunnels.json``. ``wait_s``: wait that long for the workers before writing."""
        view = self._read_view()
        now = self._clock()
        with self._lock:
            self._view = view
            live = {i: box_id for box_id, (_, wanted) in view.items() for i in wanted}
            for instance in [i for i in self.ports if i not in live]:
                del self.ports[instance]
            for instance in [i for i in self.tunnels if i not in live]:
                del self.tunnels[instance]
            for instance in sorted(live):
                if instance not in self.ports:
                    port = self._ports.take(set(self.ports.values()))
                    if port is not None:
                        self.ports[instance] = port
                if instance not in self.tunnels:
                    self.tunnels[instance] = Tunnel(live[instance], self.listen_host, self.ports.get(instance), False, now, 'pending')  # fmt: skip
                self.tunnels[instance].port = self.ports.get(instance)
                self.tunnels[instance].box = live[instance]
            boxes = set(view) | set(self.links)
            if self._pool_open and not self.stop_event.is_set():
                for box_id in sorted(boxes):
                    if box_id in self._busy:
                        continue
                    future = self._pool.submit(self._sync_box, box_id)
                    self._busy[box_id] = future
                    future.add_done_callback(lambda f, b=box_id: self._done(b, f))
            pending = list(self._busy.values())
        if wait_s and pending:
            wait_futures(pending, timeout=wait_s)
        self.write()

    def _done(self, box_id: str, future: Future) -> None:
        with self._lock:
            self._busy.pop(box_id, None)
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            self.emit('error', box=box_id, detail=f'{type(error).__name__}: {error}'[:300])

    def _sync_box(self, box_id: str) -> None:
        with self._lock:
            box, wanted = self._view.get(box_id, (None, {}))
            targets = {i: Target(self.ports.get(i), hp) for i, hp in wanted.items()}
            link = self.links.get(box_id)
        if link is not None and (not wanted or box is None or (link.host, link.port) != (box.host, box.port)):
            link.close()  # no instance left there, or the box moved: the next pass connects afresh
            self.emit('close', box=box_id, detail='no instances' if not wanted else 'box address changed' if box else 'box removed')  # fmt: skip
            with self._lock:
                self.links.pop(box_id, None)
            link = None
        if not wanted:
            return
        if box is None:
            self._apply({i: (False, 'box not in boxes.json') for i in targets})
            return
        if link is None:
            control = self.control_root / hashlib.sha256(box_id.encode()).hexdigest()[:16]
            link = BoxLink(box, control, self.listen_host, self._make_runner, self._popen, self._probe, self.emit, self._clock, self.stop_event)  # fmt: skip
            with self._lock:
                self.links[box_id] = link
        if self.stop_event.is_set():
            return
        self._apply(link.sync(targets))

    def _apply(self, results: dict[str, tuple[bool, str]]) -> None:
        changed = False
        now = self._clock()
        with self._lock:
            for instance, (up, error) in results.items():
                row = self.tunnels.get(instance)
                if row is None:  # gone while the box was being visited
                    continue
                if row.up != up:
                    row.since = now
                    self.emit('up' if up else 'down', box=row.box, instance=instance, port=row.port, detail=error)
                    changed = True
                elif row.error != error:
                    changed = True
                row.up, row.error = up, ('' if up else error)
        if changed:
            self.write()

    # -- tunnels.json ----------------------------------------------------------------------------------------------

    def document(self) -> dict:
        with self._lock:
            rows = {i: asdict(t) for i, t in sorted(self.tunnels.items())}
        return {'schema': SCHEMA, 'written_at': self._clock(), 'listen_host': self.listen_host, 'tunnels': rows}

    def write(self) -> None:
        with self._write_lock:
            write_atomic(self.path, self.document())

    # -- the loop -------------------------------------------------------------------------------------------------------

    def serve(self, interval_s: float = DEFAULT_INTERVAL_S, max_passes: int = 0) -> None:
        """Reconcile every ``interval_s`` until ``stop_event`` is set, then :meth:`shutdown`."""
        self.emit('start', listen_host=self.listen_host, port_range=list(self.port_range), interval_s=interval_s)
        n = 0
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                self.pass_once()
                n += 1
                if max_passes and n >= max_passes:
                    break
                self.stop_event.wait(max(0.0, interval_s - (time.monotonic() - started)))
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop the workers, close every master and write every tunnel down."""
        self.stop_event.set()
        with self._lock:
            self._pool_open = False
        self._pool.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            links = list(self.links.values())
            self.links = {}
        for link in links:
            link.close()
        now = self._clock()
        with self._lock:
            for row in self.tunnels.values():
                if row.up:
                    row.since = now
                row.up, row.error = False, 'keeper stopped'
        self.write()
        self.emit('stop', boxes=len(links))


def _print_event(event: dict) -> None:
    with _PRINT_LOCK:
        print(json.dumps(event, default=str), flush=True)


_PRINT_LOCK = threading.Lock()


def write_atomic(path: Path, doc: dict) -> None:
    """tmp beside the file, then rename: a reader sees the old file or the new one, never a partial one."""
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, path)


def read_tunnels(path: Path) -> dict | None:
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get('schema') == SCHEMA else None
