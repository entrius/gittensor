# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``gitt controller``: the operator's entry point to the compute-pool controller (vault ``24`` §3, ``26`` §3, §5).

Everything here drives library code that already exists — the SSH certificate transport (``controller.ssh``), the
full check and box state (``controller.checks``), the GPU-proof slot (``controller.proof``):

    gitt controller discover [--network --netuid]              admit / move / remove boxes from the metagraph (read-only)
    gitt controller admit <hotkey> --host <ip> --port <port>   pin the box's host key, create it at ADMIT (dev boxes)
    gitt controller allowlist add <hotkey> | show              curate the NVML allowlist from a known-good box
    gitt controller check <hotkey>                             one full check: verdict, new state, exit 0 / 1 / 2
    gitt controller round [--loop]                             the 20-min two-phase probe over every idle card
    gitt controller release <hotkey> [--reason TEXT]           end a bench early: BENCHED -> ADMIT, withheld pay given back
    gitt controller bless <manifest.yaml> --image <repo@sha256> --sign-key <key>   sign an entry into the registry
    gitt controller deploy <entry> --enabled/--disabled --replicas N               operator deployment settings
    gitt controller registry show                              entries (re-verified), deployments, running counts
    gitt controller reconcile [--loop]                         desired replicas vs running instances, over SSH
    gitt controller instances                                  what runs where (what the gateway will read)
    gitt controller run                                        the controller as one process: round + reconcile + watch
    gitt controller tunnels [--status]                         one SSH connection per box carrying its instances' traffic
    gitt controller status                                     boxes, cards, instances, last round / reconcile (read-only)
    gitt controller scorecard                                  the last signed scorecard, checked as the validator does

