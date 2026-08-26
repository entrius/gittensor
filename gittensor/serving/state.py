# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared serving state between the validator audit loop and the gateway.

The audit loop (validator thread) publishes which miners are READY — their
rolling ``AuditWindow`` passes — and the gateway thread dispatches user traffic
to them, least-in-flight first. Every request the gateway serves is handed back
to the audit loop (``enqueue_served`` / ``drain_served``) to be verified against
the reference: served traffic *is* the audit. Miners that are not yet READY sit
in *probation* and only receive traffic from baseline keys, so a new miner can
earn a window without a real user ever hitting an unverified card. Per-round
scores are settled over the trailing ``SERVING_SETTLEMENT_ROUNDS`` rounds.
Both threads also append to one request log (telemetry).
"""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence

import bittensor as bt

from gittensor.constants import SERVING_READY_TTL_S, SERVING_REQUEST_LOG_SIZE, SERVING_SETTLEMENT_ROUNDS
from gittensor.serving.audit import AuditWindow


@dataclass
class ReadyMiner:
    uid: int
    hotkey: str
    axon: bt.AxonInfo
    score: float
    model_id: str = ''  # release this miner passed audits for


@dataclass
class ServedRequest:
    """One gateway request as served, queued for verification by the audit loop."""

    ts: float
    uid: int
    hotkey: str
    model_id: str
    messages: List[Dict[str, str]]
    ok: bool
    latency_ms: Optional[float]
    completion: Optional[str] = None
    tokens: Optional[List[str]] = None
    token_logprobs: Optional[List[float]] = None


@dataclass
class RequestRecord:
    ts: float
    kind: str  # 'probe' | 'gateway'
    uid: Optional[int]
    ok: bool
    latency_ms: Optional[float]  # None when the request produced no response
    completion_tokens: int = 0
    ttft_ms: Optional[float] = None
    decode_tps: Optional[float] = None
    detail: str = ''

    def __post_init__(self) -> None:
        for name in ('latency_ms', 'ttft_ms', 'decode_tps'):
            self.__dict__[name] = finite_or_none(getattr(self, name))


def finite_or_none(value: Optional[float]) -> Optional[float]:
    """Miner-reported floats go into JSON responses, which reject NaN/inf."""
    return float(value) if value is not None and math.isfinite(value) else None


@dataclass
class ServingState:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ready: Dict[int, ReadyMiner] = field(default_factory=dict)
    _probation: Dict[int, ReadyMiner] = field(default_factory=dict)
    _inflight: Dict[int, int] = field(default_factory=dict)
    _log: Deque[RequestRecord] = field(default_factory=lambda: deque(maxlen=SERVING_REQUEST_LOG_SIZE))
    _served: Deque[ServedRequest] = field(default_factory=lambda: deque(maxlen=SERVING_REQUEST_LOG_SIZE))
    audits: AuditWindow = field(default_factory=AuditWindow)  # audit-loop thread only; persisted by the validator
    _history: Dict[str, Deque[float]] = field(default_factory=dict)  # hotkey -> last N round scores
    settlement_rounds: int = SERVING_SETTLEMENT_ROUNDS
    last_round_ts: float = 0.0
    ready_ttl_s: float = SERVING_READY_TTL_S

    def publish_round(
        self, miners: List[ReadyMiner], scores: Dict[str, float], probation: Optional[List[ReadyMiner]] = None
    ) -> None:
        """Audit thread: publish the READY and probation sets for the gateway and settle this round's scores.

        Every hotkey with history and no score this round records a 0, so a miner that vanishes stops earning
        within one settlement window.
        """
        with self._lock:
            self._ready = {m.uid: m for m in miners}
            self._probation = {m.uid: m for m in (probation or []) if m.uid not in self._ready}
            live = set(self._ready) | set(self._probation)
            self._inflight = {uid: self._inflight.get(uid, 0) for uid in live}
            for hotkey in set(self._history) | set(scores):
                if hotkey not in self._history:
                    self._history[hotkey] = deque(maxlen=self.settlement_rounds)
                self._history[hotkey].append(float(scores.get(hotkey, 0.0)))
            for hotkey in [hk for hk, xs in self._history.items() if not any(xs)]:
                del self._history[hotkey]
            self.last_round_ts = time.time()

    def scores_for(self, hotkeys: Sequence[str]) -> Dict[int, float]:
        """Trailing-window settled scores keyed by current UID (missing rounds count 0); a UID whose hotkey
        changed since the rounds carries nothing over."""
        with self._lock:
            return {
                uid: sum(self._history[hk]) / self.settlement_rounds
                for uid, hk in enumerate(hotkeys)
                if hk in self._history
            }

    def enqueue_served(self, served: ServedRequest) -> None:
        with self._lock:
            self._served.append(served)

    def drain_served(self) -> List[ServedRequest]:
        """Audit thread: take every request served since the last round."""
        with self._lock:
            items = list(self._served)
            self._served.clear()
            return items

    def probation_miners(self) -> List[ReadyMiner]:
        with self._lock:
            return list(self._probation.values()) if self._fresh() else []

    def _fresh(self) -> bool:
        return time.time() - self.last_round_ts <= self.ready_ttl_s

    def ready_miners(self) -> List[ReadyMiner]:
        with self._lock:
            return list(self._ready.values()) if self._fresh() else []

    def acquire(self, model_id: Optional[str] = None, probation: bool = False) -> Optional[ReadyMiner]:
        """Pick the READY miner (for ``model_id`` if given) with the fewest in-flight requests (ties -> higher score).

        With ``probation`` (baseline traffic) an idle probation miner is preferred, at most one request in flight
        each, so unverified miners get exactly the traffic they need to earn a window and no more.
        Returns None when there is no capacity or the last audit round is older than the READY TTL.
        """
        with self._lock:
            if not self._fresh():
                return None
            if probation:
                idle = [
                    u
                    for u, m in self._probation.items()
                    if (model_id is None or m.model_id == model_id) and self._inflight.get(u, 0) == 0
                ]
                if idle:
                    uid = min(idle)
                    self._inflight[uid] = 1
                    return self._probation[uid]
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
                'probation_uids': sorted(self._probation),
                'pending_verification': len(self._served),
                'inflight': dict(self._inflight),
                'last_round_ts': self.last_round_ts,
                'requests_logged': len(log),
                'gateway_requests': sum(1 for r in log if r.kind == 'gateway'),
                'gateway_ok': sum(1 for r in log if r.kind == 'gateway' and r.ok),
            }
