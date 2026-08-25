# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared serving state between the validator audit loop and the gateway.

The audit loop (validator thread) publishes which miners are READY — their
rolling ``AuditWindow`` passes — and the gateway thread dispatches user traffic
to them, least-in-flight first. Both threads also append to one request log,
which is the seed of every later phase: miner speed scoring on organic
traffic, the router competition's replay dataset, and usage telemetry.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

import bittensor as bt

from gittensor.constants import SERVING_REQUEST_LOG_SIZE
from gittensor.serving.audit import AuditWindow


@dataclass
class ReadyMiner:
    uid: int
    hotkey: str
    axon: bt.AxonInfo
    score: float
    model_id: str = ''  # release this miner passed audits for


@dataclass
class RequestRecord:
    ts: float
    kind: str  # 'audit' | 'gateway'
    uid: Optional[int]
    ok: bool
    latency_ms: Optional[float]  # None when the request produced no response
    completion_tokens: int = 0
    ttft_ms: Optional[float] = None
    decode_tps: Optional[float] = None
    detail: str = ''


@dataclass
class ServingState:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ready: Dict[int, ReadyMiner] = field(default_factory=dict)
    _inflight: Dict[int, int] = field(default_factory=dict)
    _log: Deque[RequestRecord] = field(default_factory=lambda: deque(maxlen=SERVING_REQUEST_LOG_SIZE))
    audits: AuditWindow = field(default_factory=AuditWindow)  # audit-loop thread only; persisted by the validator
    last_round_ts: float = 0.0

    def publish_ready(self, miners: List[ReadyMiner]) -> None:
        with self._lock:
            self._ready = {m.uid: m for m in miners}
            self._inflight = {uid: self._inflight.get(uid, 0) for uid in self._ready}
            self.last_round_ts = time.time()

    def ready_miners(self) -> List[ReadyMiner]:
        with self._lock:
            return list(self._ready.values())

    def acquire(self, model_id: Optional[str] = None) -> Optional[ReadyMiner]:
        """Pick the READY miner (for ``model_id`` if given) with the fewest in-flight requests (ties -> higher score)."""
        with self._lock:
            candidates = [u for u, m in self._ready.items() if model_id is None or m.model_id == model_id]
            if not candidates:
                return None
            uid = min(candidates, key=lambda u: (self._inflight.get(u, 0), -self._ready[u].score))
            self._inflight[uid] = self._inflight.get(uid, 0) + 1
            return self._ready[uid]

    def release(self, uid: int) -> None:
        with self._lock:
            if uid in self._inflight and self._inflight[uid] > 0:
                self._inflight[uid] -= 1

    def inflight(self) -> Dict[int, int]:
        with self._lock:
            return dict(self._inflight)

    def record(self, rec: RequestRecord) -> None:
        with self._lock:
            self._log.append(rec)

    def recent(self, n: int = 100) -> List[RequestRecord]:
        with self._lock:
            return list(self._log)[-n:]

    def snapshot(self) -> dict:
        with self._lock:
            log = list(self._log)
            return {
                'ready_uids': sorted(self._ready),
                'inflight': dict(self._inflight),
                'last_round_ts': self.last_round_ts,
                'requests_logged': len(log),
                'gateway_requests': sum(1 for r in log if r.kind == 'gateway'),
                'gateway_ok': sum(1 for r in log if r.kind == 'gateway' and r.ok),
            }