State lives in one directory (``--state-dir``, default ``~/.gittensor/controller``): ``boxes.json`` (the
``StateStore``, cards included), ``known_hosts`` (host keys pinned at admit), ``nvml_allowlist.json``,
``registry/`` (signed entries), ``deployments.json``, ``instances.json`` and ``controller.json`` (what ``run`` last
did); the CA private key defaults to ``gt_ca`` beside them. The one-shot commands that change box or card state
(``check``, ``round``, ``reconcile``) hold ``controller.lock``, so a proof round never lands on a card mid-start. ``run``
holds it, and ``controller.run.lock``, for its whole life: a one-shot beside it refuses and points at ``status``, except
``check --force`` on a BENCHED box, which applies nothing.
Inside ``run`` the loops share the state in memory with per-box locks (``daemon.py``, ``locks.py``); ``admit``,
``deploy`` and ``release`` stay usable beside it (the daemon merges admitted boxes and release requests, and reads
deployments fresh each pass).
The GPU proof is chosen by config, never by code: ``--proof module:Class`` plus ``--proof-args key=value``
kwargs. Without one the fail-closed ``UnconfiguredProof`` benches every box with the reason named. No inbound
endpoints and no chain access: outbound SSH and local files only (``26`` §3).
"""

from __future__ import annotations

import base64
import fcntl
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import click
import yaml
from rich.markup import escape
from rich.table import Table

from gittensor.agent.config import AGENT_SSH_PORT, WORKLOAD_PORT_RANGE
from gittensor.cli.help import StyledGroup
from gittensor.cli.helpers import NETWORK_CHOICE, console, err_console
from gittensor.cli.json_output import emit_error_json, emit_json
from gittensor.cli.miner_commands.helpers import NETUID_DEFAULT, _resolve_endpoint
from gittensor.controller import tunnels
from gittensor.controller.checks import checks as ck
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.full_check import (
    FullCheckConfig,
    finish_verdict,
    identity_passed,
    judge_identity,
    proof_skipped,
    scrape_box,
)
from gittensor.controller.checks.nvml_allowlist import NvmlAllowlist
from gittensor.controller.checks.runner import CommandResult, HostRunner
from gittensor.controller.checks.scrape import (
    KERNEL_DRIVER_COMMAND,
    NVML_MD5_COMMAND,
    GpuInfo,
    HostScrape,
    nvidia_smi_command,
    parse_kernel_driver,
    parse_md5,
    parse_nvidia_smi,
)
from gittensor.controller.checks.state import (
    ADMIT,
    BENCHED,
    CHECKING,
    IDLE,
    BoxState,
    StateStore,
    apply_unreachable,
    apply_verdict,
    provable_uuids,
    release_from_bench,
    release_requested,
    remove_requested,
    request_release,
    request_remove,
)
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.daemon import STATUS_FILE, Controller, Intervals
from gittensor.controller.discovery import ChainReader, DiscoverReport, Discovery
from gittensor.controller.heartbeat import WatchReport
from gittensor.controller.locks import BoxLocks
from gittensor.controller.manifest import ManifestError
from gittensor.controller.pay.oracle import CoinGeckoChainOracle, FailSafeOracle, MetagraphedOracle, StaticOracle
from gittensor.controller.pay.rates import RatesError, load_rates
from gittensor.controller.proof.slot import (
    PROOF_IMAGE_PULLING,
    GpuProof,
    ProbeResult,
    ProofUnavailable,
    StagedProof,
    UnconfiguredProof,
    fire_box,
    image_ref,
    proof_image_ready,
    stage_box,
)
from gittensor.controller.publish import build_fleet, live_pay, pay_entry, scorecard_view, write_fleet
from gittensor.controller.reconcile import InstanceStore, Reconciler, ReconcileReport, gateway_healthz
from gittensor.controller.registry import (
    DeploymentStore,
    Registry,
    RegistryError,
    load_release_pubkey,
    make_entry,
    sign_bytes,
)
from gittensor.controller.runspec import BIND_PRIVATE, WORKLOAD_BINDS, PullToken
from gittensor.controller.ssh import (
    CertificateAuthority,
    SshRunner,
    SshTransportError,
    pinned_host_key,
    scan_host_key,
    write_host_key,
)
from gittensor.controller.ssh.certs import CertificateError
from gittensor.controller.standing import standing

DEFAULT_STATE_DIR = Path.home() / '.gittensor' / 'controller'
EXIT_ADMIT, EXIT_BENCH, EXIT_NO_VERDICT = 0, 1, 2  # 2: transport failure or nothing to check; no state changed
PREFLIGHT_COMMAND = 'true'  # one login before anything else, so a dead transport is told apart from a failing box
_DIGEST = re.compile(r'^(sha256:)?([0-9a-f]{64})$')
_KEY_ID_UNSAFE = re.compile(r'[^A-Za-z0-9_.:@=-]')
_TRANSPORT = ('SshTransportError', 'CertificateError')


# ---------------------------------------------------------------- state directory ----------------------------------


@dataclass(frozen=True)
class StateDir:
    root: Path

    @property
    def boxes(self) -> Path:
        return self.root / 'boxes.json'

    @property
    def known_hosts(self) -> Path:
        return self.root / 'known_hosts'

    @property
    def allowlist(self) -> Path:
        return self.root / 'nvml_allowlist.json'

    @property
    def ca_key(self) -> Path:
        return self.root / 'gt_ca'

    @property
    def registry(self) -> Path:
        return self.root / 'registry'

    @property
    def deployments(self) -> Path:
        return self.root / 'deployments.json'

    @property
    def instances(self) -> Path:
        return self.root / 'instances.json'

    def ensure(self) -> StateDir:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self

    def store(self) -> StateStore:
        return StateStore(self.boxes)

    def daemon_running(self) -> bool:
        """True while a `gitt controller run` holds this state directory."""
        path = self.root / 'controller.run.lock'
        if not path.exists():
            return False
        with open(path, 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive across processes: one writer of box and card state at a time (round, check, reconcile). Waits for
        another one-shot; raises ``ControllerRunning`` while `gitt controller run` owns the directory."""
        self.ensure()
        with open(self.root / 'controller.lock', 'w') as handle:
            while True:
                if self.daemon_running():
                    raise ControllerRunning(self.root)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.5)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        """`gitt controller run` for its whole life: ``controller.run.lock`` (a second `run` refuses) and then
        ``controller.lock`` (waiting for a one-shot already in flight to finish)."""
        self.ensure()
        with open(self.root / 'controller.run.lock', 'w') as run_handle:
            try:
                fcntl.flock(run_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ControllerRunning(self.root) from None
            try:
                with open(self.root / 'controller.lock', 'w') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                fcntl.flock(run_handle, fcntl.LOCK_UN)


class ControllerRunning(Exception):
    def __init__(self, root: Path):
        super().__init__(f'controller running on {root}, use `gitt controller status`')


def _host_field(host: str, port: int) -> str:
    return f'[{host}]:{port}'


# ---------------------------------------------------------------- the proof, by config ------------------------------


class ProofLoadError(Exception):
    """The configured provider could not be imported or constructed. Nothing is checked with it."""


def load_secret_store(path: Path) -> dict[str, bytes]:
    """``{version: base64 secret}`` JSON (gt-proof's ``secrets/secret_store.json``) as ``{version: bytes}``."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise ProofLoadError(f'secret store {path}: {e}') from e
    if not isinstance(raw, dict) or not raw:
        raise ProofLoadError(f'secret store {path}: expected a non-empty JSON object {{version: base64 secret}}')
    try:
        return {str(version): base64.b64decode(secret, validate=True) for version, secret in raw.items()}
    except (TypeError, ValueError) as e:
        raise ProofLoadError(f'secret store {path}: a secret is not base64 ({e})') from e


def proof_kwargs(pairs: Iterable[str]) -> dict[str, object]:
    """``key=value`` pairs as kwargs. ``@path`` reads the value from a file (a build's version id); the key
    ``secret_store`` is a path to the JSON secret store and is loaded into ``{version: bytes}``."""
    kwargs: dict[str, object] = {}
    for pair in pairs:
        key, sep, value = pair.partition('=')
        key = key.strip()
        if not sep or not key.isidentifier():
            raise ProofLoadError(f'--proof-args {pair!r}: expected key=value')
        if value.startswith('@'):
            try:
                value = Path(value[1:]).expanduser().read_text().strip()
            except OSError as e:
                raise ProofLoadError(f'--proof-args {key}: {e}') from e
        kwargs[key] = load_secret_store(Path(value).expanduser()) if key == 'secret_store' else value
    return kwargs


def load_proof(spec: str | None, pairs: Sequence[str] = ()) -> GpuProof:
    """``module:Class`` constructed with ``pairs`` as kwargs. No spec is the fail-closed ``UnconfiguredProof``. Called
    once per round, so a rebuilt binary and a rotated secret store are read fresh."""
    if not spec:
        if pairs:
            raise ProofLoadError('--proof-args given without --proof')
        return UnconfiguredProof()
    module_name, sep, attr = spec.partition(':')
    if not sep or not module_name or not attr:
        raise ProofLoadError(f'--proof {spec!r}: expected module:Class')
    kwargs = proof_kwargs(pairs)
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as e:
        raise ProofLoadError(f'--proof {spec}: {type(e).__name__}: {e}') from e
    try:
        return factory(**kwargs)
    except Exception as e:  # the provider refused its config (no secret for the version, no binary, ...)
        raise ProofLoadError(f'--proof {spec}: {type(e).__name__}: {e}') from e


# ---------------------------------------------------------------- timing ---------------------------------------------


class TimedRunner:
    """A ``HostRunner`` that records every command's start and end on our clock, for per-phase and per-check
    timings. Thread-safe: the proof fires every card from its own thread."""

    def __init__(self, inner: HostRunner, clock: Callable[[], float] = time.monotonic):
        self.inner, self._clock = inner, clock
        self.log: list[tuple[str, float, float]] = []
        self._lock = threading.Lock()

    def run(self, command: str, timeout: float | None = None, stdin: bytes | None = None) -> CommandResult:
        started = self._clock()
        try:
            return self.inner.run(command, timeout=timeout, stdin=stdin)
        finally:
            ended = self._clock()
            with self._lock:
                self.log.append((command, started, ended))

    def close(self) -> None:
        close = getattr(self.inner, 'close', None)
        if close:
            close()


PHASES = ('connect', 'scrape', 'stage', 'fire', 'cleanup')
_PROOF_COMMANDS = ('docker create', 'docker cp', 'docker start', 'docker rm')


def phase_of(command: str) -> str:
    if command == PREFLIGHT_COMMAND:
        return 'connect'
    if command.startswith(('docker create', 'docker cp')):
        return 'stage'
    if command.startswith('docker start'):
        return 'fire'
    if command.startswith('docker rm'):
        return 'cleanup'
    return 'scrape'


def checks_of(command: str) -> tuple[str, ...]:
    """Which checks a scrape command feeds (one nvidia-smi call feeds four)."""
    if command.startswith('nvidia-smi'):
        return ck.GPU_SPEC, ck.GPU_UUID_PIN, ck.FLEET_UUID_UNIQUE, ck.POWER_LIMIT
    if command in (NVML_MD5_COMMAND, KERNEL_DRIVER_COMMAND):
        return (ck.NVML_DIGEST,)
    if command.startswith('docker inspect'):
        return (ck.AGENT_IMAGE,)
    if command.startswith('df '):
        return (ck.DISK_FREE,)
    if command.startswith('curl '):
        return (ck.NETWORK,)
    if command.startswith(_PROOF_COMMANDS):
        return (ck.GPU_PROOF,)
    return ()


def _span_ms(entries: Sequence[tuple[str, float, float]]) -> float:
    return round((max(e for _, _, e in entries) - min(s for _, s, _ in entries)) * 1000.0, 1)


def phase_timings(log: Sequence[tuple[str, float, float]]) -> dict[str, float]:
    """Wall time per phase, first start to last end (the fire phase runs its cards in parallel)."""
    out = {}
    for phase in PHASES:
        entries = [entry for entry in log if phase_of(entry[0]) == phase]
        if entries:
            out[phase] = _span_ms(entries)
    if log:
        out['total'] = _span_ms(log)
    return out


def check_timings(log: Sequence[tuple[str, float, float]]) -> dict[str, float]:
    by_check: dict[str, list] = {}
    for entry in log:
        for name in checks_of(entry[0]):
            by_check.setdefault(name, []).append(entry)
    return {name: _span_ms(entries) for name, entries in by_check.items()}


# ---------------------------------------------------------------- seams (tests replace these) ----------------------


def _make_runner(state: StateDir, box: BoxState, ca_key: Path, purpose: str) -> HostRunner:
    key_id = _KEY_ID_UNSAFE.sub('_', f'ctl-{purpose}-{box.box_id}')[:128]
    return SshRunner(box.host, box.port, CertificateAuthority(ca_key), state.known_hosts, key_id)


def _scan_host_key(host: str, port: int) -> str:
    return scan_host_key(host, port)


def _run_build(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(command, shell=True, capture_output=True, text=True)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------- one box ---------------------------------------------


def transport_failure(scrape: HostScrape) -> str:
    """The scrape steps SSH itself failed on. Any is a lost box, not a failing one: no verdict, no state change."""
    lost = [f'{step}: {msg}' for step, msg in scrape.errors.items() if msg.startswith(_TRANSPORT)]
    return '; '.join(lost)[:500]


@dataclass
class CheckOutcome:
    verdict: CheckVerdict | None
    transport_error: str = ''
    busy: str = ''  # identity passed but every card hosts our workload: nothing to prove, no verdict
    proved: list[str] = field(default_factory=list)  # the cards the proof ran on


def busy_cards(box: BoxState, uuids: Iterable[str]) -> dict[str, str]:
    """The reported cards the proof skips this round, with their card state."""
    provable = set(provable_uuids(box, list(uuids)))
    return {uuid: box.card(uuid).state for uuid in uuids if uuid not in provable}


def _provable_gpus(box: BoxState, gpus: Sequence[GpuInfo]) -> list[GpuInfo]:
    provable = set(provable_uuids(box, [g.uuid for g in gpus]))
    return [g for g in gpus if g.uuid in provable]


def check_box(
    runner: HostRunner,
    box: BoxState,
    fleet_uuids: dict[str, Iterable[str]],
    proof: GpuProof,
    allowlist: NvmlAllowlist,
    config: FullCheckConfig,
    now: float,
    cards: Sequence[str] | None = None,
) -> CheckOutcome:
    """``run_full_check`` with a transport gate: a box SSH cannot reach gets no verdict instead of a BENCH. ``cards``
    limits the proof to those cards (the re-prove of CHECKING cards); identity is judged on the whole box either way."""
    try:
        runner.run(PREFLIGHT_COMMAND, timeout=config.ssh_timeout_s)
    except (SshTransportError, CertificateError) as e:
        return CheckOutcome(None, f'{type(e).__name__}: {e}'[:500])
    scrape = scrape_box(runner, config)
    lost = transport_failure(scrape)
    if lost:
        return CheckOutcome(None, lost)
    checks = judge_identity(scrape, allowlist, box.pinned_uuids or None, config, box.box_id, fleet_uuids)
    proved: list[str] = []
    if identity_passed(checks):
        gpus = _provable_gpus(box, scrape.gpus)
        if cards is not None:
            gpus = [g for g in gpus if g.uuid in cards]
        if not gpus:
            skipped = busy_cards(box, scrape.uuids)
            return CheckOutcome(
                None, busy='every card busy: ' + ', '.join(f'{u[:12]}… {s}' for u, s in skipped.items())
            )
        if not proof_image_ready(runner, config.proof_image):
            return CheckOutcome(None, busy=PROOF_IMAGE_PULLING)
        proved = [g.uuid for g in gpus]
        checks.append(ck.check_gpu_proof(runner, gpus, proof, config.proof_image, config.proof_timeout_s))
    else:
        checks.append(proof_skipped(checks))
    return CheckOutcome(finish_verdict(checks, scrape, now), proved=proved)


# ---------------------------------------------------------------- the fleet round ------------------------------------


@dataclass
class BoxRound:
    box: BoxState  # after release_from_bench
    status_before: str
    runner: TimedRunner | None = None
    scrape: HostScrape | None = None
    checks: list[CheckResult] = field(default_factory=list)
    staged: StagedProof | None = None
    stage_error: str = ''
    proved: list[GpuInfo] = field(default_factory=list)  # the IDLE / CHECKING cards staged and fired this round
    skipped: dict[str, str] = field(default_factory=dict)  # uuid -> the busy card state it was skipped in
    cards: list[dict] = field(default_factory=list)
    fired_at: float | None = None
    transport_error: str = ''
    verdict: CheckVerdict | None = None
    after: BoxState | None = None
    locked: bool = False  # this round holds the box's lock
    busy: str = ''  # not probed this round: its lock stayed held (a start or drain), or it was benched meanwhile


@dataclass
class RoundReport:
    provider: str
    boxes: list[BoxRound]
    not_probed: list[BoxState]
    timings_ms: dict[str, float]
    # Every box dialled failed at SSH and none was scraped: the controller's own link is the likelier fault, so no
    # box's unreachable count moved this round (Kimbo 9/16). Boxes flagged endpoint_changed are not dialled and count.
    no_box_answered: bool = False

    @property
    def exit_code(self) -> int:
        if any(r.transport_error for r in self.boxes):
            return EXIT_NO_VERDICT
        if any(r.verdict is not None and not r.verdict.admitted for r in self.boxes):
            return EXIT_BENCH
        return EXIT_ADMIT


def _each(rows: Sequence, fn: Callable) -> None:
    """``fn`` on every row at once, one thread each. ``fn`` records its own failures on the row."""
    if rows:
        with ThreadPoolExecutor(max_workers=len(rows)) as pool:
            list(pool.map(fn, rows))


def run_round(
    setup: CheckSetup,
    proof: GpuProof,
    clock: Callable[[], float] = time.monotonic,
    *,
    store: StateStore | None = None,
    write_lock: threading.RLock | None = None,
    box_locks: BoxLocks | None = None,
    lock_wait_s: float = cfg.ROUND_BOX_LOCK_WAIT_S,
    pending: Mapping[str, Collection[str]] | None = None,
) -> RoundReport:
    """One probe cycle over every ADMIT / IDLE box (``23`` §3b). Benches that have expired are released first.
    ``pending``: per box, cards an instance record still names (a lease ended while the box was unreachable, its
    container not yet undeployed): skipped this round like a busy card.

    Phase 1: connect and scrape every box in parallel; judge identity with fleet-wide UUID uniqueness over every pin
    and every card reported this round; stage the proof on every box that passed, in parallel. Phase 2: one start
    signal — every staged box fires at once (a thread per box, cards parallel inside ``fire_box``). Then clean up,
    judge, ``apply_verdict``. A box lost to SSH gets no verdict; its unreachable count goes up and three in a row
    bench it for 12 h (``apply_unreachable``), unless no dialled box answered at all: then the controller's own link
    is the suspect and no count moves (``RoundReport.no_box_answered``).

    Inside `gitt controller run` the round shares the daemon's ``store`` and ``write_lock`` and holds each box's lock
    from connect to verdict. A box whose lock stays held for ``lock_wait_s`` (a start or drain in flight) is skipped
    until the next round; a box the watch benched mid-round keeps its bench and gets no verdict; and only the cards the
    proof ran on return from CHECKING to IDLE."""
    store = store if store is not None else setup.state.store()
    write_lock = write_lock if write_lock is not None else threading.RLock()
    box_locks = box_locks if box_locks is not None else BoxLocks()
    now = time.time()
    rows: list[BoxRound] = []
    not_probed: list[BoxState] = []
    with write_lock:
        store.merge_from_disk()
        for box_id in sorted(store.boxes):
            before = store.boxes[box_id]
            box = store.boxes[box_id] = release_from_bench(before, now)
            if box.status in (ADMIT, IDLE) and box.host:
                rows.append(BoxRound(box, before.status))
            else:
                not_probed.append(box)
    config, allowlist = setup.config, setup.allowlist()
    provider = str(getattr(proof, 'version', '?'))
    marks = {'start': clock()}

    def connect_and_scrape(r: BoxRound) -> None:
        if not box_locks.acquire(r.box.box_id, lock_wait_s):
            r.busy = f'box busy {lock_wait_s:.0f} s (a start or drain in flight): skipped until the next round'
            return
        r.locked = True
        with write_lock:
            current = store.boxes.get(r.box.box_id)
        if current is None or current.status not in (ADMIT, IDLE):
            r.busy = f'{current.status if current else "removed"} meanwhile: not probed'
            return
        r.box = current  # no start can move its cards while we hold the lock
        if current.endpoint_changed:
            # Discovery saw the chain publish another address with another (or no) host key: no visit, no verdict, an
            # unreachable round, until an operator re-pins it or the pinned key answers there.
            moved = current.endpoint_changed
            r.transport_error = (
                f'endpoint_changed: chain publishes {moved.get("host")}:{moved.get("port")}, {moved.get("why", "")} '
                '(`gitt controller admit --force-rekey` after verifying)'
            )[:500]
            return
        r.runner = TimedRunner(_make_runner(setup.state, r.box, setup.ca_key, 'round'), clock)
        try:
            r.runner.run(PREFLIGHT_COMMAND, timeout=config.ssh_timeout_s)
        except (SshTransportError, CertificateError) as e:
            r.transport_error = f'{type(e).__name__}: {e}'[:500]
            return
        scrape = scrape_box(r.runner, config)
        r.transport_error = transport_failure(scrape)
        if not r.transport_error:
            r.scrape = scrape

    def stage(r: BoxRound) -> None:
        if r.runner is None or r.scrape is None:
            return  # only rows that connected and scraped are staged
        held = set((pending or {}).get(r.box.box_id, ()))
        r.proved = [g for g in _provable_gpus(r.box, r.scrape.gpus) if g.uuid not in held]
        r.skipped = busy_cards(r.box, r.scrape.uuids)
        r.skipped.update({u: f'{r.box.card(u).state} (instance pending)' for u in r.scrape.uuids if u in held})
        if not r.proved:
            return  # every card hosts our workload: nothing staged, nothing fired, no verdict
        try:
            ready = proof_image_ready(r.runner, config.proof_image)
        except Exception as e:  # transport died asking
            r.stage_error = f'staging failed: {type(e).__name__}: {e}'[:300]
            return
        if not ready:
            r.busy = PROOF_IMAGE_PULLING  # setup, not proof: no verdict this round
            return
        try:
            r.staged = stage_box(r.runner, r.proved, proof, config.proof_image, config.proof_timeout_s)
        except ProofUnavailable as e:
            r.stage_error = str(e)[:300]
        except Exception as e:  # transport died mid-stage
            r.stage_error = f'staging failed: {type(e).__name__}: {e}'[:300]

    def cleanup(r: BoxRound) -> None:
        if r.runner is None or r.staged is None:
            return
        command = proof.cleanup_command(r.staged)
        if command:
            try:
                r.runner.run(command, timeout=cfg.SSH_COMMAND_TIMEOUT_S)
            except Exception:  # best effort; the containers are labelled for a sweep
                pass

    armed: list[BoxRound] = []
    try:
        try:
            _each(rows, connect_and_scrape)
            marks['scraped'] = clock()
            with write_lock:
                fleet: dict[str, set[str]] = {box_id: set(b.pinned_uuids) for box_id, b in store.boxes.items()}
            for r in rows:
                if r.scrape is not None:
                    fleet.setdefault(r.box.box_id, set()).update(r.scrape.uuids)
            for r in rows:
                if r.scrape is not None:
                    r.checks = judge_identity(
                        r.scrape, allowlist, r.box.pinned_uuids or None, config, r.box.box_id, fleet
                    )
            _each([r for r in rows if r.scrape is not None and identity_passed(r.checks)], stage)
            marks['staged'] = clock()
            armed = [r for r in rows if r.staged is not None]
            if armed:
                gate = threading.Barrier(len(armed))

                def fire(r: BoxRound) -> None:
                    runner, staged = r.runner, r.staged
                    gate.wait()
                    r.fired_at = clock()
                    if runner is None or staged is None:
                        return  # ``armed`` holds only rows that staged, and staging needs the runner
                    try:
                        r.cards = fire_box(runner, r.proved, proof, staged, config.proof_timeout_s, clock)
                    except Exception as e:
                        r.stage_error = f'fire failed: {type(e).__name__}: {e}'[:300]

                _each(armed, fire)
            marks['fired'] = clock()
        finally:
            _each(armed, cleanup)
            marks['cleaned'] = clock()
            for r in rows:
                if r.runner is not None:
                    r.runner.close()

        now = time.time()
        dialled = [r for r in rows if r.runner is not None]
        no_box_answered = bool(dialled) and all(r.scrape is None for r in dialled)
        with write_lock:
            for r in rows:
                if r.busy:
                    continue
                current = store.boxes.get(r.box.box_id)
                if current is None or current.status != r.box.status:
                    r.after = current  # benched by the watch mid-round: the bench stands, no verdict applied
                    continue
                if r.scrape is None:
                    if r.transport_error and not (no_box_answered and r.runner is not None):
                        r.after = store.boxes[r.box.box_id] = apply_unreachable(current, now)
                    continue
                if not identity_passed(r.checks):
                    r.checks.append(proof_skipped(r.checks))
                elif not r.proved:
                    r.after = current  # identity passed and every card is busy: nothing proved, nothing applied
                    age_s = now - current.last_check_at if current.last_check_at else None
                    r.busy = (
                        'every card busy: '
                        + ', '.join(f'{u[:12]}… {s}' for u, s in r.skipped.items())
                        + (f'; last proof {age_s / 3600:.1f} h ago' if age_s is not None else '; never proved')
                    )
                    continue
                elif r.stage_error:
                    r.checks.append(ck.proof_result(ProbeResult(provider, cards=r.cards, error=r.stage_error)))
                else:
                    r.checks.append(ck.proof_result(ProbeResult(provider, cards=r.cards)))
                r.verdict = finish_verdict(r.checks, r.scrape, now)
                proved = [g.uuid for g in r.proved]
                r.after = store.boxes[r.box.box_id] = apply_verdict(current, r.verdict, now, proved=proved)
            store.save()
    finally:
        for r in rows:
            if r.locked:
                box_locks.release(r.box.box_id)

    def between(a: str, b: str) -> float | None:
        return round((marks[b] - marks[a]) * 1000.0, 1) if a in marks and b in marks else None

    timings = {
        'connect_scrape': between('start', 'scraped'),
        'stage': between('scraped', 'staged'),
        'fire': between('staged', 'fired'),
        'cleanup': between('fired', 'cleaned'),
        'total': between('start', 'cleaned'),
    }
    fired = [r.fired_at for r in armed if r.fired_at is not None]
    if fired:
        timings['fire_spread'] = round((max(fired) - min(fired)) * 1000.0, 3)
    return RoundReport(provider, rows, not_probed, {k: v for k, v in timings.items() if v is not None}, no_box_answered)


def reprove_box(
    setup: CheckSetup,
    proof: GpuProof,
    box_id: str,
    *,
    store: StateStore,
    write_lock: threading.RLock,
    box_locks: BoxLocks,
    lock_wait_s: float = cfg.ROUND_BOX_LOCK_WAIT_S,
    exclude: Collection[str] = (),
) -> RoundReport:
    """One box proved at once, inside `gitt controller run`, instead of at the next 20-min round: an IDLE box's CHECKING
    cards (Kimbo 9/15; not ``exclude``, the cards an instance record still names), or every card of a box at ADMIT,
    its first proof (Kimbo 9/16). Identity on the box and the same
    two-phase probe (``probe_box``: stage, fire, clean up) on those cards only, holding the box's lock, then
    ``apply_verdict`` (pinning an ADMIT box; returning only the proved cards to IDLE). A BENCH verdict benches the box
    as the round would. No verdict (the lock stayed held, no CHECKING card left, SSH down) changes nothing: the daemon
    retries later, and unreachable boxes are counted by the round."""
    provider = str(getattr(proof, 'version', '?'))
    started = time.monotonic()
    with write_lock:
        box = store.boxes.get(box_id)
    row = BoxRound(box or BoxState(box_id), box.status if box else '?')

    def report() -> RoundReport:
        return RoundReport(provider, [row], [], {'total': round((time.monotonic() - started) * 1000.0, 1)})

    if not box_locks.acquire(box_id, lock_wait_s):
        row.busy = f'box busy {lock_wait_s:.0f} s (a start, drain or round in flight): retried later'
        return report()
    try:
        with write_lock:
            current = store.boxes.get(box_id)
            fleet: dict[str, Iterable[str]] = {
                b.box_id: list(b.pinned_uuids) for b in store.boxes.values() if b.box_id != box_id
            }
        if current is None or current.status not in (ADMIT, IDLE) or not current.host or current.endpoint_changed:
            why = 'endpoint changed' if current is not None and current.endpoint_changed else None
            row.busy = f'{why or (current.status if current else "removed")} meanwhile: not re-proved'
            return report()
        cards: list[str] | None = None  # ADMIT: every card the box reports, its first proof
        if current.status == IDLE:
            cards = sorted(
                uuid for uuid, card in current.cards.items() if card.state == CHECKING and uuid not in exclude
            )
            if not cards:
                row.busy = 'no CHECKING card left: nothing to re-prove'
                return report()
        row.box, row.status_before = current, current.status
        row.runner = TimedRunner(_make_runner(setup.state, current, setup.ca_key, 'reprove'))
        try:
            outcome = check_box(
                row.runner, current, fleet, proof, setup.allowlist(), setup.config, time.time(), cards=cards
            )
        finally:
            row.runner.close()
        row.transport_error, row.busy = outcome.transport_error, outcome.busy
        if outcome.verdict is None:
            return report()
        with write_lock:
            latest = store.boxes.get(box_id)
            if latest is None or latest.status != current.status:
                row.after = latest  # benched by the watch meanwhile: the bench stands, no verdict applied
            else:
                row.verdict = outcome.verdict
                row.after = store.boxes[box_id] = apply_verdict(
                    latest, outcome.verdict, time.time(), proved=outcome.proved
                )
                store.save()
    finally:
        box_locks.release(box_id)
    return report()


# ---------------------------------------------------------------- options + setup ------------------------------------


def _fail(message: str, json_mode: bool, code: int, **extra) -> NoReturn:
    if json_mode:
        emit_error_json(message, **extra)
    else:
        err_console.print(f'[red]Error:[/red] {escape(message)}')
    sys.exit(code)


def _digests(ctx, param, values) -> tuple[str, ...]:
    out = []
    for value in values:
        m = _DIGEST.match(value.strip().lower())
        if not m:
            raise click.BadParameter(f'{value!r} is not sha256:<64 hex>')
        out.append(f'sha256:{m.group(2)}')
    return tuple(out)


def _state_options(f: Callable) -> Callable:
    f = click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')(f)
    return click.option(
        '--state-dir',
        type=click.Path(file_okay=False, path_type=Path),
        default=DEFAULT_STATE_DIR,
        show_default=True,
        help='boxes.json, known_hosts, nvml_allowlist.json (and gt_ca by default).',
    )(f)


def _chain_options(f: Callable) -> Callable:
    """Which metagraph discovery reads (read-only: the controller holds no chain key)."""
    options = [
        click.option(
            '--netuid', type=int, default=NETUID_DEFAULT, show_default=True, help='Subnet whose metagraph is read.'
        ),
        click.option('--network', type=NETWORK_CHOICE, default=None, help='Network name (local, test, finney).'),
        click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).'),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def _chain_reader(endpoint: str, netuid: int) -> ChainReader:
    return ChainReader(endpoint, netuid)


def _ca_key_option(f: Callable) -> Callable:
    return click.option(
        '--ca-key',
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help='SSH CA private key that signs the per-visit certificates (default: <state-dir>/gt_ca).',
    )(f)


def _check_options(f: Callable) -> Callable:
    options = [
        click.option(
            '--proof',
            'proof_spec',
            default=None,
            metavar='MODULE:CLASS',
            help='GPU-proof provider to load. None: every box is benched, the reason named.',
        ),
        click.option(
            '--proof-args',
            multiple=True,
            metavar='KEY=VALUE',
            help='Provider kwargs. "@path" reads a value from a file; secret_store=<json> loads {version: base64}.',
        ),
        click.option(
            '--agent-image-digest',
            'digests',
            multiple=True,
            callback=_digests,
            metavar='SHA256',
            help='A published agent image digest to accept.',
        ),
        click.option(
            '--agent-image-id',
            'image_ids',
            multiple=True,
            callback=_digests,
            metavar='SHA256',
            help='Dev boxes only: the exact image ID of a locally built agent (it has no repo digest).',
        ),
        click.option('--proof-image', default=image_ref(), show_default=True, help='Image the proof job runs in.'),
        click.option(
            '--allowlist',
            'allowlist_location',
            default=None,
            help='NVML allowlist file or URL (default: <state-dir>/nvml_allowlist.json).',
        ),
        click.option(
            '--network-target',
            'network_targets',
            multiple=True,
            metavar='URL',
            help='URLs the box must reach (default: Docker Hub and Hugging Face).',
        ),
        click.option('--disk-min-gb', type=float, default=cfg.DISK_MIN_FREE_GB, show_default=True),
        _ca_key_option,
    ]
    for option in reversed(options):
        f = option(f)
    return f


@dataclass
class CheckSetup:
    state: StateDir
    ca_key: Path
    proof_spec: str | None
    proof_args: tuple[str, ...]
    config: FullCheckConfig
    allowlist_location: str

    def proof(self) -> GpuProof:
        return load_proof(self.proof_spec, self.proof_args)

    def allowlist(self) -> NvmlAllowlist:
        location = self.allowlist_location
        if not location.startswith(('http://', 'https://')) and not Path(location).exists():
            return NvmlAllowlist({}, source=f'{location} (missing: every driver fails closed)')
        return NvmlAllowlist.load(location)


def _setup(
    state_dir: Path,
    ca_key: Path | None,
    proof_spec: str | None,
    proof_args: Sequence[str],
    digests: Sequence[str],
    image_ids: Sequence[str],
    proof_image: str,
    allowlist_location: str | None,
    network_targets: Sequence[str],
    disk_min_gb: float,
) -> CheckSetup:
    state = StateDir(Path(state_dir).expanduser())
    config = FullCheckConfig(
        agent_image_digests=tuple(digests),
        agent_image_ids=tuple(image_ids),
        proof_image=proof_image,
        network_targets=tuple(network_targets) or tuple(cfg.NETWORK_TARGETS),
        disk_min_free_gb=disk_min_gb,
    )
    return CheckSetup(
        state,
        Path(ca_key).expanduser() if ca_key else state.ca_key,
        proof_spec,
        tuple(proof_args),
        config,
        str(allowlist_location or state.allowlist),
    )


@contextmanager
def _one_shot_lock(state: StateDir, json_mode: bool) -> Iterator[None]:
    """``controller.lock`` for a one-shot; refuses (exit 2) while `gitt controller run` owns the state directory."""
    try:
        with state.lock():
            yield
    except ControllerRunning as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)


def _read_pull_token(path: Path | None, json_mode: bool) -> PullToken | None:
    if not path:
        return None
    try:
        return PullToken.parse(path.read_text())
    except (OSError, ValueError) as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)


def _admitted_box(store: StateStore, hotkey: str, json_mode: bool) -> BoxState:
    box = store.boxes.get(hotkey)
    if box is None or not box.host:
        _fail(
            f'{hotkey} is not admitted: run `gitt controller admit {hotkey} --host <ip> --port <port>` first',
            json_mode,
            EXIT_NO_VERDICT,
        )
    return box


def _require_ca_key(path: Path, json_mode: bool) -> None:
    if not path.is_file():
        _fail(f'CA private key not found at {path} (--ca-key)', json_mode, EXIT_NO_VERDICT)


def _when(ts: float | None) -> str:
    return '?' if ts is None else time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(ts))


# ---------------------------------------------------------------- rendering ----------------------------------------


_MARK = {'pass': '[green]✓ pass[/green]', 'fail': '[red]✗ fail[/red]', 'skip': '[dim]— skip[/dim]'}


def _status(c: CheckResult) -> str:
    return 'skip' if c.skipped else ('pass' if c.passed else 'fail')


def _card_detail(card: dict) -> str:
    parts = [f'{card.get("uuid", "?")[:12]}…', str(card.get('reason', ''))]
    if card.get('elapsed_ms') is not None:
        parts.append(f'ours {card["elapsed_ms"]:.0f} ms')
    if card.get('wall_ms'):
        parts.append(f'kernel {card["wall_ms"]:.0f} ms')
    if card.get('filled_bytes'):
        parts.append(f'filled {card["filled_bytes"] / 1e9:.1f} GB')
    return ' '.join(parts)


def check_detail(c: CheckResult) -> str:
    ev = c.evidence
    if c.name == ck.GPU_PROOF and ev.get('cards'):
        return f'{ev.get("provider", "?")}: ' + '; '.join(_card_detail(card) for card in ev['cards'])
    if c.skipped or not c.passed:
        return str(ev.get('reason', ''))
    if c.name == ck.GPU_SPEC:
        return f'{ev.get("count")}× {ev.get("model")}'
    if c.name == ck.GPU_UUID_PIN:
        return str(ev.get('reason') or f'{len(ev.get("pinned", []))} pinned')
    if c.name == ck.FLEET_UUID_UNIQUE:
        return f'vs {ev.get("boxes_compared", 0)} other box(es)'
    if c.name == ck.NVML_DIGEST:
        return f'driver {ev.get("driver")}, md5 {str(ev.get("md5", ""))[:12]}…'
    if c.name == ck.POWER_LIMIT:
        return ', '.join(f'{r["limit_w"]:.0f} / {r["default_w"]:.0f} W' for r in ev.get('readings', []))
    if c.name == ck.AGENT_IMAGE:
        return str(ev.get('matched') or 'published digest')
    if c.name == ck.DISK_FREE:
        return f'{ev.get("free_gb")} GB free (floor {ev.get("min_gb")})'
    if c.name == ck.NETWORK:
        return ', '.join(f'{t["http_code"]}' for t in ev.get('targets', {}).values())
    return ''


def verdict_table(verdict: CheckVerdict, check_ms: dict[str, float], title: str) -> Table:
    table = Table(title=title, show_header=True)
    table.add_column('Check', style='cyan', no_wrap=True)
    table.add_column('Status', no_wrap=True)
    table.add_column('ms', justify='right', no_wrap=True)
    table.add_column('Detail', style='dim')
    for c in verdict.checks:
        ms = check_ms.get(c.name)
        table.add_row(c.name, _MARK[_status(c)], '' if ms is None else f'{ms:.0f}', escape(check_detail(c)))
    return table


def _timings_text(timings: dict[str, float]) -> str:
    return ' · '.join(
        f'{name} {ms:.0f} ms' if name != 'fire_spread' else f'{name} {ms:.1f} ms' for name, ms in timings.items()
    )


def _verdict_markup(verdict: CheckVerdict | None) -> str:
    if verdict is None:
        return '[yellow]no verdict[/yellow]'
    return '[green]ADMIT[/green]' if verdict.admitted else '[red]BENCH[/red]'


# ---------------------------------------------------------------- commands -----------------------------------------


@click.group(name='controller', cls=StyledGroup)
def controller_group():
    """The compute-pool controller: admit boxes, check them, run the probe round.

    \b
    Commands:
        admit      Pin a box's host key and create it at ADMIT
        allowlist  Curate the NVML allowlist (driver -> libnvidia-ml md5)
        check      One full check of one box: verdict, state, exit 0 ADMIT / 1 BENCH / 2 no verdict
        round      The two-phase probe over every idle card (--loop: every 20 min)
        release    End a bench early: BENCHED → ADMIT, with a reason (safe beside run)
        bless      Sign a manifest + digest-pinned image into the registry
        deploy     Enable / disable a registry entry and set its replica count
        registry   Show the registry, re-verified, with deployments
        reconcile  Place and drain instances until running matches desired (--loop: every 30 s)
        instances  List placement instances: entry, box, card, container, host:port, healthy
        run        The controller as one process: proof round, reconcile, heartbeat + health watch
        tunnels    Keep one SSH connection per box forwarding its instances to local ports (--status)
        status     Boxes, cards, instances, last round / reconcile / watch (read-only, safe beside run)
    """


def _parse_port_maps(values: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        inner, sep, public = value.partition('=')
        if not sep or not inner.isdigit() or not public.isdigit():
            raise ValueError(f'--port-map {value!r}: expected PORT=PUBLIC')
        out[str(int(inner))] = int(public)
    return out


def _parse_port_range(value: str) -> list[int]:
    low, sep, high = value.partition('-')
    high = high if sep else low
    if not low.isdigit() or not high.isdigit() or not 1 <= int(low) <= int(high) <= 65535:
        raise ValueError(f'--workload-ports {value!r}: expected LOW-HIGH (1-65535, LOW <= HIGH)')
    return [int(low), int(high)]


@controller_group.command('admit')
@click.argument('hotkey')
@click.option('--host', required=True, help="The box's address.")
@click.option('--port', type=int, default=AGENT_SSH_PORT, show_default=True, help="The agent sshd's port.")
@click.option(
    '--force-rekey', is_flag=True, default=False, help='Re-pin a host key that changed (verify the box first).'
)
@click.option(
    '--port-map',
    'port_maps',
    multiple=True,
    metavar='PORT=PUBLIC',
    help='A host that remaps published ports (a Lium pod): instances on host port PORT are reached on PUBLIC.',
)
@click.option(
    '--workload-ports',
    default=None,
    metavar='LOW-HIGH',
    help=f'Host ports instances are published on (default: {WORKLOAD_PORT_RANGE[0]}-{WORKLOAD_PORT_RANGE[1]}, '
    'what `gitt up` opens). A dev box whose provider exposes other ports: e.g. 8080-8080 with --port-map.',
)
@_state_options
def admit_command(hotkey, host, port, force_rekey, port_maps, workload_ports, state_dir, json_mode):
    """Pin a box's SSH host key (trust on first use, once) and create it at ADMIT.

    A changed host key is refused unless --force-rekey. Re-admitting keeps the box's status, pin and bench, and clears
    an endpoint change discovery flagged. `gitt controller discover` admits registered boxes by itself; this stays for
    dev boxes and for resolving an endpoint change.
    """
    try:
        port_map = _parse_port_maps(port_maps)
        port_range = _parse_port_range(workload_ports) if workload_ports else None
    except ValueError as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)
    state = StateDir(Path(state_dir).expanduser()).ensure()
    try:
        key = _scan_host_key(host, port)
    except SshTransportError as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)
    store = state.store()
    box = store.boxes.get(hotkey)
    previous = {k for k in ((box.host_key if box else ''), pinned_host_key(state.known_hosts, host, port)) if k}
    changed = sorted(k for k in previous if k != key)
    if changed and not force_rekey:
        _fail(
            f'host key at {_host_field(host, port)} changed: pinned {changed[0]}, box now presents {key}. '
            'Refusing to re-pin; verify the box, then pass --force-rekey.',
            json_mode,
            EXIT_BENCH,
        )
    new = box is None
    box = BoxState(hotkey, source='operator') if new else BoxState.from_dict(box.as_dict())
    if box.host and (box.host, box.port) != (host, port):
        write_host_key(state.known_hosts, box.host, box.port, None)  # the box moved: drop its old address
    box.host, box.port, box.host_key = host, port, key
    box.endpoint_changed = {}
    if port_maps:
        box.port_map = port_map
    if port_range:
        box.workload_ports = port_range
    write_host_key(state.known_hosts, host, port, key)
    store.put(box)
    if json_mode:
        emit_json(
            {
                'success': True,
                'hotkey': hotkey,
                'host': host,
                'port': port,
                'host_key': key,
                'status': box.status,
                'new': new,
                'rekeyed': bool(changed),
                'port_map': box.port_map,
                'workload_ports': box.workload_ports,
            }
        )
        return
    verb = 'Admitted' if new else ('Re-keyed' if changed else 'Updated')
    err_console.print(
        f'[green]{verb}[/green] {escape(hotkey)} at {escape(_host_field(host, port))}: host key {escape(key)} '
        f'pinned, status {box.status}.'
    )


@controller_group.command('check')
@click.argument('hotkey')
@click.option(
    '--force',
    is_flag=True,
    default=False,
    help='Check a BENCHED box anyway (operator debugging). The verdict is shown but NOT applied; the bench stands. '
    'Allowed beside `gitt controller run`.',
)
@_check_options
@_state_options
def check_command(hotkey, force, state_dir, json_mode, **opts):
    """One full check of one admitted box: scrape, judge, the GPU proof on every card, then apply the verdict.

    \b
    Exit 0 ADMIT, 1 BENCH, 2 no verdict (transport failure: the unreachable count goes up; 3 in a row benches 12 h).
    Beside `gitt controller run` only `check --force` on a BENCHED box runs (it writes nothing); anything else refuses.
    Example (dev box, sealed proof):
        gitt controller check 5F... --agent-image-id sha256:... \\
            --proof gittensor_proof.provider:SealedProof \\
            --proof-args secret_store=secrets/secret_store.json \\
            --proof-args version=@dist/gt_proof.version --proof-args binary_path=dist/gt_proof
    """
    setup = _setup(state_dir, **opts)
    if force and setup.state.daemon_running():
        # A forced check never applies its verdict and a benched box has no leases, so it may run beside `run`
        # (Kimbo 9/15); on any other box it refuses below, exactly as the lock would.
        _check_one(setup, hotkey, force, json_mode, beside_daemon=True)
        return
    with _one_shot_lock(setup.state, json_mode):
        _check_one(setup, hotkey, force, json_mode)


def _check_one(setup: CheckSetup, hotkey: str, force: bool, json_mode: bool, beside_daemon: bool = False) -> None:
    store = setup.state.store()
    box = _admitted_box(store, hotkey, json_mode)
    _require_ca_key(setup.ca_key, json_mode)
    now = time.time()
    released = release_from_bench(box, now)
    if beside_daemon and released.status != BENCHED:
        _fail(str(ControllerRunning(setup.state.root)), json_mode, EXIT_NO_VERDICT)
    forced = released.status == BENCHED and force
    if released.status == BENCHED and not force:
        _fail(
            f'{hotkey} is BENCHED until {_when(box.bench_until)} ({", ".join(box.last_failed) or "?"}); not checked'
            ' (--force to check anyway without touching the bench)',
            json_mode,
            EXIT_BENCH,
        )
    if forced and not json_mode:
        err_console.print(
            f'[yellow]--force: {hotkey} is BENCHED until {_when(box.bench_until)}; the verdict will not be applied[/yellow]'
        )
    try:
        proof = setup.proof()
    except ProofLoadError as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)
    fleet: dict[str, Iterable[str]] = {
        b.box_id: list(b.pinned_uuids) for b in store.boxes.values() if b.box_id != hotkey
    }
    runner = TimedRunner(_make_runner(setup.state, released, setup.ca_key, 'check'))
    try:
        outcome = check_box(runner, released, fleet, proof, setup.allowlist(), setup.config, now)
    finally:
        runner.close()
    timings = phase_timings(runner.log)
    if outcome.busy:
        _fail(f'no verdict — {outcome.busy}; state unchanged', json_mode, EXIT_NO_VERDICT, hotkey=hotkey)
    if outcome.verdict is None:
        after = released if forced else apply_unreachable(released, now)
        if not forced:
            store.put(after)
        benched = ' — BENCHED for 12 h' if after.status == BENCHED and released.status != BENCHED else ''
        _fail(
            f'no verdict — transport failure: {outcome.transport_error} '
            f'(unreachable {after.unreachable_count} round(s) in a row{benched})',
            json_mode,
            EXIT_NO_VERDICT,
            hotkey=hotkey,
            unreachable_count=after.unreachable_count,
            status=after.status,
            timings_ms=timings,
        )
    verdict = outcome.verdict
    after = released if forced else apply_verdict(released, verdict, now)
    if not forced:
        store.put(after)
    per_check = check_timings(runner.log)
    if json_mode:
        emit_json(
            {
                'success': verdict.admitted,
                'hotkey': hotkey,
                'host': _host_field(box.host, box.port),
                'provider': str(getattr(proof, 'version', '?')),
                'status': {'before': box.status, 'after': after.status},
                'timings_ms': timings,
                'check_ms': per_check,
                **verdict.as_dict(),
            }
        )
    else:
        title = f'gitt controller check — {hotkey} @ {_host_field(box.host, box.port)}'
        console.print(verdict_table(verdict, per_check, escape(title)))
        console.print(
            f'{_verdict_markup(verdict)}  {box.status} → {after.status}'
            + (f' (bench until {_when(after.bench_until)})' if after.status == BENCHED else '')
        )
        console.print(f'[dim]{_timings_text(timings)}[/dim]')
    sys.exit(EXIT_ADMIT if verdict.admitted else EXIT_BENCH)


@controller_group.command('round')
@click.option('--loop', is_flag=True, default=False, help='Repeat every --interval seconds.')
@click.option(
    '--interval', type=float, default=cfg.FULL_CHECK_INTERVAL_S, show_default=True, help='Seconds between round starts.'
)
@click.option(
    '--build-cmd',
    default=None,
    help='Shell command run between rounds (a fresh proof build); the proof is re-loaded after it.',
)
@click.option('--max-rounds', type=int, default=0, hidden=True)
@_check_options
@_state_options
def round_command(loop, interval, build_cmd, max_rounds, state_dir, json_mode, **opts):
    """The probe cycle over every ADMIT / IDLE box: release expired benches, stage the proof on every box (phase 1),
    fire every box at one instant (phase 2), judge, apply verdicts.

    \b
    Exit (last round): 0 all ADMIT, 1 any BENCH, 2 any box without a verdict.
    With --loop, --build-cmd runs between rounds, e.g. gt-proof's `ci/gen_secret.sh && ci/build.sh`, and the
    provider (binary, secret store, @version file) is re-read before the next round.
    """
    setup = _setup(state_dir, **opts)
    _require_ca_key(setup.ca_key, json_mode)
    proof: GpuProof | None = None
    n = 0
    while True:
        started = time.monotonic()
        n += 1
        try:
            proof = setup.proof()
        except ProofLoadError as e:
            if proof is None:
                _fail(str(e), json_mode, EXIT_NO_VERDICT)
            err_console.print(f'[yellow]Round {n}: keeping the previous proof — {escape(str(e))}[/yellow]')
        with _one_shot_lock(setup.state, json_mode):
            report = run_round(setup, proof)
        _print_round(report, n, json_mode)
        if not loop or (max_rounds and n >= max_rounds):
            break
        if build_cmd:
            built_at = time.monotonic()
            proc = _run_build(build_cmd)
            took = (time.monotonic() - built_at) * 1000.0
            tail = (proc.stdout or proc.stderr or '').strip().splitlines()[-1:] or ['']
            style = 'dim' if proc.returncode == 0 else 'yellow'
            err_console.print(
                f'[{style}]build exit {proc.returncode} in {took:.0f} ms: {escape(tail[0][:200])}[/{style}]'
            )
        _sleep(max(0.0, interval - (time.monotonic() - started)))
    sys.exit(report.exit_code)


def _cards_text(r: BoxRound) -> str:
    """'1 proved · 1 skipped (LEASED)': the fleet table says which cards a round left alone and why."""
    if r.scrape is None:
        return ''
    parts = [f'{len(r.proved)} proved'] if r.proved or not r.skipped else []
    if r.skipped:
        states = ', '.join(sorted(set(r.skipped.values())))
        parts.append(f'{len(r.skipped)} skipped ({states})')
    return ' · '.join(parts)


def _print_round(report: RoundReport, n: int, json_mode: bool) -> None:
    if json_mode:
        emit_json(
            {
                'success': report.exit_code == EXIT_ADMIT,
                'round': n,
                'provider': report.provider,
                'timings_ms': report.timings_ms,
                'no_box_answered': report.no_box_answered,
                'boxes': [
                    {
                        'hotkey': r.box.box_id,
                        'host': _host_field(r.box.host, r.box.port),
                        'status': {'before': r.status_before, 'after': (r.after or r.box).status},
                        'transport_error': r.transport_error,
                        'busy': r.busy,
                        'cards': {'proved': [g.uuid for g in r.proved], 'skipped': r.skipped},
                        'timings_ms': phase_timings(r.runner.log) if r.runner else {},
                        'check_ms': check_timings(r.runner.log) if r.runner else {},
                        **(r.verdict.as_dict() if r.verdict else {'verdict': None}),
                    }
                    for r in report.boxes
                ],
                'not_probed': [
                    {'hotkey': b.box_id, 'status': b.status, 'bench_until': b.bench_until} for b in report.not_probed
                ],
            }
        )
        return
    table = Table(title=f'gitt controller round {n} — provider {escape(report.provider)}', show_header=True)
    for column in ('Hotkey', 'Host', 'State', 'Verdict', 'Cards', 'Proof ms', 'Failed / reason'):
        table.add_column(column, no_wrap=column != 'Failed / reason')
    for r in report.boxes:
        proof_ms = [ms for c in r.cards if (ms := c.get('elapsed_ms')) is not None]
        if r.verdict is None:
            reason = r.transport_error or r.busy or ('identity ok; every card busy' if r.scrape is not None else '')
        else:
            reason = '; '.join(
                f'{c.name}: {check_detail(c)}' for c in r.verdict.checks if not c.passed and not c.skipped
            )
        if r.busy:
            verdict_cell = '[dim]busy[/dim]'
        elif r.verdict is not None or r.scrape is None:
            verdict_cell = _verdict_markup(r.verdict)
        else:
            verdict_cell = '[dim]no proof[/dim]'
        table.add_row(
            escape(r.box.box_id),
            escape(_host_field(r.box.host, r.box.port)),
            f'{r.status_before} → {(r.after or r.box).status}',
            verdict_cell,
            escape(_cards_text(r)),
            f'{max(proof_ms):.0f}' if proof_ms else '',
            escape(reason),
        )
    for b in report.not_probed:
        table.add_row(
            escape(b.box_id), escape(_host_field(b.host, b.port)), b.status, '[dim]not probed[/dim]', '', '', ''
        )
    console.print(table)
    if report.no_box_answered:
        console.print(
            "[red]no box answered SSH: the controller's own link is suspect; no unreachable round counted[/red]"
        )
    console.print(f'[dim]{_timings_text(report.timings_ms) or "no boxes to probe"}[/dim]')


# ---------------------------------------------------------------- release / remove ---------------------------------


@controller_group.command('remove')
@click.argument('hotkey')
@click.option('--reason', default='', help='Why the box is forgotten: logged by the controller when it drops it.')
@_state_options
def remove_command(hotkey, reason, state_dir, json_mode):
    """Forget a box: its record and its pinned host key are dropped. For a box that is gone for good (a rented pod
    returned, a machine retired); a benched box would otherwise wait out its bench and be probed forever. Refuses a
    box that still carries an instance (exit 1): drain it first (`deploy --replicas 0`, or `release` and wait).

    
    Beside `gitt controller run` the request is recorded in boxes.json and the controller drops the box on its next
    reconcile pass; without one it is dropped at once. A box discovery admitted from the chain comes back on the next
    discovery pass while its hotkey still publishes a compute endpoint.
    """
    state = StateDir(Path(state_dir).expanduser())
    store = state.store()
    box = store.boxes.get(hotkey)
    if box is None:
        _fail(f'{hotkey} is not admitted', json_mode, EXIT_NO_VERDICT)
    live = InstanceStore(state.instances).on_box(hotkey)
    if live:
        _fail(f'{hotkey} still carries {len(live)} instance(s): drain it first', json_mode, EXIT_BENCH)
    store.put(request_remove(box, time.time(), reason))
    removed = False
    if not state.daemon_running():
        try:
            with state.lock():
                store = state.store()
                if hotkey in store.boxes and not InstanceStore(state.instances).on_box(hotkey):
                    store.remove(hotkey)
                    if box.host:
                        write_host_key(state.known_hosts, box.host, box.port, None)
                    removed = True
        except ControllerRunning:
            removed = False  # `run` started meanwhile: it applies the request on its next pass
    if json_mode:
        emit_json({'success': True, 'hotkey': hotkey, 'reason': reason, 'removed': removed, 'pending': not removed})
        return
    if removed:
        console.print(f'{escape(hotkey)} removed')
    else:
        console.print(
            f'{escape(hotkey)}: removal requested; the running controller drops it on its next reconcile pass'
        )


@controller_group.command('release')
@click.argument('hotkey')
@click.option('--reason', default='', help='Why the bench ends early: recorded on the `released` standing event.')
@_state_options
def release_command(hotkey, reason, state_dir, json_mode):
    """End a bench early: BENCHED → ADMIT, re-pinned by the next proof round like an expired bench, with a `released`
    standing event carrying the reason. The ladder rung stays; the pay withheld by the bench (the box's UTC day ±1) is
    given back: the ledger rows already written stay, the next settlement pays them.

    \b
    Beside `gitt controller run` the release is recorded in boxes.json and the controller applies it on its next round;
    without one it applies at once. Refuses a box that is not benched (exit 1).
    """
    state = StateDir(Path(state_dir).expanduser())
    store = state.store()
    box = store.boxes.get(hotkey)
    if box is None:
        _fail(f'{hotkey} is not admitted', json_mode, EXIT_NO_VERDICT)
    if box.status != BENCHED:
        _fail(f'{hotkey} is {box.status}, not benched: nothing to release', json_mode, EXIT_BENCH)
    store.put(request_release(box, time.time(), reason))
    after: BoxState | None = None
    if not state.daemon_running():
        try:
            with state.lock():
                store = state.store()
                current = store.boxes[hotkey]
                after = release_from_bench(current, time.time())
                if after is not current:
                    store.put(after)
        except ControllerRunning:
            after = None  # `run` started meanwhile: it applies the request on its next round
    released = after is not None and after.status != BENCHED
    until = _when(box.bench_until)
    if json_mode:
        emit_json(
            {
                'success': True,
                'hotkey': hotkey,
                'reason': reason,
                'released': released,
                'pending': not released,
                'bench_until': box.bench_until,
                'status': after.status if after is not None else BENCHED,
            }
        )
        return
    if released:
        err_console.print(
            f'[green]Released[/green] {escape(hotkey)}: BENCHED (until {until}) → ADMIT; the next round or check '
            're-pins it.'
        )
    else:
        err_console.print(
            f'[green]Release recorded[/green] for {escape(hotkey)} (BENCHED until {until}): the running controller '
            'applies it on its next round.'
        )


# ---------------------------------------------------------------- allowlist ----------------------------------------


def _read_allowlist(path: Path, json_mode: bool) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or '{}')
        if not isinstance(data, dict):
            raise ValueError('not a JSON object')
    except ValueError as e:
        _fail(f'{path}: {e}', json_mode, EXIT_NO_VERDICT)
    return {str(k): sorted({v} if isinstance(v, str) else set(v)) for k, v in data.items()}


@controller_group.group(name='allowlist', cls=StyledGroup)
def allowlist_group():
    """The NVML allowlist the full check judges against: driver version -> libnvidia-ml.so.1 md5s.

    Operator-curated: `add` trusts the box it reads from, so only add from a box you know is genuine.
    """


@allowlist_group.command('add')
@click.argument('hotkey')
@_ca_key_option
@_state_options
def allowlist_add(hotkey, ca_key, state_dir, json_mode):
    """Read an admitted box's driver version and NVML library md5 over SSH and add them to the allowlist."""
    state = StateDir(Path(state_dir).expanduser())
    box = _admitted_box(state.store(), hotkey, json_mode)
    ca_key = Path(ca_key).expanduser() if ca_key else state.ca_key
    _require_ca_key(ca_key, json_mode)
    runner = _make_runner(state, box, ca_key, 'allowlist')
    try:
        smi = runner.run(nvidia_smi_command(), timeout=cfg.NVIDIA_SMI_TIMEOUT_S)
        md5 = runner.run(NVML_MD5_COMMAND, timeout=cfg.SSH_COMMAND_TIMEOUT_S)
        kernel = runner.run(KERNEL_DRIVER_COMMAND, timeout=cfg.SSH_COMMAND_TIMEOUT_S)
    except (SshTransportError, CertificateError) as e:
        _fail(f'{type(e).__name__}: {e}', json_mode, EXIT_NO_VERDICT)
    finally:
        getattr(runner, 'close', lambda: None)()
    try:
        drivers = sorted({g.driver for g in parse_nvidia_smi(smi.stdout)}) if smi.ok else []
    except ValueError:
        drivers = []
    kernel_driver = parse_kernel_driver(kernel.stdout) if kernel.ok else ''
    digest = parse_md5(md5.stdout) if md5.ok else ''
    if len(drivers) != 1:
        problem = f'nvidia-smi reports drivers {drivers or "none"}'
    elif kernel_driver != drivers[0]:
        problem = f'nvidia-smi driver {drivers[0]} disagrees with the kernel module ({kernel_driver or "unreadable"})'
    elif not digest:
        problem = 'libnvidia-ml.so.1 not found or unhashed'
    else:
        problem = ''
    if problem:
        _fail(f'nothing added: {problem}', json_mode, EXIT_BENCH)
    driver = drivers[0]
    data = _read_allowlist(state.allowlist, json_mode)
    added = digest not in data.get(driver, [])
    data[driver] = sorted({*data.get(driver, []), digest})
    state.ensure()
    state.allowlist.write_text(json.dumps(dict(sorted(data.items())), indent=2) + '\n')
    nvml_path = md5.stdout.split()[1] if len(md5.stdout.split()) > 1 else ''
    if json_mode:
        emit_json(
            {
                'success': True,
                'hotkey': hotkey,
                'driver': driver,
                'md5': digest,
                'nvml_path': nvml_path,
                'added': added,
                'allowlist': str(state.allowlist),
            }
        )
        return
    verb = '[green]Added[/green]' if added else '[dim]Already present:[/dim]'
    err_console.print(f'{verb} driver {driver} → {digest} ({escape(nvml_path)}) in {escape(str(state.allowlist))}')


@allowlist_group.command('show')
@_state_options
def allowlist_show(state_dir, json_mode):
    """Print the allowlist."""
    state = StateDir(Path(state_dir).expanduser())
    data = _read_allowlist(state.allowlist, json_mode)
    if json_mode:
        emit_json({'success': True, 'allowlist': str(state.allowlist), 'drivers': data})
        return
    if not data:
        err_console.print(f'[yellow]{escape(str(state.allowlist))} is empty: every box fails nvml_digest.[/yellow]')
        return
    table = Table(title=escape(str(state.allowlist)), show_header=True)
    table.add_column('Driver', style='cyan', no_wrap=True)
    table.add_column('libnvidia-ml.so.1 md5', no_wrap=True)
    for driver, digests in data.items():
        table.add_row(driver, '\n'.join(digests))
    console.print(table)


# ---------------------------------------------------------------- registry + placement -----------------------------


def _registry_options(f: Callable) -> Callable:
    f = click.option(
        '--allow-dev-keys', is_flag=True, default=False, help='Trust a --release-pubkey tagged DO-NOT-SHIP (dev).'
    )(f)
    return click.option(
        '--release-pubkey',
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help='Public key registry entries must verify against (default: the compiled release key).',
    )(f)


def _open_registry(state: StateDir, release_pubkey: Path | None, allow_dev_keys: bool, json_mode: bool) -> Registry:
    try:
        return Registry(state.registry, load_release_pubkey(release_pubkey, allow_dev_keys))
    except RegistryError as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)


@controller_group.command('bless')
@click.argument('manifest_path', type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option('--image', required=True, help='The image as repo[:tag]@sha256:<64 hex>; pinned over the manifest image.')
@click.option(
    '--sign-key', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path), help='Release key.'
)
@click.option(
    '--qualified',
    'qualified_path',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help='JSON of our qualification measurements {at, box, driver, load_s, health_ok, canary_ok, vram_gb, '
    'decode_tps_single?, prefill_tps?, notes}: signed beside the manifest, never inside it.',
)
@click.option(
    '--source-image',
    default=None,
    help="The author's reference --image was copied from (same digest): signed into the entry as provenance.",
)
@_registry_options
@_state_options
def bless_command(
    manifest_path, image, sign_key, qualified_path, source_image, release_pubkey, allow_dev_keys, state_dir, json_mode
):
    """Sign a manifest and its digest-pinned image together into the registry as <name>@<version>.

    The manifest must pass the schema and the consistency checks. The signature is checked against the key the
    controller trusts before the entry is written. A different entry under the same name@version (a changed image,
    manifest or qualified block) is refused: bump the version.
    """
    state = StateDir(Path(state_dir).expanduser()).ensure()
    registry = _open_registry(state, release_pubkey, allow_dev_keys, json_mode)
    try:
        document = yaml.safe_load(manifest_path.read_text())
        qualified = json.loads(qualified_path.read_text()) if qualified_path else None
        verified = make_entry(
            document if isinstance(document, dict) else {}, image, qualified=qualified, source_image=source_image
        )
    except (yaml.YAMLError, ManifestError) as e:
        problems = getattr(e, 'problems', [str(e)])
        _fail(f'{manifest_path}: ' + '; '.join(problems), json_mode, EXIT_BENCH, problems=problems)
    except RegistryError as e:  # a malformed --qualified block, or a --source-image with another digest
        _fail(str(e), json_mode, EXIT_BENCH)
    except ValueError as e:  # the --qualified file is not JSON
        _fail(f'{qualified_path}: {e}', json_mode, EXIT_BENCH)
    entry = verified.entry
    path, _ = registry.paths(entry.entry_id)
    if path.exists():
        try:
            current = registry.read(entry.entry_id).entry
        except RegistryError:
            current = None
        if current is not None and (current.image, current.manifest, current.qualified, current.source_image) == (
            entry.image,
            entry.manifest,
            entry.qualified,
            entry.source_image,
        ):
            _bless_output(json_mode, entry.entry_id, current.image, path, already=True)
            return
        _fail(f'{entry.entry_id} is already blessed with different content: bump version', json_mode, EXIT_BENCH)
    try:
        registry.write(verified, sign_bytes(entry.canonical_bytes(), sign_key))
    except RegistryError as e:
        _fail(str(e), json_mode, EXIT_BENCH)
    _bless_output(json_mode, entry.entry_id, entry.image, path, already=False)


def _bless_output(json_mode: bool, entry_id: str, image: str, path: Path, already: bool) -> None:
    if json_mode:
        emit_json({'success': True, 'entry': entry_id, 'image': image, 'path': str(path), 'already': already})
        return
    verb = '[dim]Already blessed:[/dim]' if already else '[green]Blessed[/green]'
    err_console.print(f'{verb} {escape(entry_id)} → {escape(image)} ({escape(str(path))})')


@controller_group.command('deploy')
@click.argument('entry_id')
@click.option('--enabled/--disabled', 'enabled', default=None, help='Run it (desired = replicas) or not (desired = 0).')
@click.option('--replicas', type=click.IntRange(min=0), default=None, help='Instances wanted while enabled.')
@click.option('--box', 'box', default=None, help='Place only on this box (hotkey): a canary run on our own card.')
@click.option('--any-box', is_flag=True, default=False, help='Clear a --box pin.')
@_registry_options
@_state_options
def deploy_command(entry_id, enabled, replicas, box, any_box, release_pubkey, allow_dev_keys, state_dir, json_mode):
    """Operator deployment settings for one registry entry; the next reconcile acts on them.

    Enabling re-verifies the entry first; an entry that does not verify cannot be enabled.
    """
    state = StateDir(Path(state_dir).expanduser()).ensure()
    if enabled is None and replicas is None and box is None and not any_box:
        _fail(
            'nothing to change: pass --enabled/--disabled, --replicas, --box or --any-box', json_mode, EXIT_NO_VERDICT
        )
    if any_box:
        box = ''
    if enabled:
        try:
            _open_registry(state, release_pubkey, allow_dev_keys, json_mode).read(entry_id)
        except RegistryError as e:
            _fail(f'not enabled: {e}', json_mode, EXIT_BENCH)
    deployment = DeploymentStore(state.deployments).set(entry_id, enabled, replicas, box)
    if json_mode:
        emit_json(
            {
                'success': True,
                'entry': entry_id,
                'enabled': deployment.enabled,
                'replicas': deployment.replicas,
                'box': deployment.box,
            }
        )
        return
    err_console.print(
        f'{escape(entry_id)}: {"[green]enabled[/green]" if deployment.enabled else "[yellow]disabled[/yellow]"}, '
        f'replicas {deployment.replicas} (desired {deployment.desired})'
        + (f', pinned to {escape(deployment.box)}' if deployment.box else '')
    )


@controller_group.group(name='registry', cls=StyledGroup)
def registry_group():
    """Blessed entries (signed image + manifest) and their operator-owned deployment settings."""


@registry_group.command('show')
@_registry_options
@_state_options
def registry_show(release_pubkey, allow_dev_keys, state_dir, json_mode):
    """Every entry, re-verified on this read, with its deployment and running instance count."""
    state = StateDir(Path(state_dir).expanduser())
    registry = _open_registry(state, release_pubkey, allow_dev_keys, json_mode)
    deployments = DeploymentStore(state.deployments)
    instances = InstanceStore(state.instances).instances.values()
    rows = []
    for entry_id in sorted(set(registry.ids()) | set(deployments.deployments)):
        deployment = deployments.get(entry_id)
        row = {
            'entry': entry_id,
            'enabled': deployment.enabled,
            'replicas': deployment.replicas,
            'running': sum(1 for r in instances if r.entry == entry_id and not r.draining),
        }
        try:
            verified = registry.read(entry_id)
            e = verified.entry
            row.update(
                verified=True,
                image=e.image,
                source_image=e.source_image,
                blessed_at=e.blessed_at,
                qualified=e.qualified,
                error='',
            )
        except RegistryError as e:
            row.update(verified=False, image='', source_image=None, blessed_at=None, qualified=None, error=str(e))
        rows.append(row)
    if json_mode:
        emit_json({'success': all(r['verified'] for r in rows), 'entries': rows})
        return
    table = Table(title=escape(str(state.registry)), show_header=True)
    for column in ('Entry', 'Verified', 'Enabled', 'Replicas', 'Running', 'Qualified', 'Image / error'):
        table.add_column(column, no_wrap=column not in ('Qualified', 'Image / error'))
    for r in rows:
        table.add_row(
            escape(r['entry']),
            '[green]✓[/green]' if r['verified'] else '[red]✗[/red]',
            'yes' if r['enabled'] else 'no',
            str(r['replicas']),
            str(r['running']),
            escape(_qualified_text(r['qualified'])),
            escape(r['image'] or r['error']),
        )
    console.print(table)


def _qualified_text(q: dict | None) -> str:
    """One line of our qualification measurements for `registry show`."""
    if not q:
        return '—'
    parts = [
        f'{str(q["box"])[:16]}, driver {q["driver"]}',
        f'load {q["load_s"]} s',
        f'health {"ok" if q["health_ok"] else "FAIL"}',
        f'canary {"ok" if q["canary_ok"] else "FAIL"}',
        f'{q["vram_gb"]} GB',
    ]
    if 'decode_tps_single' in q:
        parts.append(f'{q["decode_tps_single"]} tok/s single')
    if 'prefill_tps' in q:
        parts.append(f'prefill {q["prefill_tps"]} tok/s')
    if q.get('notes'):
        parts.append(str(q['notes']))
    return ' · '.join(parts)


def _workload_bind_option(fn):
    return click.option(
        '--workload-bind',
        type=click.Choice(WORKLOAD_BINDS),
        default=BIND_PRIVATE,
        show_default=True,
        help="Where a new workload's port is published: private, the box's docker bridge address; public, the "
        'previous publish form. Applies to new starts; running instances keep theirs.',
    )(fn)


@controller_group.command('reconcile')
@click.option('--loop', is_flag=True, default=False, help='Repeat every --interval seconds.')
@click.option('--interval', type=float, default=cfg.RECONCILE_INTERVAL_S, show_default=True)
@click.option(
    '--pull-token-file',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help='Read-only registry token, one line "username:token"; installed for each pull and removed after.',
)
@_workload_bind_option
@click.option('--max-passes', type=int, default=0, hidden=True)
@_registry_options
@_ca_key_option
@_state_options
def reconcile_command(
    loop,
    interval,
    pull_token_file,
    workload_bind,
    max_passes,
    release_pubkey,
    allow_dev_keys,
    ca_key,
    state_dir,
    json_mode,
):
    """Make running instances match every enabled deployment × replicas: start on IDLE cards that fit, drain what is
    over, disabled or unverifiable, and re-adopt our labelled containers after a restart.

    \b
    Exit (last pass): 0 converged cleanly, 1 a failed start / drain or an entry not run, 2 a box unreachable.
    """
    state = StateDir(Path(state_dir).expanduser()).ensure()
    ca_key = Path(ca_key).expanduser() if ca_key else state.ca_key
    _require_ca_key(ca_key, json_mode)
    registry = _open_registry(state, release_pubkey, allow_dev_keys, json_mode)
    token = _read_pull_token(pull_token_file, json_mode)
    n = 0
    while True:
        started = time.monotonic()
        n += 1
        with _one_shot_lock(state, json_mode):
            reconciler = Reconciler(
                boxes=state.store(),
                instances=InstanceStore(state.instances),
                deployments=DeploymentStore(state.deployments),
                registry=registry,
                make_runner=lambda box: _make_runner(state, box, ca_key, 'reconcile'),
                pull_token=token,
                workload_bind=workload_bind,
                sleep=_sleep,
                visit_all=n == 1,
            )
            report = reconciler.run_pass()
        _print_reconcile(report, n, reconciler, json_mode)
        if not loop or (max_passes and n >= max_passes):
            break
        _sleep(max(0.0, interval - (time.monotonic() - started)))
    sys.exit(EXIT_NO_VERDICT if report.unreachable else (EXIT_ADMIT if report.ok else EXIT_BENCH))


def _print_reconcile(report: ReconcileReport, n: int, reconciler: Reconciler, json_mode: bool) -> None:
    if json_mode:
        emit_json(
            {
                'success': report.ok,
                'pass': n,
                'desired': report.desired,
                'running': report.running,
                'actions': [vars(a) for a in report.actions],
                'errors': report.errors,
                'unreachable': report.unreachable,
                'timings_ms': report.timings_ms,
                'instances': _instance_rows(reconciler.instances, reconciler.boxes),
            }
        )
        return
    table = Table(title=f'gitt controller reconcile {n}', show_header=True)
    for column in ('Action', 'Box', 'Instance', 'Entry', 'Card', 'States', 'ms', 'Detail'):
        table.add_column(column, no_wrap=column != 'Detail')
    for a in report.actions:
        mark = '[green]✓[/green]' if a.ok else '[red]✗[/red]'
        total = sum(v for k, v in a.timings_ms.items() if not k.startswith('prestage.'))
        table.add_row(
            f'{mark} {a.kind}',
            escape(a.box[:16]),
            escape(a.instance),
            escape(a.entry),
            escape(f'{a.uuid[:12]}…' if a.uuid else ''),
            ' → '.join(a.states),
            f'{total:.0f}' if a.timings_ms else '',
            escape(a.detail),
        )
    console.print(table)
    summary = ', '.join(f'{e} {report.running.get(e, 0)}/{d}' for e, d in report.desired.items()) or 'no deployments'
    console.print(f'[dim]running/desired: {escape(summary)} · {_timings_text(report.timings_ms)}[/dim]')
    for line in report.errors:
        err_console.print(f'[yellow]{escape(line)}[/yellow]')
    for box_id, why in report.unreachable.items():
        err_console.print(f'[red]unreachable[/red] {escape(box_id)}: {escape(why)}')


def _instance_rows(instances: InstanceStore, boxes: StateStore) -> list[dict]:
    rows = []
    for record in sorted(instances.instances.values(), key=lambda r: (r.entry, r.box, r.id)):
        box = boxes.boxes.get(record.box)
        card = box.card(record.uuid).state if box and box.status == IDLE else (box.status if box else '?')
        rows.append({**vars(record), 'card_state': card})
    return rows


@controller_group.command('instances')
@_state_options
def instances_command(state_dir, json_mode):
    """Placement instances as recorded: what the gateway reads to route."""
    state = StateDir(Path(state_dir).expanduser())
    rows = _instance_rows(InstanceStore(state.instances), state.store())
    if json_mode:
        emit_json({'success': True, 'instances': rows})
        return
    if not rows:
        err_console.print('[dim]no instances[/dim]')
        return
    table = Table(title=escape(str(state.instances)), show_header=True)
    for column in ('Instance', 'Entry', 'Box', 'Card', 'State', 'Container', 'Host:port', 'Healthy', 'Draining'):
        table.add_column(column, no_wrap=True)
    for r in rows:
        table.add_row(
            escape(r['id']),
            escape(r['entry']),
            escape(r['box'][:16]),
            escape(f'{r["uuid"][:12]}…'),
            r['card_state'],
            escape(r['container_id'][:12]),
            escape(f'{r["host"]}:{r["port"]}' if r['port'] else r['host']),
            '[green]yes[/green]' if r['healthy'] else '[red]no[/red]',
            'yes' if r['draining'] else 'no',
        )
    console.print(table)


# ---------------------------------------------------------------- tunnels: the traffic path, its own process --------


@controller_group.command('tunnels')
@click.option('--status', is_flag=True, default=False, help='Print tunnels.json and exit (read-only).')
@click.option(
    '--listen-host',
    default=tunnels.DEFAULT_LISTEN_HOST,
    show_default=True,
    help='Address the local ports listen on: the docker network gateway the gateway container reaches this host at.',
)
@click.option(
    '--port-range',
    default=f'{tunnels.DEFAULT_PORT_RANGE[0]}-{tunnels.DEFAULT_PORT_RANGE[1]}',
    show_default=True,
    help='Local ports handed to instances; an instance keeps its port for its whole life.',
)
@click.option('--interval', type=float, default=tunnels.DEFAULT_INTERVAL_S, show_default=True, help='Seconds between passes.')  # fmt: skip
@click.option('--max-passes', type=int, default=0, hidden=True)
@_ca_key_option
@_state_options
def tunnels_command(status, listen_host, port_range, interval, max_passes, ca_key, state_dir, json_mode):
    """Keep one SSH connection per box that carries an instance, with a local forward per instance to where the
    workload answers on the box, and write <state-dir>/tunnels.json (what the gateway routes by) every pass.

    \b
    Its own long-running process (pm2: gt-tunnels), beside `run`: restarting the controller leaves the connections
    that carry traffic up. Forwards are added and cancelled on the live connection, never by reconnecting. One JSON
    line per event on stdout. SIGTERM closes every connection, writes every tunnel down and exits 0.
    """
    state = StateDir(Path(state_dir).expanduser())
    if status:
        _print_tunnels(state, json_mode)
        return
    try:
        ports = tunnels.parse_port_range(port_range)
    except ValueError as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)
    state.ensure()
    ca_key = Path(ca_key).expanduser() if ca_key else state.ca_key
    _require_ca_key(ca_key, json_mode)
    try:
        with tunnels.keeper_lock(state.root):
            keeper = _make_keeper(state, ca_key, listen_host, ports)
            stop = keeper.stop_event
            previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                keeper.serve(interval_s=interval, max_passes=max_passes)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    except tunnels.KeeperRunning as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)


