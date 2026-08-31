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
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import bittensor as bt

from gittensor.constants import SERVING_READY_TTL_S, SERVING_REQUEST_LOG_SIZE, SERVING_SETTLEMENT_ROUNDS
from gittensor.serving.audit import AuditWindow


@dataclass
class ReadyMiner:
    uid: int
    hotkey: str
    axon: bt.AxonInfo
    score: float
    release_id: str = ''  # release this miner passed audits for


@dataclass
class ServedRequest:
    """One gateway request as served, queued for verification by the audit loop."""

    ts: float
    uid: int
    hotkey: str
    model_id: str  # the model the miner reported serving
    messages: List[Dict[str, str]]
    ok: bool
    latency_ms: Optional[float]
    completion: Optional[str] = None
    tokens: Optional[List[str]] = None
    token_ids: Optional[List[int]] = None
    token_bytes: Optional[List[List[int]]] = None
    token_logprobs: Optional[List[float]] = None
    detail: str = ''  # axon status message when not ok
    source: str = 'gateway'  # 'gateway' | 'baseline'
    ttft_ms: Optional[float] = None  # validator-observed time to first streamed event
    inflight: int = 1  # this validator's requests in flight to the miner when this one was dispatched (incl. itself)
    release_id: str = ''  # the release this request was routed for; audited against that release's reference
    max_tokens: int = 0  # what was asked for; a completion longer than this is not the runtime's

    def __post_init__(self) -> None:
        if not self.release_id:
            self.release_id = self.model_id


@dataclass
class RequestRecord:
    ts: float
    kind: str  # 'verify' | 'gateway'
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
    dormant_rounds: Dict[str, int] = field(default_factory=dict)  # audit thread only: hotkey -> rounds w/o completion
    attest_status: Dict[str, dict] = field(default_factory=dict)  # audit thread only: hotkey -> last attest verdict
    uuid_owner: Dict[str, Tuple[str, int]] = field(default_factory=dict)  # GPU UUID -> (hotkey, round last seen)
    last_credit: Dict[str, float] = field(default_factory=dict)  # audit thread only: hotkey -> last measured credit
    _sent_tokens: Dict[str, Deque[Tuple[float, int]]] = field(default_factory=dict)  # hotkey -> (ts, max_tokens)
    attest_round: int = 0
    last_round: dict = field(default_factory=dict)  # audit thread's summary of the last round, for /v1/serving/status
    _rng: random.Random = field(default_factory=random.Random, repr=False)
    settlement_rounds: int = SERVING_SETTLEMENT_ROUNDS
    last_round_ts: float = 0.0
    ready_ttl_s: float = SERVING_READY_TTL_S

    def publish_round(
        self,
        miners: List[ReadyMiner],
        scores: Dict[str, float],
        probation: Optional[List[ReadyMiner]] = None,
        summary: Optional[dict] = None,
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
            self.last_round = dict(summary or {})
            self.last_round_ts = time.time()

    def seed_ready(self, miners: List[ReadyMiner], probation: Optional[List[ReadyMiner]] = None) -> None:
        """Startup: republish the READY set derived from the durable store, without settling a score round.

        Routing eligibility only — ``_history`` is untouched, so nothing is paid for a round this validator
        did not run, and ``last_round_ts`` keeps the restored value: the TTL counts from the round that
        actually measured these miners, so trust never outlives what an uninterrupted validator would have had.
        """
        with self._lock:
            self._ready = {m.uid: m for m in miners}
            self._probation = {m.uid: m for m in (probation or []) if m.uid not in self._ready}
            live = set(self._ready) | set(self._probation)
            self._inflight = {uid: self._inflight.get(uid, 0) for uid in live}
            self.last_round = {'seeded': True, 'ready': len(self._ready), 'probation': len(self._probation)}

    def scores_for(self, hotkeys: Sequence[str]) -> Dict[int, float]:
        """Trailing-window settled scores keyed by current UID (missing rounds count 0); a UID whose hotkey
        changed since the rounds carries nothing over."""
        with self._lock:
            return {
                uid: sum(self._history[hk]) / self.settlement_rounds
                for uid, hk in enumerate(hotkeys)
                if hk in self._history
            }

    def settled_scores(self) -> Dict[str, float]:
        """Trailing-window settled score per hotkey (missing rounds count 0)."""
        with self._lock:
            return {hk: sum(xs) / self.settlement_rounds for hk, xs in self._history.items()}

    def enqueue_served(self, served: ServedRequest) -> None:
        with self._lock:
            self._served.append(served)

    def charge_sent(self, hotkey: str, max_tokens: int, now: Optional[float] = None) -> None:
        """This validator's own ledger of what it asked each miner for: the miner charges a permitted validator's
        per-tempo budget on ``max_tokens`` up front, so the validator keeps the same count to judge a refusal."""
        with self._lock:
            self._sent_tokens.setdefault(hotkey, deque(maxlen=SERVING_REQUEST_LOG_SIZE)).append(
                (now if now is not None else time.time(), int(max_tokens))
            )

    def sent_tokens(self, hotkey: str, window_s: float, now: Optional[float] = None) -> int:
        """``max_tokens`` this validator sent ``hotkey`` in the trailing ``window_s`` seconds."""
        since = (now if now is not None else time.time()) - window_s
        with self._lock:
            return sum(n for ts, n in self._sent_tokens.get(hotkey, ()) if ts >= since)

    def drain_served(self) -> List[ServedRequest]:
        """Audit thread: take every request served since the last round."""
        with self._lock:
            items = list(self._served)
            self._served.clear()
            return items

    def _fresh(self) -> bool:
        return time.time() - self.last_round_ts <= self.ready_ttl_s

    def ready_miners(self) -> List[ReadyMiner]:
        with self._lock:
            return list(self._ready.values()) if self._fresh() else []

    def acquire(self, release_id: Optional[str] = None, probation: bool = False) -> Optional[ReadyMiner]:
        """Pick a READY miner (for ``release_id`` if given) with the fewest in-flight requests.

        Among those, the choice is random, weighted by score: a better miner is sent more, but not everything.
        Breaking the tie deterministically sends every request to one miner whenever requests do not overlap - at
        one request per 20 s against sub-second completions, in-flight is always 0 - which loads that miner until
        its own TTFT, and so its pay, falls, while the miner beside it is barely measured.

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
                    if (release_id is None or m.release_id == release_id) and self._inflight.get(u, 0) == 0
                ]
                if idle:
                    uid = min(idle)
                    self._inflight[uid] = 1
                    return self._probation[uid]
            candidates = [u for u, m in self._ready.items() if release_id is None or m.release_id == release_id]
            if not candidates:
                return None
            fewest = min(self._inflight.get(u, 0) for u in candidates)
            uid = self._weighted_choice([u for u in candidates if self._inflight.get(u, 0) == fewest])
            self._inflight[uid] = self._inflight.get(uid, 0) + 1
            return self._ready[uid]

    def _weighted_choice(self, uids: List[int]) -> int:
        """One of ``uids``, with probability proportional to score; uniform when no score separates them."""
        if len(uids) == 1:
            return uids[0]
        weights = [max(0.0, self._ready[u].score) for u in uids]
        total = sum(weights)
        if total <= 0:
            return self._rng.choice(uids)
        return self._rng.choices(uids, weights=weights, k=1)[0]

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
                'last_round': dict(self.last_round),
                'requests_logged': len(log),
                'gateway_requests': sum(1 for r in log if r.kind == 'gateway'),
                'gateway_ok': sum(1 for r in log if r.kind == 'gateway' and r.ok),
            }
