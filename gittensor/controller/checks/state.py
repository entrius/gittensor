# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Per-box state for the idle pool: ADMIT / IDLE / BENCHED, the UUID pin, and the bench backoff ladder.

Pure functions over a ``BoxState`` plus a tiny JSON store; no scheduler yet (that is WS-D/E). A box enters at ADMIT;
its first passing full check pins its UUIDs and moves it to IDLE, where it is re-checked every
``FULL_CHECK_INTERVAL_S``. Any BENCH verdict sends it to BENCHED for the next rung of the ladder (1 h -> 4 h -> 16 h
-> 64 h, ``23`` §5), clears the pin, and when the bench expires it re-enters through ADMIT. A long clean stretch
resets the ladder. LEASED / RELEASING arrive with the lease state machine.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.verdict import CheckVerdict

ADMIT = 'ADMIT'
IDLE = 'IDLE'
BENCHED = 'BENCHED'
STATUSES = (ADMIT, IDLE, BENCHED)


@dataclass
class BoxState:
    box_id: str  # the miner hotkey (one box per hotkey in step 1)
    status: str = ADMIT
    pinned_uuids: List[str] = field(default_factory=list)
    card_name: str = ''
    bench_count: int = 0  # rungs climbed; resets after a clean stretch
    benched_at: Optional[float] = None
    bench_until: Optional[float] = None
    last_check_at: Optional[float] = None
    last_failed: List[str] = field(default_factory=list)
    admitted_at: Optional[float] = None
    unreachable_count: int = 0  # consecutive rounds with no verdict because SSH failed; reset by any verdict
    # Where the box's agent sshd answers, and its host key pinned at admission (``gitt controller admit``). Files
    # written before these fields existed load with the defaults.
    host: str = ''
    port: int = 0
    host_key: str = ''

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'BoxState':
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def backoff_seconds(bench_count: int, ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S) -> int:
    """Bench length for the ``bench_count``-th bench (1-based); past the last rung it stays at the last rung."""
    if bench_count < 1:
        return 0
    return int(ladder[min(bench_count, len(ladder)) - 1])


def apply_verdict(
    state: BoxState,
    verdict: CheckVerdict,
    now: float,
    ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S,
    ladder_reset_after_s: float = cfg.BENCH_LADDER_RESET_AFTER_S,
) -> BoxState:
    """The state after a full check. Pure: returns a new ``BoxState``."""
    new = BoxState.from_dict(state.as_dict())
    new.last_check_at = now
    new.last_failed = list(verdict.failed)
    new.unreachable_count = 0
    if verdict.admitted:
        if new.status == ADMIT:
            new.pinned_uuids = list(verdict.gpu_uuids)
            new.card_name = verdict.card_name
            new.admitted_at = now
        new.status = IDLE
        return new
    # BENCH: climb the ladder (or restart it after a long clean stretch), clear the pin, wait it out.
    if new.benched_at is not None and now - new.benched_at > ladder_reset_after_s:
        new.bench_count = 0
    new.bench_count += 1
    new.status = BENCHED
    new.benched_at = now
    new.bench_until = now + backoff_seconds(new.bench_count, ladder)
    new.pinned_uuids = []
    new.admitted_at = None
    return new


UNREACHABLE = 'ssh_unreachable'


def apply_unreachable(
    state: BoxState,
    now: float,
    bench_after: int = cfg.UNREACHABLE_BENCH_AFTER,
    bench_s: float = cfg.UNREACHABLE_BENCH_S,
) -> BoxState:
    """The state after a round in which SSH could not reach the box: no verdict, the count goes up, and at
    ``bench_after`` in a row the box is BENCHED for a flat ``bench_s`` without climbing the fraud ladder. Pure."""
    new = BoxState.from_dict(state.as_dict())
    new.unreachable_count += 1
    if new.unreachable_count >= bench_after and new.status != BENCHED:
        new.status = BENCHED
        new.benched_at = now
        new.bench_until = now + bench_s
        new.last_failed = [UNREACHABLE]
        new.pinned_uuids = []
        new.admitted_at = None
    return new


def release_from_bench(state: BoxState, now: float) -> BoxState:
    """BENCHED -> ADMIT once the bench has expired; otherwise unchanged."""
    if state.status != BENCHED or state.bench_until is None or now < state.bench_until:
        return state
    new = BoxState.from_dict(state.as_dict())
    new.status = ADMIT
    new.bench_until = None
    return new


def due_for_check(state: BoxState, now: float, interval_s: float = cfg.FULL_CHECK_INTERVAL_S) -> bool:
    """ADMIT is always due; IDLE is due every ``interval_s``; BENCHED is never due (release it first)."""
    if state.status == ADMIT:
        return True
    if state.status == IDLE:
        return state.last_check_at is None or now - state.last_check_at >= interval_s
    return False


class StateStore:
    """All boxes in one JSON file: ``{box_id: BoxState}``. Small enough to rewrite whole."""

    def __init__(self, path):
        self.path = Path(path)
        self.boxes: Dict[str, BoxState] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text() or '{}')
            self.boxes = {k: BoxState.from_dict(v) for k, v in raw.items()}

    def get(self, box_id: str) -> BoxState:
        return self.boxes.get(box_id) or BoxState(box_id)

    def put(self, state: BoxState) -> None:
        self.boxes[state.box_id] = state
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        tmp.write_text(json.dumps({k: v.as_dict() for k, v in sorted(self.boxes.items())}, indent=1))
        tmp.replace(self.path)

    def by_status(self, status: str) -> List[BoxState]:
        return [b for b in self.boxes.values() if b.status == status]