def _make_keeper(state: StateDir, ca_key: Path, listen_host: str, ports: tuple[int, int]) -> tunnels.TunnelKeeper:
    make_runner = tunnels.ssh_runner_factory(ca_key, state.known_hosts)
    return tunnels.TunnelKeeper(state.root, make_runner, listen_host=listen_host, port_range=ports)


def _print_tunnels(state: StateDir, json_mode: bool) -> None:
    path = state.root / tunnels.TUNNELS_FILE
    doc = tunnels.read_tunnels(path)
    if doc is None:
        _fail(f'{path}: no tunnels written yet (is gt-tunnels running?)', json_mode, EXIT_NO_VERDICT)
    if json_mode:
        emit_json({'success': True, **doc})
        return
    now = time.time()
    rows = doc.get('tunnels', {})
    table = Table(
        title=f'{escape(str(path))} · written {_age(doc.get("written_at"), now)} ago · listen {escape(str(doc.get("listen_host", "")))}',
        show_header=True,
    )
    for column in ('Instance', 'Box', 'Local', 'Up', 'Since', 'Error'):
        table.add_column(column, no_wrap=column != 'Error')
    for instance, row in rows.items():
        table.add_row(
            escape(instance),
            escape(str(row.get('box', ''))[:16]),
            escape(f'{row.get("host")}:{row.get("port")}' if row.get('port') else '—'),
            '[green]up[/green]' if row.get('up') else '[red]down[/red]',
            _age(row.get('since'), now),
            escape(str(row.get('error', ''))),
        )
    console.print(table)
    if not rows:
        err_console.print('[dim]no instances[/dim]')


