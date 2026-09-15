# The MIT License (MIT)
# Copyright © 2025 Entrius

"""One in-memory lock per box, for ``gitt controller run`` (vault ``24`` §3 WS-D, ``23`` §4a).

What must never overlap on one box is a GPU proof and a lease start or drain: the proof would land on a card mid-load,
or a start on a card mid-proof. The reconciler holds a box's lock for the whole of its starts and drains there (a model
load is minutes); the proof round waits a little for it and otherwise skips that box until the next round. The watch
(heartbeat + health) takes no box lock: it only reads, and the undeploys it issues are idempotent, so it never waits
behind a model load. State writes are serialised separately by one short lock around the state files.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class BoxLocks:
    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, box_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(box_id, threading.Lock())

    def acquire(self, box_id: str, timeout: float | None = None) -> bool:
        """Blocking without a timeout; False when ``timeout`` ran out. A plain lock: any thread may release it."""
        return self._lock(box_id).acquire(timeout=-1 if timeout is None else max(0.0, timeout))

    def release(self, box_id: str) -> None:
        self._lock(box_id).release()

    def held(self, box_id: str) -> bool:
        return self._lock(box_id).locked()

    @contextmanager
    def hold(self, box_id: str) -> Iterator[None]:
        self.acquire(box_id)
        try:
            yield
        finally:
            self.release(box_id)
