# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Discovery via the chain (vault ``24`` §3 WS-A "Next slice"): the fleet admits itself.

The controller has no inbound endpoints and the agent runs nothing but sshd, so a box cannot announce itself to us:
the chain is the registry. ``gitt up`` serves the box's public IP and sshd port as its hotkey's axon, marked as a
compute endpoint (``gittensor.agent.config.is_compute_axon``). The controller only **reads** the metagraph (it holds
no chain key) and, every ``DISCOVER_INTERVAL_S``, settles the state store against it:

* **A registered hotkey with a compute endpoint and no box**: ``ssh-keyscan`` the address, pin the key, create the box
  at ADMIT, exactly what ``gitt controller admit`` does by hand; the next proof round checks it.
* **An endpoint that changed**: re-scan the new address. The pinned host key answering there moves the box. Any other
  key (or none) is **not re-pinned**: the box is flagged ``endpoint_changed``, gets no verdict and counts an unreachable
  round every round (three bench it for 12 h), and takes no new instance, until ``gitt controller admit
  --force-rekey`` or the pinned key answers at the new address.
* **A deregistered hotkey**: BENCHED without end so the reconciler drains its instances; once nothing runs there the
  box leaves the store and its ``known_hosts`` entry goes with it.

Only boxes discovery created (``source == 'chain'``) are ever moved or removed: a box an operator admitted is left
alone. A compute endpoint on a non-public address, or on an address another box already holds, is never scanned.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from gittensor.agent.config import is_compute_axon
from gittensor.controller.checks.state import DEREGISTERED, BoxState, StateStore, apply_deregistered
from gittensor.controller.reconcile import InstanceStore
from gittensor.controller.ssh import SshTransportError, write_host_key

CHAIN = 'chain'
MAX_PARALLEL_SCANS = 32


@dataclass(frozen=True)
class ChainEndpoint:
    """One neuron's axon as the metagraph reports it."""

    hotkey: str
    ip: str
    port: int
    compute: bool  # carries the compute-box marker
    uid: int | None = None

    @classmethod
    def from_axon(cls, axon, uid: int | None = None) -> ChainEndpoint:
        marker = (int(axon.protocol), int(axon.placeholder1), int(axon.placeholder2))
        return cls(str(axon.hotkey), str(axon.ip), int(axon.port), is_compute_axon(*marker), uid)