# ---------------------------------------------------------------- run: the controller as one process ----------------


def _age(ts: float | None, now: float) -> str:
    if ts is None:
        return '—'
    s = max(0.0, now - ts)
    return f'{s:.0f}s' if s < 120 else (f'{s / 60:.0f}m' if s < 7200 else f'{s / 3600:.1f}h')


class _DaemonPrinter:
    """`run`'s log: one timestamped (UTC) line per event on stderr, or one JSON object per event on stdout (--json)."""

    def __init__(self, json_mode: bool):
        self.json_mode = json_mode
        self._lock = threading.Lock()
        self._last_errors: list[str] = []
        self._last_ignored: list[str] = []

    def _emit(self, event: str, payload: dict, line: str) -> None:
        with self._lock:
            if self.json_mode:
                print(json.dumps({'event': event, 'at': time.time(), **payload}, default=str), flush=True)
            else:
                err_console.print(f'[dim]{time.strftime("%H:%M:%S", time.gmtime())}[/dim] {line}')

    def round(self, report: RoundReport, n: int) -> None:
        self._probe('round', f'round {n}', {'round': n}, report)

    def reprove(self, report: RoundReport) -> None:
        self._probe('reprove', 're-prove', {}, report)

    def _probe(self, event: str, label: str, head: dict, report: RoundReport) -> None:
        parts, rows = [], []
        for r in report.boxes:
            after = (r.after or r.box).status
            why = r.busy or r.transport_error
            if r.verdict is not None and not r.verdict.admitted:
                why = '; '.join(
                    f'{c.name}: {check_detail(c)}' for c in r.verdict.checks if not c.passed and not c.skipped
                )
            mark = _verdict_markup(r.verdict) if r.verdict is not None else ('[dim]busy[/dim]' if r.busy else '')
            parts.append(
                f'{escape(r.box.box_id[:16])} {r.status_before}→{after} {mark} {escape(_cards_text(r))}'
                + (f' ({escape(why[:200])})' if why else '')
            )
            rows.append(
                {
                    'hotkey': r.box.box_id,
                    'before': r.status_before,
                    'after': after,
                    'verdict': r.verdict.verdict if r.verdict else None,
                    'busy': r.busy,
                    'transport_error': r.transport_error,
                    'failed': r.verdict.failed if r.verdict else [],
                    # why each failed: the operator's log only (fleet.json publishes the names, never the detail)
                    'why': {c.name: check_detail(c)[:300] for c in r.verdict.checks if not c.passed and not c.skipped}
                    if r.verdict
                    else {},
                }
            )
        payload = {**head, 'provider': report.provider, 'exit_code': report.exit_code, 'boxes': rows}
        payload['timings_ms'] = report.timings_ms
        self._emit(
            event, payload, f'[cyan]{label}[/cyan] {escape(report.provider)}: ' + (' · '.join(parts) or 'no boxes')
        )

    def _actions(self, event: str, actions: Sequence) -> None:
        for a in actions:
            mark = '[green]✓[/green]' if a.ok else '[red]✗[/red]'
            states = ' → '.join(a.states)
            line = f'{mark} {event} {a.kind} {escape(a.box[:16])} {escape(a.instance)} {states} {escape(a.detail)}'
            self._emit(event, asdict(a), line)

    def _problems(self, event: str, errors: Sequence[str], unreachable: dict[str, str]) -> None:
        for line in errors:
            self._emit(f'{event}_error', {'error': line}, f'[yellow]{event}: {escape(line)}[/yellow]')
        for box_id, why in unreachable.items():
            self._emit(f'{event}_unreachable', {'hotkey': box_id, 'why': why}, f'[red]unreachable[/red] {escape(box_id[:16])}: {escape(why)}')  # fmt: skip

    def reconcile(self, report: ReconcileReport, n: int) -> None:
        self._actions('reconcile', report.actions)
        errors = [] if report.errors == self._last_errors else report.errors  # "N short" every 30 s is noise
        self._last_errors = list(report.errors)
        self._problems('reconcile', errors, report.unreachable)
        if report.launched:
            boxes = ', '.join(b[:16] for b in report.launched)
            self._emit('reconcile_launched', {'pass': n, 'boxes': report.launched}, f'[dim]reconcile {n}: working on {escape(boxes)}[/dim]')  # fmt: skip

    def background(self, report: ReconcileReport) -> None:
        self._actions('reconcile', report.actions)
        self._problems('reconcile', report.errors, report.unreachable)

    def watch(self, report: WatchReport) -> None:
        self._actions('watch', report.actions)
        self._problems('watch', [], report.unreachable)
        for row in report.usage:  # the lease accounting check: every number, for the operator's audit
            mark = '[red]✗[/red]' if row['kind'] in ('strike', 'detection') else '[dim]·[/dim]'
            line = f'{mark} usage_check {row["kind"]} {escape(row["box"][:16])} {escape(row["instance"])}'
            if 'surplus' in row:
                line += f' surplus {row["surplus"]:.0f} / threshold {row["threshold"]:.0f}'
            if row.get('detail'):
                line += f' {escape(str(row["detail"]))}'
            self._emit('usage_check', row, line)

    def discover(self, report: DiscoverReport) -> None:
        for a in report.actions:
            mark = '[green]✓[/green]' if a.ok else '[red]✗[/red]'
            self._emit('discover', asdict(a), f'{mark} discover {a.kind} {escape(a.hotkey[:16])} {escape(a.detail)}')
        ignored = [f'{hotkey}: {why}' for hotkey, why in sorted(report.ignored.items())]
        if ignored != self._last_ignored:  # a bad address is published every read; say it once
            for line in ignored:
                self._emit('discover_ignored', {'ignored': line}, f'[yellow]discover ignored {escape(line)}[/yellow]')
            self._last_ignored = ignored

    def note(self, loop: str, message: str) -> None:
        self._emit('note', {'loop': loop, 'message': message}, f'[yellow]{loop}:[/yellow] {escape(message)}')

    def error(self, loop: str, message: str) -> None:
        self._emit('error', {'loop': loop, 'message': message}, f'[red]{loop} error:[/red] {escape(message)}')


