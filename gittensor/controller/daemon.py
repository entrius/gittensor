# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``gitt controller run``: the controller as one process (vault ``24`` §3 WS-D, ``26`` §3).

Three loops, each on its own thread, over one in-memory state (``boxes.json`` + ``instances.json``) that every write
changes under one short lock and saves at once:

* **the proof round** every ``FULL_CHECK_INTERVAL_S`` (20 min) on the wall clock, checked every ``ROUND_WAKE_S`` so a
  controller that slept runs the round it missed on waking (one catch-up, then the cadence resumes); the proof build
  (``--build-cmd``) after every round, retried on every wake while it fails (the previous binary stays in use). A
  round that fails before any box answered SSH (the provider missing, the controller's own link down) is logged once
  as an error and counts no box unreachable;
* **the reconciler** every ``RECONCILE_INTERVAL_S`` (30 s); its starts and drains run on their own threads holding
  their box's lock, so a pass never waits for a model load;
* **the watch** every ``WATCH_TICK_S``: the generic heartbeat (``HEARTBEAT_INTERVAL_S``) and the manifest health probe
  wherever one is due; on the same tick, **the re-prove**: an IDLE box with a card in CHECKING (a drain done, a
  failed start, a health replacement) gets the proof on that box only, for its CHECKING cards, on a thread of its own,
  so a replacement can start within a minute instead of waiting up to 20 (Kimbo 9/15); a box newly at ADMIT
  (discovery, or `gitt controller admit` beside us) gets its first proof the same way, at once, instead of at the
  next fleet round (Kimbo 9/16). The fleet-wide round is unchanged. A benched box has no cards, so nothing benched is
  re-proved: it waits out the bench. Then the pay ledger's settlement tick (``pay/ledger.py``) whenever one is due;
* **the scorecard** every ``SCORECARD_INTERVAL_S``: the trailing window settled at the oracle's price and written as
  ``scorecard/latest.json`` + ``latest.sha256`` for the validator (``pay/scorecard.py``). With it, and every
  ``PUBLISH_INTERVAL_S`` from the watch tick, the sanitized ``public/fleet.json`` the website shows (``publish.py``).
* **discovery** (``--discover``) every ``DISCOVER_INTERVAL_S``: the metagraph, read-only, settles which boxes exist
  (``discovery.py``). One pass runs before round 1, so a box already published on chain is in round 1 and does not
  wait a discovery interval at ADMIT; a box found later is proved at the next watch tick.

No lock is held across a model load: a start holds only its own box, the proof round skips a box whose lock stays held
(its cards are STARTING anyway) and proves it next round, and the watch takes no box lock at all (``locks.py``). On
SIGTERM the loops finish the visit in flight, state is written, and the process exits; a start still loading is left
to the next process, which finds its labelled container (``reconcile.py``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import HostRunner
from gittensor.controller.checks.state import ADMIT, CHECKING, IDLE, BoxState, StateStore
from gittensor.controller.discovery import ChainEndpoint, DiscoverReport, Discovery
from gittensor.controller.heartbeat import Watch, WatchReport
from gittensor.controller.locks import BoxLocks
from gittensor.controller.pay.ledger import Ledger, settle_window
from gittensor.controller.pay.oracle import FailSafeOracle, StaticOracle
from gittensor.controller.pay.rates import GpuRate, load_rates
from gittensor.controller.pay.scorecard import build_scorecard, write_scorecard
from gittensor.controller.publish import Publisher, build_fleet
from gittensor.controller.reconcile import InstanceStore, Reconciler, ReconcileReport
from gittensor.controller.registry import DeploymentStore, Registry
from gittensor.controller.runspec import BoxHttp, HttpClient, PullToken

STATUS_FILE = 'controller.json'


def _no_scan(host: str, port: int) -> str:
    raise RuntimeError('no host-key scanner configured')


class Reporter(Protocol):
    def round(self, report: Any, n: int) -> None: ...
    def error(self, loop: str, message: str) -> None: ...
    def reconcile(self, report: ReconcileReport, n: int) -> None: ...
    def background(self, report: ReconcileReport) -> None: ...
    def watch(self, report: WatchReport) -> None: ...
    def reprove(self, report: Any) -> None: ...
    def discover(self, report: DiscoverReport) -> None: ...
    def note(self, loop: str, message: str) -> None: ...


class SilentReporter:
    def round(self, report, n):
        pass

    def reprove(self, report):
        pass

    def reconcile(self, report, n):
        pass

    def background(self, report):
        pass

    def watch(self, report):
        pass

    def discover(self, report):
        pass

    def note(self, loop, message):
        pass

    def error(self, loop, message):
        pass


@dataclass
class Intervals:
    round_s: float = cfg.FULL_CHECK_INTERVAL_S
    reconcile_s: float = cfg.RECONCILE_INTERVAL_S
    heartbeat_s: float = cfg.HEARTBEAT_INTERVAL_S
    watch_tick_s: float = cfg.WATCH_TICK_S
    scorecard_s: float = cfg.SCORECARD_INTERVAL_S
    discover_s: float = cfg.DISCOVER_INTERVAL_S


class Controller:
    """``state`` names the files (``StateDir``). ``run_round(proof, store=, write_lock=, box_locks=)`` is the fleet
    round, ``reprove(proof, box_id, store=, write_lock=, box_locks=)`` the one-box re-prove (both return a round report)
    and ``load_proof()`` builds the provider, all from the CLI; ``make_runner(box, purpose)`` opens a visit."""

    def __init__(
        self,
        state: Any,
        registry: Registry,
        make_runner: Callable[[BoxState, str], HostRunner],
        run_round: Callable[..., Any],
        load_proof: Callable[[], Any],
        *,
        build: Callable[[str], subprocess.CompletedProcess] | None = None,
        build_cmd: str | None = None,
        pull_token: PullToken | None = None,
        intervals: Intervals | None = None,
        reporter: Reporter | None = None,
        http_for: Callable[[HostRunner, BoxState], HttpClient] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        reprove: Callable[..., Any] | None = None,
        oracle: FailSafeOracle | None = None,
        rates: dict[str, GpuRate] | None = None,
        read_chain: Callable[[], list[ChainEndpoint]] | None = None,
        scan_host_key: Callable[[str, int], str] | None = None,
        wall: Callable[[], float] = time.time,
        network: str | None = None,
        netuid: int | None = None,
    ):
        self.state = state
        self.registry = registry
        self.network, self.netuid = network, netuid
        self.publisher = Publisher(state.root)
        self._images: dict[str, str | None] = {}  # entry id -> image reference, for the public document
        self.wall = wall
        self.oracle = oracle or FailSafeOracle(StaticOracle())
        self.rates = rates if rates is not None else load_rates()
        self.ledger = Ledger(state.root / 'ledger')
        self.read_chain = read_chain  # None: no discovery loop (boxes come from `gitt controller admit` only)
        self.intervals = intervals or Intervals()
        self.reporter = reporter or SilentReporter()
        self.write_lock = threading.RLock()
        self.box_locks = BoxLocks()
        self.boxes = StateStore(state.boxes)
        self.instances = InstanceStore(state.instances)
        self.stop = threading.Event()
        self._run_round, self._load_proof = run_round, load_proof
        self._reprove = reprove
        self._reproving: dict[str, threading.Thread] = {}
        self._reprove_retry_at: dict[str, float] = {}  # box -> not before: its last re-prove got no verdict
        self._build, self.build_cmd = build, build_cmd
        self._build_failed = False  # the last --build-cmd exited non-zero: retried on every wake
        self._last_round_at: float | None = None  # wall clock; the round schedule runs from it
        self.proof = None
        self.counts = {'round': 0, 'reconcile': 0}
        self.status: dict[str, Any] = {
            'pid': os.getpid(),
            'started_at': time.time(),
            'stopped_at': None,
            'intervals': asdict(self.intervals),
            'round': {},
            'reconcile': {},
            'watch': {},
            'pay': {},
        }
        http_for = http_for or (lambda runner, box: BoxHttp(runner))
        self.reconciler = Reconciler(
            boxes=self.boxes,
            instances=self.instances,
            deployments=DeploymentStore(state.deployments),
            registry=registry,
            make_runner=lambda box: make_runner(box, 'reconcile'),
            http_for=http_for,
            pull_token=pull_token,
            sleep=sleep,
            box_locks=self.box_locks,
            background=True,
            on_background=self._background_done,
            _lock=self.write_lock,
        )
        self.watch = Watch(
            self.boxes,
            self.instances,
            registry,
            lambda box: make_runner(box, 'watch'),
            http_for,
            self.intervals.heartbeat_s,
            lock=self.write_lock,
            box_locks=self.box_locks,
        )
        self.discovery = Discovery(
            self.boxes, self.instances, state.known_hosts, scan_host_key or _no_scan, lock=self.write_lock
        )
        self._threads: list[threading.Thread] = []

    # -- one pass of each loop (tests call these directly) ---------------------------------------------------------

    def round_tick(self, now: float | None = None) -> bool:
        """The round loop's wake, every ``ROUND_WAKE_S``: run the round when it is due on the wall clock (the first at
        once; then ``round_s`` after the last), catching up at once after a sleep, and retry a failed build. True when a
        round ran."""
        now = self.wall() if now is None else now
        last = self._last_round_at
        if last is not None and now - last < self.intervals.round_s:
            if self._build_failed:
                self.build_once()
            return False
        if last is not None and now - last > self.intervals.round_s * cfg.ROUND_CATCH_UP_FACTOR:
            self.reporter.note(
                'round', f'catching up: the last round was {(now - last) / 60:.0f} min ago (interval {self.intervals.round_s / 60:.0f} min)'
            )  # fmt: skip
        self._last_round_at = now
        self.round_once()
        self.build_once()
        return True

    def round_once(self) -> Any:
        self.counts['round'] += 1
        n = self.counts['round']
        try:
            self.proof = self._load_proof()
        except Exception as e:
            if self.proof is None:
                self.reporter.error('round', f'round {n} not run: no proof provider ({e})')
                self._set_status('round', {'n': n, 'at': time.time(), 'error': str(e)[:300]})
                return None
            self.reporter.note('round', f'round {n}: keeping the previous proof ({e})')
        started = time.time()
        try:
            report = self._run_round(
                self.proof,
                store=self.boxes,
                write_lock=self.write_lock,
                box_locks=self.box_locks,
                pending=self._pending_cards(),
            )
        except Exception as e:  # before any box was visited (the allowlist fetch, our own link): nobody's fault
            self.reporter.error('round', f'round {n} failed before any box was visited: {type(e).__name__}: {e}')
            self._set_status('round', {'n': n, 'at': time.time(), 'error': f'{type(e).__name__}: {e}'[:300]})
            return None
        if getattr(report, 'no_box_answered', False):
            self.reporter.error(
                'round',
                f"round {n}: none of {len(report.boxes)} box(es) answered SSH: the controller's own link is suspect, "
                'no unreachable round counted',
            )
        boxes = {
            r.box.box_id: {
                'before': r.status_before,
                'after': (r.after or r.box).status,
                'verdict': r.verdict.verdict if r.verdict else None,
                'busy': r.busy,
                'transport_error': r.transport_error,
            }
            for r in report.boxes
        }
        self._set_status(
            'round',
            {
                'n': n,
                'started_at': started,
                'finished_at': time.time(),
                'exit_code': report.exit_code,
                'provider': report.provider,
                'timings_ms': report.timings_ms,
                'boxes': boxes,
            },
        )
        self.reporter.round(report, n)
        return report

    def build_once(self) -> bool:
        """``--build-cmd`` once. A non-zero exit keeps the previous provider and binary, is logged as an error with the
        command's tail, and is retried on the next wake (``round_tick``), not at the next round. A build that made it
        re-reads the provider, so the newest built version is what the next probe runs. True when the build passed."""
        if not self.build_cmd or self._build is None:
            return True
        started = time.monotonic()
        proc = self._build(self.build_cmd)
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-1:] or ['']
        took = (time.monotonic() - started) * 1000.0
        if proc.returncode != 0:
            self._build_failed = True
            version = getattr(self.proof, 'version', None)
            self.reporter.error(
                'round',
                f'build exit {proc.returncode} in {took:.0f} ms: {tail[0][:200]!r}; keeping proof {version or "(none)"}, '
                f'retrying in {cfg.ROUND_WAKE_S:.0f} s',
            )
            return False
        was_failing, self._build_failed = self._build_failed, False
        self.reporter.note('round', f'build exit 0 in {took:.0f} ms: {tail[0][:200]}' + (' (recovered)' if was_failing else ''))  # fmt: skip
        try:
            self.proof = self._load_proof()
        except Exception as e:
            self.reporter.error('round', f'built, but the provider did not load: {e}; keeping the previous proof')
        return True

    def reconcile_once(self) -> ReconcileReport:
        self.counts['reconcile'] += 1
        n = self.counts['reconcile']
        with self.write_lock:
            self.boxes.merge_from_disk()  # boxes an operator admitted beside us
        self.reconciler.deployments = DeploymentStore(self.state.deployments)  # operator-owned: read fresh each pass
        report = self.reconciler.run_pass()
        self._set_status(
            'reconcile',
            {
                'n': n,
                'at': time.time(),
                'ok': report.ok,
                'desired': report.desired,
                'running': report.running,
                'actions': [asdict(a) for a in report.actions],
                'errors': report.errors,
                'unreachable': report.unreachable,
                'launched': report.launched,
                'in_flight': report.in_flight,
            },
        )
        self.reporter.reconcile(report, n)
        return report

    def settle_once(self, now: float | None = None) -> int | None:
        """The ledger's settlement tick, when one is due: the rows written, or None."""
        now = time.time() if now is None else now
        if not self.ledger.due(now):
            return None
        with self.write_lock:
            return len(self.ledger.settle(list(self.boxes.boxes.values()), self.instances.instances, now))

    def scorecard_once(self, now: float | None = None) -> dict:
        """Settle the trailing window at the oracle's price and write the scorecard. Returns the document."""
        now = time.time() if now is None else now
        quote = self.oracle.quote()  # may read the network: outside the state lock
        with self.write_lock:
            boxes = dict(self.boxes.boxes)
        start = now - cfg.SETTLEMENT_WINDOW_S
        settlement = settle_window(self.ledger.rows(start, now), boxes, self.rates, quote, start, now)
        doc = build_scorecard(settlement, boxes, self.rates, now, self.intervals.scorecard_s)
        path, sha = write_scorecard(self.state.root / 'scorecard', doc)
        implied = doc['pool']['implied_usd_per_card_hour']
        self._set_status(
            'pay',
            {
                'at': now,
                'sha256': sha,
                'path': str(path),
                'valid_until': doc['valid_until'],
                'recycle_share': doc['recycle_share'],
                'paid_usd': doc['pool']['paid_usd'],
                'pool_usd': doc['pool']['usd'],
                'implied_usd_per_card_hour': implied,
                'oracle': doc['oracle'],
            },
        )
        rates = ', '.join(f'{g} ${v["idle"]:.3f}/${v["leased"]:.3f}' for g, v in implied.items()) or 'no cards'
        paying = sum(1 for h in doc['hotkeys'] if h['weight'] > 0)
        self.reporter.note(
            'pay',
            f'scorecard {sha[:12]}: {paying} hotkey(s) paid, recycle {doc["recycle_share"] * 100:.1f}%, '
            f'idle/leased per card-hour {rates}' + (' (oracle held)' if quote.held else ''),
        )
        self.publish_once(force=True)
        return doc

    def _image_of(self, entry_id: str) -> str | None:
        if entry_id not in self._images:
            try:
                self._images[entry_id] = self.registry.read(entry_id).entry.image
            except Exception:
                return None  # not cached: an entry blessed later is found on a later write
        return self._images[entry_id]

    def publish_once(self, force: bool = False, running: bool = True) -> bool:
        """Write ``public/fleet.json`` (``publish.py``) when it is due. Never raises: the page is not worth a loop."""
        if not self.publisher.due(force):
            return False
        try:
            with self.write_lock:
                boxes, instances = dict(self.boxes.boxes), dict(self.instances.instances)
                status = dict(self.status)
            doc = build_fleet(
                self.state.root,
                boxes,
                instances,
                status,
                running,
                time.time(),
                self._image_of,
                self.network,
                self.netuid,
            )
            self.publisher.write(doc)
        except Exception as e:
            self.publisher.last_at = self.publisher.wall()  # a failing write is retried on the interval, not every tick
            self.reporter.note('publish', f'{type(e).__name__}: {e}')
            return False
        return True

    def watch_once(self) -> WatchReport:
        report = self.watch.run_pass()
        self.settle_once()
        if report.visited:
            self._set_status(
                'watch',
                {
                    'at': time.time(),
                    'visited': report.visited,
                    'actions': [asdict(a) for a in report.actions],
                    'unreachable': report.unreachable,
                },
            )
            self.reporter.watch(report)
        self.reprove_once()
        self.publish_once()
        return report

    def _pending_cards(self) -> dict[str, set[str]]:
        """Per box, the cards an instance record still names: a CHECKING card among them is not proved yet (its lease
        ended while the box was unreachable; the reconciler undeploys the container once the box answers, and only
        then is the card free for the proof)."""
        with self.write_lock:
            out: dict[str, set[str]] = {}
            for record in self.instances.instances.values():
                out.setdefault(record.box, set()).add(record.uuid)
            return out

    def reprove_once(self) -> list[str]:
        """Launch the one-box probe on every box that is due one and not already being probed (or waiting to retry
        one that got no verdict): an IDLE box with a CHECKING card, and a box at ADMIT (its first proof, at once).
        The provider (version, binary, secret store) is re-read first, exactly as the round does after ``--build-cmd``,
        so the probe runs the newest build. Returns the boxes launched."""
        if self._reprove is None:
            return []
        now = time.time()
        pending = self._pending_cards()
        with self.write_lock:
            running = {box_id for box_id, thread in self._reproving.items() if thread.is_alive()}
            due = sorted(
                box.box_id
                for box in self.boxes.boxes.values()
                if box.host
                and box.box_id not in running
                and now >= self._reprove_retry_at.get(box.box_id, 0.0)
                and (
                    (box.status == ADMIT and not box.endpoint_changed)
                    or (
                        box.status == IDLE
                        and any(
                            card.state == CHECKING and uuid not in pending.get(box.box_id, ())
                            for uuid, card in box.cards.items()
                        )
                    )
                )
            )
        if not due:
            return []
        try:
            proof = self.proof = self._load_proof()  # the newest build, re-read as the round does (Kimbo 9/16)
        except Exception as e:
            proof = self.proof
            if proof is None:
                self.reporter.note('reprove', f'not run: no proof provider ({e})')
                return []
            self.reporter.note('reprove', f'keeping the previous proof ({e})')
        for box_id in due:
            thread = threading.Thread(
                target=self._reprove_box,
                args=(proof, box_id, pending.get(box_id, set())),
                name=f'reprove-{box_id[:16]}',
                daemon=True,
            )
            with self.write_lock:
                self._reproving[box_id] = thread
            thread.start()
        return due

    def _reprove_box(self, proof: Any, box_id: str, exclude: set[str]) -> None:
        reprove = self._reprove
        if reprove is None:
            return
        try:
            report = reprove(
                proof, box_id, store=self.boxes, write_lock=self.write_lock, box_locks=self.box_locks, exclude=exclude
            )
        except Exception as e:  # never kill the thread silently; the next tick after the retry delay tries again
            with self.write_lock:
                self._reprove_retry_at[box_id] = time.time() + cfg.REPROVE_RETRY_S
            self.reporter.note('reprove', f'{box_id[:16]}: {type(e).__name__}: {e}')
            return
        (row,) = report.boxes
        with self.write_lock:
            if row.verdict is None:
                self._reprove_retry_at[box_id] = time.time() + cfg.REPROVE_RETRY_S
            else:
                self._reprove_retry_at.pop(box_id, None)
        self._set_status(
            'reprove',
            {
                'at': time.time(),
                'box': box_id,
                'before': row.status_before,
                'after': (row.after or row.box).status,
                'verdict': row.verdict.verdict if row.verdict else None,
                'busy': row.busy,
                'transport_error': row.transport_error,
            },
        )
        self.reporter.reprove(report)

    def discover_once(self) -> DiscoverReport | None:
        """Read the metagraph and settle the boxes against it. A failed read changes nothing."""
        if self.read_chain is None:
            return None
        try:
            endpoints = self.read_chain()
        except Exception as e:
            self.reporter.note('discover', f'metagraph read failed, nothing changed: {type(e).__name__}: {e}')
            self._set_status('discover', {'at': time.time(), 'error': f'{type(e).__name__}: {e}'[:300]})
            return None
        report = self.discovery.run_pass(endpoints)
        self._set_status(
            'discover',
            {
                'at': time.time(),
                'registered': report.registered,
                'compute': report.compute,
                'actions': [asdict(a) for a in report.actions],
                'ignored': report.ignored,
            },
        )
        self.reporter.discover(report)
        return report

    def _background_done(self, report: ReconcileReport) -> None:
        with self.write_lock:
            self.status['reconcile']['last_background'] = {
                'at': time.time(),
                'actions': [asdict(a) for a in report.actions],
                'errors': report.errors,
                'unreachable': report.unreachable,
            }
        self._write_status()
        self.reporter.background(report)

    # -- status file ------------------------------------------------------------------------------------------------

    def _set_status(self, key: str, value: dict) -> None:
        with self.write_lock:
            self.status[key] = value
        self._write_status()

    def _write_status(self) -> None:
        path = self.state.root / STATUS_FILE
        with self.write_lock:
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(self.status, indent=1, default=str))
            tmp.replace(path)

    # -- the loops --------------------------------------------------------------------------------------------------

    def _loop(
        self,
        name: str,
        once: Callable[[], Any],
        interval_s: float,
        after: Callable[[], None] | None,
        first_wait_s: float = 0.0,
    ) -> None:
        if first_wait_s and self.stop.wait(first_wait_s):
            return
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                once()
            except Exception as e:  # one bad pass never ends a loop; state is saved write by write
                self.reporter.note(name, f'{type(e).__name__}: {e}')
            if after is not None and not self.stop.is_set():
                try:
                    after()
                except Exception as e:
                    self.reporter.note(name, f'{type(e).__name__}: {e}')
            self.stop.wait(max(0.0, interval_s - (time.monotonic() - started)))

    def start(self) -> None:
        loops = (
            ('round', self.round_tick, cfg.ROUND_WAKE_S, None),
            ('reconcile', self.reconcile_once, self.intervals.reconcile_s, None),
            ('watch', self.watch_once, self.intervals.watch_tick_s, None),
            ('scorecard', self.scorecard_once, self.intervals.scorecard_s, None),
        )
        first_wait = {}
        if self.read_chain is not None:
            # Discovery before round 1 (Kimbo 9/16): a box already on chain is in the first round instead of waiting
            # at ADMIT for the first discovery pass. A failed read changes nothing; the loop retries on its interval.
            try:
                self.discover_once()
            except Exception as e:
                self.reporter.note('discover', f'{type(e).__name__}: {e}')
            loops += (('discover', self.discover_once, self.intervals.discover_s, None),)
            first_wait['discover'] = self.intervals.discover_s
        for name, once, interval_s, after in loops:
            thread = threading.Thread(
                target=self._loop,
                args=(name, once, interval_s, after, first_wait.get(name, 0.0)),
                name=f'controller-{name}',
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def shutdown(self, grace_s: float = cfg.SHUTDOWN_GRACE_S) -> bool:
        """Stop the loops, let each finish the visit in flight (up to ``grace_s``), write state. True when every loop
        stopped in time. Background starts are not waited for."""
        self.stop.set()
        deadline = time.monotonic() + grace_s
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        clean = not any(thread.is_alive() for thread in self._threads)
        with self.write_lock:
            self.boxes.save()
            self.instances.save()
            self.status['stopped_at'] = time.time()
        self._write_status()
        self.publish_once(force=True, running=False)
        return clean

    def serve(self, max_seconds: float | None = None, install_signals: bool = True) -> bool:
        previous = {}
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda *_: self.stop.set())
        try:
            self.start()
            began = time.monotonic()
            while not self.stop.wait(1.0):
                if max_seconds and time.monotonic() - began >= max_seconds:
                    break
            self.reporter.note('controller', 'stopping: finishing the visits in flight')
            return self.shutdown()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