def address_problem(ip: str, port: int) -> str:
    """Why the controller must not dial this address ('' when it may). A miner chooses what it publishes: nothing
    private, loopback or link-local is scanned, or a hotkey could point our controller at our own network."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return f'{ip!r} is not an IP address'
    if not address.is_global:
        return f'{ip} is not a public address'
    if not 1 <= int(port) <= 65535:
        return f'port {port} is out of range'
    return ''


class ChainReader:
    """The metagraph of one netuid, read-only: the same ``bittensor`` call the validator syncs with. One connection is
    kept across reads and dropped on an error, so the next read reconnects."""

    def __init__(self, endpoint: str, netuid: int):
        self.endpoint, self.netuid = endpoint, netuid
        self._subtensor = None

    def read(self) -> list[ChainEndpoint]:
        import bittensor as bt

        try:
            if self._subtensor is None:
                self._subtensor = bt.Subtensor(network=self.endpoint)
            metagraph = self._subtensor.metagraph(self.netuid)
        except Exception:
            self._subtensor = None
            raise
        return [ChainEndpoint.from_axon(axon, int(uid)) for uid, axon in zip(metagraph.uids, metagraph.axons)]


@dataclass
class DiscoverAction:
    kind: str  # admit | moved | endpoint_changed | endpoint_restored | deregistered | removed | scan_failed | conflict
    hotkey: str
    ok: bool = True
    detail: str = ''


@dataclass
class DiscoverReport:
    registered: int = 0  # hotkeys on the metagraph
    compute: int = 0  # of them, carrying the compute marker
    actions: list[DiscoverAction] = field(default_factory=list)
    ignored: dict[str, str] = field(default_factory=dict)  # compute endpoints never dialled, and why

    @property
    def ok(self) -> bool:
        return all(a.ok for a in self.actions)


@dataclass
class Discovery:
    boxes: StateStore
    instances: InstanceStore
    known_hosts: Path
    scan: Callable[[str, int], str]  # ssh-keyscan: the ed25519 host key, or raises SshTransportError
    wall: Callable[[], float] = time.time
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)  # the shared state-write lock

    def run_pass(self, endpoints: Iterable[ChainEndpoint]) -> DiscoverReport:
        endpoints = list(endpoints)
        report = DiscoverReport(registered=len({e.hotkey for e in endpoints}))
        registered = {e.hotkey for e in endpoints}
        compute: dict[str, ChainEndpoint] = {}
        for e in endpoints:
            if not e.compute:
                continue
            report.compute += 1
            problem = address_problem(e.ip, e.port)
            if problem:
                report.ignored[e.hotkey] = problem
            else:
                compute[e.hotkey] = e

        with self.lock:
            self.boxes.merge_from_disk()
            boxes = {box_id: BoxState.from_dict(box.as_dict()) for box_id, box in self.boxes.boxes.items()}
        to_scan: dict[str, ChainEndpoint] = {}
        for hotkey, e in sorted(compute.items()):
            box = boxes.get(hotkey)
            if box is None:
                to_scan[hotkey] = e
            elif box.source == CHAIN and box.last_failed != [DEREGISTERED] and (box.host, box.port) != (e.ip, e.port):
                to_scan[hotkey] = e

        keys: dict[str, str | Exception] = {}

        def scan(hotkey: str) -> None:
            e = to_scan[hotkey]
            try:
                keys[hotkey] = self.scan(e.ip, e.port)
            except (SshTransportError, OSError) as error:
                keys[hotkey] = error

        if to_scan:
            with ThreadPoolExecutor(max_workers=min(len(to_scan), MAX_PARALLEL_SCANS)) as pool:
                list(pool.map(scan, sorted(to_scan)))

        with self.lock:
            for hotkey, e in sorted(to_scan.items()):
                self._settle_endpoint(hotkey, e, keys[hotkey], report)
            for hotkey, box in sorted(self.boxes.boxes.items()):
                if box.source != CHAIN:
                    continue
                e = compute.get(hotkey)
                if hotkey not in registered or box.last_failed == [DEREGISTERED]:
                    self._deregister(box, hotkey in registered, report)
                elif e is not None and box.endpoint_changed and (box.host, box.port) == (e.ip, e.port):
                    restored = BoxState.from_dict(box.as_dict())
                    restored.endpoint_changed = {}
                    self.boxes.put(restored)
                    report.actions.append(
                        DiscoverAction('endpoint_restored', hotkey, True, f'chain publishes {e.ip}:{e.port} again')
                    )
            self._record_uids({e.hotkey: e.uid for e in endpoints})
        return report

    def _record_uids(self, uids: dict[str, int | None]) -> None:
        """Every box's UID as this read has it, an operator's box too; None for a hotkey not on the metagraph. Under
        the lock. Not an action: a UID is only shown, nothing is decided from it."""
        for hotkey, box in sorted(self.boxes.boxes.items()):
            uid = uids.get(hotkey)
            if box.uid != uid:
                new = BoxState.from_dict(box.as_dict())
                new.uid = uid
                self.boxes.put(new)

    # -- one hotkey (under the lock) ------------------------------------------------------------------------------------

    def _holder(self, hotkey: str, ip: str, port: int) -> str:
        """Another box already at this address (its known_hosts entry is that box's pinned key)."""
        return next((b.box_id for b in self.boxes.boxes.values() if b.box_id != hotkey and (b.host, b.port) == (ip, port)), '')  # fmt: skip

    def _settle_endpoint(self, hotkey: str, e: ChainEndpoint, key: str | Exception, report: DiscoverReport) -> None:
        now = self.wall()
        address = f'{e.ip}:{e.port}'
        holder = self._holder(hotkey, e.ip, e.port)
        box = self.boxes.boxes.get(hotkey)
        if box is None:
            if holder:
                report.actions.append(DiscoverAction('conflict', hotkey, False, f'{address} is already box {holder}'))
            elif isinstance(key, Exception):
                report.actions.append(DiscoverAction('scan_failed', hotkey, False, f'{address}: {key}'[:300]))
            else:
                self.boxes.put(BoxState(hotkey, source=CHAIN, host=e.ip, port=e.port, host_key=key))
                write_host_key(self.known_hosts, e.ip, e.port, key)
                report.actions.append(DiscoverAction('admit', hotkey, True, f'{address}: host key {key} pinned, ADMIT'))
            return
        if box.source != CHAIN or (box.host, box.port) == (e.ip, e.port):
            return  # an operator's box, or already settled by an admit beside us
        new = BoxState.from_dict(box.as_dict())
        if not holder and not isinstance(key, Exception) and key == box.host_key:
            write_host_key(self.known_hosts, box.host, box.port, None)
            write_host_key(self.known_hosts, e.ip, e.port, key)
            moved_from = f'{box.host}:{box.port}'
            new.host, new.port, new.endpoint_changed = e.ip, e.port, {}
            self.boxes.put(new)
            report.actions.append(
                DiscoverAction('moved', hotkey, True, f'{moved_from} -> {address}: the pinned host key answers there')
            )
            return
        seen = '' if isinstance(key, Exception) else key
        flagged = new.endpoint_changed
        if (flagged.get('host'), flagged.get('port'), flagged.get('host_key')) == (e.ip, e.port, seen):
            return  # already flagged for exactly this; the round keeps counting it
        if holder:
            why = f'{address} is already box {holder}'
        elif isinstance(key, Exception):
            why = f'no host key at {address} ({key})'[:300]
        else:
            why = f'{address} presents {seen}, pinned {box.host_key}'
        new.endpoint_changed = {'host': e.ip, 'port': e.port, 'host_key': seen, 'at': now, 'why': why}
        self.boxes.put(new)
        report.actions.append(
            DiscoverAction(
                'endpoint_changed', hotkey, False,
                f'was {box.host}:{box.port}; {why}: not re-pinned (`gitt controller admit --force-rekey` after verifying)',
            )
        )  # fmt: skip

    def _deregister(self, box: BoxState, registered_again: bool, report: DiscoverReport) -> None:
        live = self.instances.on_box(box.box_id)
        if live:
            if box.last_failed != [DEREGISTERED]:
                self.boxes.put(apply_deregistered(box, self.wall()))
                report.actions.append(
                    DiscoverAction('deregistered', box.box_id, True, f'BENCHED; {len(live)} instance(s) to drain')
                )
            return
        self.boxes.remove(box.box_id)
        if box.host:
            write_host_key(self.known_hosts, box.host, box.port, None)
        again = ' (registered again: re-admitted on the next pass)' if registered_again else ''
        report.actions.append(DiscoverAction('removed', box.box_id, True, f'deregistered, nothing left on it{again}'))