@controller_group.command('run')
@click.option(
    '--round-interval',
    type=float,
    default=cfg.FULL_CHECK_INTERVAL_S,
    show_default=True,
    help='Seconds between proof round starts.',
)
@click.option(
    '--build-cmd', default=None, help='Shell command run after every round (a fresh proof build); re-read next round.'
)
@click.option(
    '--reconcile-interval',
    type=float,
    default=cfg.RECONCILE_INTERVAL_S,
    show_default=True,
    help='Seconds between reconcile passes.',
)
@click.option(
    '--heartbeat-interval',
    type=float,
    default=cfg.HEARTBEAT_INTERVAL_S,
    show_default=True,
    help='Seconds between heartbeats of a box with a LEASED card.',
)
@click.option(
    '--pull-token-file',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help='Read-only registry token, one line "username:token"; installed for each pull and removed after.',
)
@_workload_bind_option
@click.option(
    '--scorecard-interval',
    type=float,
    default=cfg.SCORECARD_INTERVAL_S,
    show_default=True,
    help='Seconds between signed-scorecard writes; the scorecard is valid for two intervals.',
)
@click.option(
    '--price-source',
    type=click.Choice(cfg.PRICE_SOURCES),
    default=cfg.PRICE_SOURCE,
    show_default=True,
    help='Where prices come from: CoinGecko for TAO/USD + the chain pool for alpha/TAO (as phase 0 did), metagraphed, or the static values.',
)
@click.option(
    '--metagraphed-url',
    default=cfg.METAGRAPHED_URL,
    envvar='GT_METAGRAPHED_URL',
    help="metagraphed's REST base URL (with --price-source metagraphed).",
)
@click.option(
    '--static-tao-usd', type=float, default=cfg.STATIC_TAO_USD, show_default=True, help='Fallback USD per TAO.'
)
@click.option(
    '--static-alpha-tao', type=float, default=cfg.STATIC_ALPHA_TAO, show_default=True, help='Fallback TAO per alpha.'
)
@click.option(
    '--gateway-url',
    default='',
    help='The gateway (e.g. http://127.0.0.1:8791): a planned drain then waits until its /healthz shows no request '
    'in flight on the instance before the container is stopped, and the lease accounting check reads its totals. '
    'Unset: a short fixed grace instead, and no accounting check.',
)
@click.option(
    '--discover',
    is_flag=True,
    default=False,
    help='Also read the metagraph every --discover-interval and admit / move / remove boxes from it.',
)
@click.option(
    '--discover-interval',
    type=float,
    default=cfg.DISCOVER_INTERVAL_S,
    show_default=True,
    help='Seconds between metagraph reads (with --discover).',
)
@click.option('--max-seconds', type=float, default=0, hidden=True)
@_chain_options
@_registry_options
@_check_options
@_state_options
def run_command(
    round_interval,
    build_cmd,
    reconcile_interval,
    heartbeat_interval,
    pull_token_file,
    workload_bind,
    scorecard_interval,
    price_source,
    metagraphed_url,
    static_tao_usd,
    static_alpha_tao,
    gateway_url,
    discover,
    discover_interval,
    max_seconds,
    netuid,
    network,
    rpc_url,
    release_pubkey,
    allow_dev_keys,
    state_dir,
    json_mode,
    **opts,
):
    """The controller as one process: the proof round (every --round-interval on the wall clock, caught up at once
    after a sleep; --build-cmd after each round, retried every few seconds while it fails), the
    reconciler (every --reconcile-interval), the in-lease watch (heartbeat every --heartbeat-interval, manifest
    health probe every health.interval_s) with the pay ledger's settlement tick, the signed scorecard (every
    --scorecard-interval) and, with --discover, discovery (the metagraph every --discover-interval), each on its own
    thread over one state. A card that reaches CHECKING, or a box that enters ADMIT, is proved on its own box at the
    next watch tick, not at the next round; with --discover one metagraph read runs before round 1.

    \b
    It holds the state directory for its whole life: `check`, `round`, `reconcile` and `discover` refuse beside it
    (use `status`); `admit`, `deploy`, `release` and `check --force` on a BENCHED box keep working. SIGTERM finishes
    the visits in flight, writes state and exits 0. Takes every `round` flag (--proof, --proof-args,
    --agent-image-digest, ...), every `reconcile` flag and, for --discover, --network / --rpc-url / --netuid.
    """
    reader = _chain_reader(_resolve_endpoint(network, rpc_url), netuid) if discover else None
    setup = _setup(state_dir, **opts)
    setup.state.ensure()
    _require_ca_key(setup.ca_key, json_mode)
    registry = _open_registry(setup.state, release_pubkey, allow_dev_keys, json_mode)
    token = _read_pull_token(pull_token_file, json_mode)
    try:
        setup.proof()  # a provider that cannot load fails now, not 20 minutes in; every round re-loads it
    except ProofLoadError as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)
    try:
        rates = load_rates()
    except RatesError as e:
        _fail(f'pay rates: {e}', json_mode, EXIT_NO_VERDICT)
    static = StaticOracle(static_tao_usd, static_alpha_tao)
    if price_source == 'metagraphed':
        if not metagraphed_url:
            _fail('--price-source metagraphed needs --metagraphed-url', json_mode, EXIT_NO_VERDICT)
        inner: Any = MetagraphedOracle(metagraphed_url, netuid)
    elif price_source == 'coingecko+chain':
        inner = CoinGeckoChainOracle(_resolve_endpoint(network, rpc_url), netuid)
    else:
        inner = static
    oracle = FailSafeOracle(inner, static)
    printer = _DaemonPrinter(json_mode)
    try:
        with setup.state.run_lock():
            controller = Controller(
                setup.state,
                registry,
                make_runner=lambda box, purpose: _make_runner(setup.state, box, setup.ca_key, purpose),
                run_round=lambda proof, **shared: run_round(setup, proof, **shared),
                reprove=lambda proof, box_id, **shared: reprove_box(setup, proof, box_id, **shared),
                load_proof=setup.proof,
                build=_run_build,
                build_cmd=build_cmd,
                pull_token=token,
                workload_bind=workload_bind,
                gateway_state=gateway_healthz(gateway_url) if gateway_url else None,
                intervals=Intervals(
                    round_s=round_interval,
                    reconcile_s=reconcile_interval,
                    heartbeat_s=heartbeat_interval,
                    scorecard_s=scorecard_interval,
                    discover_s=discover_interval,
                ),
                reporter=printer,
                sleep=_sleep,
                oracle=oracle,
                rates=rates,
                read_chain=reader.read if reader is not None else None,
                scan_host_key=_scan_host_key,
                network=network,
                netuid=netuid,
            )
            discovering = (
                f', discover every {discover_interval:.0f} s ({reader.endpoint} netuid {reader.netuid})'
                if reader
                else ''
            )
            printer.note(
                'controller',
                f'running on {setup.state.root} (pid {os.getpid()}): round every {round_interval:.0f} s, reconcile '
                f'every {reconcile_interval:.0f} s, heartbeat every {heartbeat_interval:.0f} s, scorecard every '
                f'{scorecard_interval:.0f} s (prices: {price_source}){discovering}',
            )
            clean = controller.serve(max_seconds=max_seconds or None)
    except ControllerRunning as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)
    sys.exit(EXIT_ADMIT if clean else EXIT_NO_VERDICT)


