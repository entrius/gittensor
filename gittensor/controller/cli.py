# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``gitt controller``: the operator's entry point to the compute-pool controller (vault ``24`` §3, ``26`` §3, §5).

Everything here drives library code that already exists — the SSH certificate transport (``controller.ssh``), the
full check and box state (``controller.checks``), the GPU-proof slot (``controller.proof``):

    gitt controller admit <hotkey> --host <ip> --port <port>   pin the box's host key, create it at ADMIT
    gitt controller allowlist add <hotkey> | show              curate the NVML allowlist from a known-good box
    gitt controller check <hotkey>                             one full check: verdict, new state, exit 0 / 1 / 2
    gitt controller round [--loop]                             the 20-min two-phase probe over every idle box

State lives in one directory (``--state-dir``, default ``~/.gittensor/controller``): ``boxes.json`` (the
``StateStore``), ``known_hosts`` (host keys pinned at admit) and ``nvml_allowlist.json``; the CA private key defaults
to ``gt_ca`` beside them. The GPU proof is chosen by config, never by code: ``--proof module:Class`` plus
``--proof-args key=value`` kwargs. Without one the fail-closed ``UnconfiguredProof`` benches every box with the reason
named. No inbound endpoints and no chain access: outbound SSH and local files only (``26`` §3).
"""

from __future__ import annotations

import base64
import importlib
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import click
from rich.markup import escape
from rich.table import Table

from gittensor.agent.config import AGENT_SSH_PORT
from gittensor.cli.help import StyledGroup
from gittensor.cli.helpers import console, err_console
from gittensor.cli.json_output import emit_error_json, emit_json
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
    HostScrape,
    nvidia_smi_command,
    parse_kernel_driver,
    parse_md5,
    parse_nvidia_smi,
)
from gittensor.controller.checks.state import (
    ADMIT,
    BENCHED,
    IDLE,
    BoxState,
    StateStore,
    apply_verdict,
    release_from_bench,
)
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.proof.slot import (
    GpuProof,
    ProbeResult,
    ProofUnavailable,
    StagedProof,
    UnconfiguredProof,
    fire_box,
    image_ref,
    stage_box,
)
from gittensor.controller.ssh import CertificateAuthority, SshRunner, SshTransportError, known_hosts_line, scan_host_key
from gittensor.controller.ssh.certs import CertificateError

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

    def ensure(self) -> StateDir:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self

    def store(self) -> StateStore:
        return StateStore(self.boxes)


def _host_field(host: str, port: int) -> str:
    return f'[{host}]:{port}'


def pinned_host_key(known_hosts: Path, host: str, port: int) -> str:
    """The key ``known_hosts`` pins for ``[host]:port``, or ''."""
    if not known_hosts.exists():
        return ''
    for line in known_hosts.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == _host_field(host, port):
            return f'{parts[1]} {parts[2]}'
    return ''


def write_host_key(known_hosts: Path, host: str, port: int, host_key: str | None) -> None:
    """Replace the ``[host]:port`` entry with ``host_key`` (or drop it when None)."""
    lines = known_hosts.read_text().splitlines() if known_hosts.exists() else []
    kept = [line for line in lines if line.strip() and line.split()[0] != _host_field(host, port)]
    text = ''.join(f'{line}\n' for line in kept)
    if host_key:
        text += known_hosts_line(host, port, host_key)
    known_hosts.write_text(text)


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


def check_box(
    runner: HostRunner,
    box: BoxState,
    fleet_uuids: dict[str, Iterable[str]],
    proof: GpuProof,
    allowlist: NvmlAllowlist,
    config: FullCheckConfig,
    now: float,
) -> CheckOutcome:
    """``run_full_check`` with a transport gate: a box SSH cannot reach gets no verdict instead of a BENCH."""
    try:
        runner.run(PREFLIGHT_COMMAND, timeout=config.ssh_timeout_s)
    except (SshTransportError, CertificateError) as e:
        return CheckOutcome(None, f'{type(e).__name__}: {e}'[:500])
    scrape = scrape_box(runner, config)
    lost = transport_failure(scrape)
    if lost:
        return CheckOutcome(None, lost)
    checks = judge_identity(scrape, allowlist, box.pinned_uuids or None, config, box.box_id, fleet_uuids)
    if identity_passed(checks):
        checks.append(ck.check_gpu_proof(runner, scrape.gpus, proof, config.proof_image, config.proof_timeout_s))
    else:
        checks.append(proof_skipped(checks))
    return CheckOutcome(finish_verdict(checks, scrape, now))


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
    cards: list[dict] = field(default_factory=list)
    fired_at: float | None = None
    transport_error: str = ''
    verdict: CheckVerdict | None = None
    after: BoxState | None = None


@dataclass
class RoundReport:
    provider: str
    boxes: list[BoxRound]
    not_probed: list[BoxState]
    timings_ms: dict[str, float]

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


def run_round(setup: CheckSetup, proof: GpuProof, clock: Callable[[], float] = time.monotonic) -> RoundReport:
    """One probe cycle over every ADMIT / IDLE box (``23`` §3b). Benches that have expired are released first.

    Phase 1: connect and scrape every box in parallel; judge identity with fleet-wide UUID uniqueness over every pin
    and every card reported this round; stage the proof on every box that passed, in parallel. Phase 2: one start
    signal — every staged box fires at once (a thread per box, cards parallel inside ``fire_box``). Then clean up,
    judge, ``apply_verdict``. A box lost to SSH gets no verdict and keeps its state."""
    store = setup.state.store()
    now = time.time()
    rows: list[BoxRound] = []
    not_probed: list[BoxState] = []
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
        try:
            r.staged = stage_box(r.runner, r.scrape.gpus, proof, config.proof_image, config.proof_timeout_s)
        except ProofUnavailable as e:
            r.stage_error = str(e)[:300]
        except Exception as e:  # transport died mid-stage
            r.stage_error = f'staging failed: {type(e).__name__}: {e}'[:300]

    def cleanup(r: BoxRound) -> None:
        command = proof.cleanup_command(r.staged)
        if command:
            try:
                r.runner.run(command, timeout=cfg.SSH_COMMAND_TIMEOUT_S)
            except Exception:  # best effort; the containers are labelled for a sweep
                pass

    armed: list[BoxRound] = []
    try:
        _each(rows, connect_and_scrape)
        marks['scraped'] = clock()
        fleet: dict[str, set[str]] = {box_id: set(b.pinned_uuids) for box_id, b in store.boxes.items()}
        for r in rows:
            if r.scrape is not None:
                fleet.setdefault(r.box.box_id, set()).update(r.scrape.uuids)
        for r in rows:
            if r.scrape is not None:
                r.checks = judge_identity(r.scrape, allowlist, r.box.pinned_uuids or None, config, r.box.box_id, fleet)
        _each([r for r in rows if r.scrape is not None and identity_passed(r.checks)], stage)
        marks['staged'] = clock()
        armed = [r for r in rows if r.staged is not None]
        if armed:
            gate = threading.Barrier(len(armed))

            def fire(r: BoxRound) -> None:
                gate.wait()
                r.fired_at = clock()
                try:
                    r.cards = fire_box(r.runner, r.scrape.gpus, proof, r.staged, config.proof_timeout_s, clock)
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
    for r in rows:
        if r.scrape is None:
            continue
        if not identity_passed(r.checks):
            r.checks.append(proof_skipped(r.checks))
        elif r.stage_error:
            r.checks.append(ck.proof_result(ProbeResult(provider, cards=r.cards, error=r.stage_error)))
        else:
            r.checks.append(ck.proof_result(ProbeResult(provider, cards=r.cards)))
        r.verdict = finish_verdict(r.checks, r.scrape, now)
        r.after = store.boxes[r.box.box_id] = apply_verdict(r.box, r.verdict, now)
    store.save()

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
    return RoundReport(provider, rows, not_probed, {k: v for k, v in timings.items() if v is not None})


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
        round      The two-phase probe over every idle box (--loop: every 20 min)
    """