def _heartbeat_cell(row: dict, now: float) -> str:
    if row.get('heartbeat_ok') is None:
        misses = row.get('heartbeat_misses') or 0
        return '[dim]—[/dim]' + (f' [yellow]{misses} missed[/yellow]' if misses else '')
    mark = '[green]ok[/green]' if row['heartbeat_ok'] else '[red]FAIL[/red]'
    return f'{mark} {_age(row.get("last_heartbeat_at"), now)}'


def _pay_line(view: dict, now: float) -> str:
    if not view:
        return '[dim]pay: no scorecard yet[/dim]'
    doc = view.get('scorecard') or {}
    mark = '[green]valid[/green]' if view['valid'] else f'[red]refused[/red] ({escape(view["error"][:120])})'
    implied = (doc.get('pool') or {}).get('implied_usd_per_card_hour') or {}
    rates = ' · '.join(
        f'{escape(g)} ${v["idle"]:.3f} idle / ${v["leased"]:.3f} leased per card-hour ({v["cards"]:.1f} cards)'
        for g, v in implied.items()
    )
    oracle = doc.get('oracle') or {}
    return (
        f'pay: scorecard {(view.get("sha256") or "?")[:12]} {mark}, issued {_age(doc.get("issued_at"), now)} ago'
        f' · {rates or "no accruing cards"} · recycle {float(doc.get("recycle_share", 1.0)) * 100:.1f}%'
        f' · TAO ${float(oracle.get("tao_usd", 0)):.2f}, alpha {float(oracle.get("alpha_tao", 0)):.6f} TAO'
        + (' [yellow](price held)[/yellow]' if oracle.get('held') else '')
    )


@controller_group.command('status')
@_state_options
def status_command(state_dir, json_mode):
    """The controller as its state files show it: running or not, the last round / reconcile / watch, every box with
    its cards (state and age) and standing, its pay (the last scorecard's window plus what the ledger has settled
    since it, so a card leased after the scorecard shows its seconds and USD now, labelled with the scorecard's age),
    every instance with its heartbeat and health. Read-only; safe beside `run`."""
    state = StateDir(Path(state_dir).expanduser())
    now = time.time()
    running = state.root.is_dir() and state.daemon_running()
    status_path = state.root / STATUS_FILE
    try:
        info = json.loads(status_path.read_text()) if status_path.exists() else {}
    except (OSError, ValueError):
        info = {}
    pay_view = scorecard_view(state.root, now)
    paid = {h['hotkey']: h for h in (pay_view.get('scorecard') or {}).get('hotkeys', [])}
    issued_at = (pay_view.get('scorecard') or {}).get('issued_at')
    age_s = round(now - float(issued_at), 1) if issued_at is not None else None
    store = state.store()
    since_scorecard = live_pay(state.root, store.boxes, pay_view, now)
    boxes = []
    for box in sorted(store.boxes.values(), key=lambda b: b.box_id):
        cards = [
            {'uuid': u, 'state': c.state, 'since': c.since, 'instance': c.instance_id}
            for u, c in sorted(box.cards.items())
        ]
        entry = paid.get(box.box_id) or {}
        live = since_scorecard.get(box.box_id)
        boxes.append(
            {
                'hotkey': box.box_id,
                'uid': box.uid,
                'host': _host_field(box.host, box.port),
                'status': box.status,
                'standing': standing(box.standing_events, now),
                'cards': cards,
                'last_check_at': box.last_check_at,
                'last_failed': box.last_failed,
                'bench_until': box.bench_until,
                'withheld_from': box.withheld_from,
                'release_requested': release_requested(box),
                'remove_requested': remove_requested(box),
                'source': box.source,
                'endpoint_changed': box.endpoint_changed,
                'standing_events': box.standing_events[-5:],
                'pay': pay_entry(entry, live, age_s) if entry or live else {},
            }
        )
    instances = _instance_rows(InstanceStore(state.instances), store)
    if json_mode:
        pay = {k: v for k, v in pay_view.items() if k != 'scorecard'}
        if pay_view.get('scorecard'):
            doc = pay_view['scorecard']
            pay.update({k: doc.get(k) for k in ('issued_at', 'valid_until', 'window', 'oracle', 'recycle_share')})
            pay['implied_usd_per_card_hour'] = (doc.get('pool') or {}).get('implied_usd_per_card_hour')
        emit_json(
            {
                'success': True,
                'running': running,
                'controller': info,
                'pay': pay,
                'boxes': boxes,
                'instances': instances,
            }
        )
        return
    last_round, last_reconcile, last_watch = (
        info.get('round') or {},
        info.get('reconcile') or {},
        info.get('watch') or {},
    )
    head = '[green]running[/green]' if running else '[yellow]not running[/yellow]'
    if info.get('pid'):
        head += f' · pid {info["pid"]} · started {_when(info.get("started_at"))}'
    console.print(head)
    console.print(
        f'last round {last_round.get("n", "—")} ({_age(last_round.get("finished_at"), now)} ago, exit '
        f'{last_round.get("exit_code", "—")}) · last reconcile {last_reconcile.get("n", "—")} '
        f'({_age(last_reconcile.get("at"), now)} ago) · last watch visit {_age(last_watch.get("at"), now)} ago'
    )
    console.print(_pay_line(pay_view, now))
    table = Table(title='boxes', show_header=True)
    pay_head = (
        f'Pay (scorecard {_age(issued_at, now)} ago + since)' if issued_at is not None else 'Pay (ledger, last hour)'
    )
    for column in ('Hotkey', 'Host', 'Status', 'Standing', 'Cards', pay_head, 'Last check', 'Bench / withheld', 'Last event'):  # fmt: skip
        table.add_column(column, no_wrap=column not in ('Cards', 'Last event'))
    for b in boxes:
        cards = '\n'.join(
            f'{c["uuid"][:12]}… {c["state"]} {_age(c["since"], now)}' + (f' {c["instance"]}' if c['instance'] else '')
            for c in b['cards']
        )
        bench = f'until {_when(b["bench_until"])}' if b['status'] == BENCHED else ''
        if b['release_requested']:
            bench += ' · release requested'
        if b['remove_requested']:
            bench += f'{" · " if bench else ""}removal requested'
        if b['withheld_from']:
            bench += f'{" · " if bench else ""}pay withheld from {_when(b["withheld_from"])}'
        event = b['standing_events'][-1] if b['standing_events'] else None
        pay = b['pay']
        if pay:
            total, live = pay['total'], pay['live']
            pay_cell = (
                f'${total["usd"]:.3f} · idle {total["idle_s"] / 3600:.2f} h · leased {total["leased_s"] / 3600:.2f} h'
                + (f' · [red]withheld {total["withheld_s"] / 3600:.2f} h[/red]' if total['withheld_s'] else '')
                + (
                    f' [dim](since: leased {live["leased_s"]:.0f} s, idle {live["idle_s"]:.0f} s, ${live["usd"]:.3f})[/dim]'
                    if live
                    else ''
                )
            )
        else:
            pay_cell = '[dim]—[/dim]'
        table.add_row(
            escape(b['hotkey'][:16]),
            escape(b['host']),
            b['status']
            + (f' ({", ".join(b["last_failed"])})' if b['status'] == BENCHED and b['last_failed'] else '')
            + (
                f' [red]endpoint changed → {b["endpoint_changed"].get("host")}:{b["endpoint_changed"].get("port")}[/red]'
                if b['endpoint_changed']
                else ''
            ),
            b['standing'],
            escape(cards) or '[dim]—[/dim]',
            pay_cell,
            f'{_age(b["last_check_at"], now)} ago' if b['last_check_at'] else '—',
            escape(bench),
            escape(f'{event["kind"]} {_age(event["at"], now)} ago') if event else '',
        )
    console.print(table)
    if not instances:
        err_console.print('[dim]no instances[/dim]')
        return
    table = Table(title='instances', show_header=True)
    for column in ('Instance', 'Entry', 'Box', 'Card', 'State', 'Container', 'Healthy', 'Heartbeat', 'Health'):
        table.add_column(column, no_wrap=True)
    for r in instances:
        health = (
            '[green]ok[/green]' if r.get('health_ok') else ('[red]fail[/red]' if r.get('health_ok') is False else '—')
        )
        if r.get('health_failures'):
            health += f' {r["health_failures"]}×'
        table.add_row(
            escape(r['id']),
            escape(r['entry']),
            escape(r['box'][:16]),
            escape(f'{r["uuid"][:12]}…'),
            r['card_state'] + (' (draining)' if r['draining'] else ''),
            escape(r['container_id'][:12]),
            '[green]yes[/green]' if r['healthy'] else '[red]no[/red]',
            _heartbeat_cell(r, now),
            health + f' {_age(r.get("last_health_at"), now)}',
        )
    console.print(table)