@controller_group.command('admit')
@click.argument('hotkey')
@click.option('--host', required=True, help="The box's address.")
@click.option('--port', type=int, default=AGENT_SSH_PORT, show_default=True, help="The agent sshd's port.")
@click.option(
    '--force-rekey', is_flag=True, default=False, help='Re-pin a host key that changed (verify the box first).'
)
@_state_options
def admit_command(hotkey, host, port, force_rekey, state_dir, json_mode):
    """Pin a box's SSH host key (trust on first use, once) and create it at ADMIT.

    A changed host key is refused unless --force-rekey. Re-admitting keeps the box's status, pin and bench.
    """
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
    box = BoxState(hotkey) if new else BoxState.from_dict(box.as_dict())
    if box.host and (box.host, box.port) != (host, port):
        write_host_key(state.known_hosts, box.host, box.port, None)  # the box moved: drop its old address
    box.host, box.port, box.host_key = host, port, key
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
@_check_options
@_state_options
def check_command(hotkey, state_dir, json_mode, **opts):
    """One full check of one admitted box: scrape, judge, the GPU proof on every card, then apply the verdict.

    \b
    Exit 0 ADMIT, 1 BENCH, 2 no verdict (transport failure; state unchanged).
    Example (dev box, sealed proof):
        gitt controller check 5F... --agent-image-id sha256:... \\
            --proof gittensor_proof.provider:SealedProof \\
            --proof-args secret_store=secrets/secret_store.json \\
            --proof-args version=@dist/gt_proof.version --proof-args binary_path=dist/gt_proof
    """
    setup = _setup(state_dir, **opts)
    store = setup.state.store()
    box = _admitted_box(store, hotkey, json_mode)
    _require_ca_key(setup.ca_key, json_mode)
    now = time.time()
    released = release_from_bench(box, now)
    if released.status == BENCHED:
        _fail(
            f'{hotkey} is BENCHED until {_when(box.bench_until)} ({", ".join(box.last_failed) or "?"}); not checked',
            json_mode,
            EXIT_BENCH,
        )
    try:
        proof = setup.proof()
    except ProofLoadError as e:
        _fail(str(e), json_mode, EXIT_NO_VERDICT)
    fleet = {b.box_id: list(b.pinned_uuids) for b in store.boxes.values() if b.box_id != hotkey}
    runner = TimedRunner(_make_runner(setup.state, released, setup.ca_key, 'check'))
    try:
        outcome = check_box(runner, released, fleet, proof, setup.allowlist(), setup.config, now)
    finally:
        runner.close()
    timings = phase_timings(runner.log)
    if outcome.verdict is None:
        _fail(
            f'no verdict, state unchanged — transport failure: {outcome.transport_error}',
            json_mode,
            EXIT_NO_VERDICT,
            hotkey=hotkey,
            timings_ms=timings,
        )
    verdict = outcome.verdict
    after = apply_verdict(released, verdict, now)
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


def _print_round(report: RoundReport, n: int, json_mode: bool) -> None:
    if json_mode:
        emit_json(
            {
                'success': report.exit_code == EXIT_ADMIT,
                'round': n,
                'provider': report.provider,
                'timings_ms': report.timings_ms,
                'boxes': [
                    {
                        'hotkey': r.box.box_id,
                        'host': _host_field(r.box.host, r.box.port),
                        'status': {'before': r.status_before, 'after': (r.after or r.box).status},
                        'transport_error': r.transport_error,
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
    for column in ('Hotkey', 'Host', 'State', 'Verdict', 'Proof ms', 'Failed / reason'):
        table.add_column(column, no_wrap=column != 'Failed / reason')
    for r in report.boxes:
        proof_ms = [c.get('elapsed_ms') for c in r.cards if c.get('elapsed_ms') is not None]
        if r.verdict is None:
            reason = r.transport_error
        else:
            reason = '; '.join(
                f'{c.name}: {check_detail(c)}' for c in r.verdict.checks if not c.passed and not c.skipped
            )
        table.add_row(
            escape(r.box.box_id),
            escape(_host_field(r.box.host, r.box.port)),
            f'{r.status_before} → {(r.after or r.box).status}',
            _verdict_markup(r.verdict),
            f'{max(proof_ms):.0f}' if proof_ms else '',
            escape(reason),
        )
    for b in report.not_probed:
        table.add_row(escape(b.box_id), escape(_host_field(b.host, b.port)), b.status, '[dim]not probed[/dim]', '', '')
    console.print(table)
    console.print(f'[dim]{_timings_text(report.timings_ms) or "no boxes to probe"}[/dim]')


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


def register_controller_commands(cli):
    """Register `gitt controller` with the root CLI group."""
    cli.add_command(controller_group, name='controller')