@controller_group.command('publish')
@_registry_options
@_state_options
def publish_command(release_pubkey, allow_dev_keys, state_dir, json_mode):
    """Write ``public/fleet.json`` once: the sanitized fleet document the website shows (no address, port, container
    or image id, raw GPU UUID or error text; see publish.py). `run` writes it on its own; this is for a look at the
    document or a host where `run` is down. Read-only on every other state file; safe beside `run`."""
    state = StateDir(Path(state_dir).expanduser())
    if not state.root.is_dir():
        _fail(f'{state.root}: no controller state directory', json_mode, EXIT_NO_VERDICT)
    registry = _open_registry(state, release_pubkey, allow_dev_keys, json_mode)

    def image_of(entry_id: str) -> str | None:
        try:
            return registry.read(entry_id).entry.image
        except RegistryError:
            return None

    status_path = state.root / STATUS_FILE
    try:
        info = json.loads(status_path.read_text()) if status_path.exists() else {}
    except (OSError, ValueError):
        info = {}
    doc = build_fleet(
        state.root,
        state.store().boxes,
        InstanceStore(state.instances).instances,
        info,
        state.daemon_running(),
        time.time(),
        image_of,
    )
    path = write_fleet(state.root, doc)
    if json_mode:
        emit_json({'success': True, 'path': str(path), 'fleet': doc})
        return
    totals = doc['totals']
    states = ', '.join(f'{n} {s}' for s, n in sorted(totals['cards_by_state'].items())) or 'no cards'
    console.print(f'wrote {escape(str(path))}: {totals["boxes"]} box(es), {totals["cards"]} card(s) ({states})')


@controller_group.command('scorecard')
@_state_options
def scorecard_command(state_dir, json_mode):
    """The last signed scorecard the controller wrote (``scorecard/latest.json``), checked the way the validator checks
    it: the sha256 beside it, the schema, valid_until, and weights + recycle_share summing to one pool. Exit 0 when a
    validator would use it, 1 when it would refuse it (the compute share recycles), 2 when there is none."""
    state = StateDir(Path(state_dir).expanduser())
    now = time.time()
    view = scorecard_view(state.root, now)
    if not view:
        _fail(
            f'no scorecard in {state.root / "scorecard"} yet: `gitt controller run` writes one',
            json_mode,
            EXIT_NO_VERDICT,
        )
    code = EXIT_ADMIT if view['valid'] else EXIT_BENCH
    if json_mode:
        emit_json({'success': view['valid'], **view})
        sys.exit(code)
    console.print(_pay_line(view, now))
    doc = view.get('scorecard') or {}
    window = doc.get('window') or {}
    console.print(
        f'window {_when(window.get("start"))} → {_when(window.get("end"))} · valid until {_when(doc.get("valid_until"))}'
        f' · pool {float((doc.get("pool") or {}).get("alpha", 0)):.2f} alpha = ${float((doc.get("pool") or {}).get("usd", 0)):.2f}'
        f', paid ${float((doc.get("pool") or {}).get("paid_usd", 0)):.2f} · {escape(view["path"])}'
    )
    table = Table(title='hotkeys', show_header=True)
    for column in ('Hotkey', 'Status', 'Standing', 'Weight', 'USD', 'Idle h', 'Leased h', 'Withheld h', 'Cards'):
        table.add_column(column, no_wrap=column != 'Cards')
    for h in doc.get('hotkeys', []):
        table.add_row(
            escape(h['hotkey'][:16]),
            h.get('status', ''),
            h.get('standing', ''),
            f'{h["weight"] * 100:.4f}%',
            f'${h.get("usd", 0):.3f}',
            f'{h["idle_s"] / 3600:.2f}',
            f'{h["leased_s"] / 3600:.2f}',
            f'[red]{h["withheld_s"] / 3600:.2f}[/red]' if h.get('withheld') else '0',
            escape(' '.join(f'{c["uuid_hash"][:10]}…:{c["state"]}' for c in h.get('cards', []))) or '[dim]—[/dim]',
        )
    console.print(table)
    sys.exit(code)


# ---------------------------------------------------------------- discovery: the chain is the registry --------------


def _discover_payload(report: DiscoverReport) -> dict:
    return {
        'registered': report.registered,
        'compute': report.compute,
        'actions': [asdict(a) for a in report.actions],
        'ignored': report.ignored,
    }


@controller_group.command('discover')
@_chain_options
@_state_options
def discover_command(netuid, network, rpc_url, state_dir, json_mode):
    """Read the metagraph once and settle the boxes against it: every registered hotkey serving a compute endpoint
    (what `gitt up` publishes) is scanned, pinned and created at ADMIT; a box whose endpoint changed to another host
    key is flagged, not re-pinned; a deregistered box is benched, drained by the reconciler and then removed.

    \b
    Read-only on chain: the controller holds no chain key. Boxes an operator admitted are left alone.
    Exit 0 settled, 1 a scan failed / an endpoint changed / an address conflict, 2 the metagraph read failed.
    `gitt controller run --discover` does this every --discover-interval.
    """
    state = StateDir(Path(state_dir).expanduser()).ensure()
    endpoint = _resolve_endpoint(network, rpc_url)
    with _one_shot_lock(state, json_mode):
        try:
            endpoints = _chain_reader(endpoint, netuid).read()
        except Exception as e:
            _fail(f'metagraph read failed ({endpoint}, netuid {netuid}): {type(e).__name__}: {e}'[:400], json_mode, EXIT_NO_VERDICT)  # fmt: skip
        discovery = Discovery(state.store(), InstanceStore(state.instances), state.known_hosts, _scan_host_key)
        report = discovery.run_pass(endpoints)
    if json_mode:
        emit_json({'success': report.ok, 'network': endpoint, 'netuid': netuid, **_discover_payload(report)})
    else:
        console.print(
            f'[dim]{escape(endpoint)} netuid {netuid}: {report.registered} registered, '
            f'{report.compute} compute endpoint(s)[/dim]'
        )
        if report.actions:
            table = Table(title='gitt controller discover', show_header=True)
            for column in ('Action', 'Hotkey', 'Detail'):
                table.add_column(column, no_wrap=column != 'Detail')
            for a in report.actions:
                mark = '[green]✓[/green]' if a.ok else '[red]✗[/red]'
                table.add_row(f'{mark} {a.kind}', escape(a.hotkey), escape(a.detail))
            console.print(table)
        else:
            console.print('[dim]nothing to change[/dim]')
        for hotkey, why in report.ignored.items():
            err_console.print(f'[yellow]ignored[/yellow] {escape(hotkey)}: {escape(why)}')
    sys.exit(EXIT_ADMIT if report.ok else EXIT_BENCH)


def register_controller_commands(cli):
    """Register `gitt controller` with the root CLI group."""
    cli.add_command(controller_group, name='controller')
