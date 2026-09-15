# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Per-box and per-card state: ADMIT / IDLE / BENCHED boxes, the UUID pin, the bench backoff ladder, and the card
state machine (IDLE / STARTING / LEASED / DRAINING / CHECKING, ``23`` §4a).

Pure functions over a ``BoxState`` plus a tiny JSON store; the controller is the single writer. A box enters at ADMIT;
its first passing full check pins its UUIDs, moves it to IDLE and makes every pinned card IDLE. Any BENCH verdict
sends the box to BENCHED for the next rung of the ladder (1 h -> 4 h -> 16 h -> 64 h, ``23`` §5), clears the pin and
the cards, and when the bench expires it re-enters through ADMIT. A long clean stretch resets the ladder.

The card, not the box, is the unit of placement (``23`` §4a, §7): a start takes an IDLE card to STARTING, then LEASED
at its first healthy probe (or CHECKING on a failed start); a drain takes LEASED through DRAINING to CHECKING; the
next passing proof returns a CHECKING card to IDLE. The proof round only proves IDLE and CHECKING cards.
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

# Card states. IDLE is shared with the box status of the same name.
STARTING = 'STARTING'
LEASED = 'LEASED'
DRAINING = 'DRAINING'
CHECKING = 'CHECKING'
CARD_STATES = (IDLE, STARTING, LEASED, DRAINING, CHECKING)
PROVABLE = (IDLE, CHECKING)  # cards the proof round stages and fires; the rest host (or are leaving) our workload
BUSY = (STARTING, LEASED, DRAINING)

# Every transition the controller may make. CHECKING -> IDLE belongs to a passing proof (``apply_verdict``), not here.
TRANSITIONS = {
    IDLE: (STARTING,),
    STARTING: (LEASED, DRAINING, CHECKING),  # healthy / stop mid-start / failed start
    LEASED: (DRAINING, CHECKING),  # drain / the container vanished under us
    DRAINING: (CHECKING,),
    CHECKING: (),
}

FAILED_STARTS = 'failed_starts'  # the bench reason when too many starts fail in a row


class CardTransitionError(ValueError):
    """A card transition the state machine does not allow (or a card the box does not have)."""


@dataclass
class CardState:
    state: str = IDLE
    instance_id: str = ''  # the placement instance on this card, while STARTING / LEASED / DRAINING
    since: Optional[float] = None

    @classmethod
    def from_dict(cls, d: dict) -> 'CardState':
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


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
    cards: Dict[str, CardState] = field(default_factory=dict)  # pinned uuid -> card state
    failed_starts: int = 0  # consecutive failed lease starts on this box; FAILED_STARTS_BENCH_AFTER benches it
    # Published port -> the port the outside world reaches it on, for hosts that remap ports (a Lium pod). Empty on a
    # real miner box, where the manifest's port is published as-is.
    port_map: Dict[str, int] = field(default_factory=dict)
    # What the last passing full check saw, for the in-lease heartbeat's "same card?": {'power_limits': {uuid: W},
    # 'nvml_md5': md5}.
    identity: Dict[str, object] = field(default_factory=dict)
    # Dated events WS-E folds into standing: {'at', 'kind', ...}. Kept across a bench.
    standing_events: List[dict] = field(default_factory=list)
    # When a hard in-lease failure (a heartbeat) stopped this box's pay; WS-F withholds the leased accrual from it.
    withheld_from: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'BoxState':
        fields = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        fields['cards'] = {uuid: CardState.from_dict(c) for uuid, c in (fields.get('cards') or {}).items()}
        state = cls(**fields)
        if 'cards' not in d and state.status == IDLE:  # a file from before cards existed: its pinned cards are idle
            state.cards = {uuid: CardState(IDLE, '', state.last_check_at) for uuid in state.pinned_uuids}
        return state

    def card(self, uuid: str) -> CardState:
        return self.cards.get(uuid) or CardState()

    def public_port(self, port: int) -> int:
        return int(self.port_map.get(str(port), port))


def backoff_seconds(bench_count: int, ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S) -> int:
    """Bench length for the ``bench_count``-th bench (1-based); past the last rung it stays at the last rung."""
    if bench_count < 1:
        return 0
    return int(ladder[min(bench_count, len(ladder)) - 1])


def _bench(
    new: BoxState,
    now: float,
    failed: Sequence[str],
    ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S,
    ladder_reset_after_s: float = cfg.BENCH_LADDER_RESET_AFTER_S,
) -> BoxState:
    """Climb the ladder (or restart it after a long clean stretch), clear the pin and the cards, wait it out."""
    if new.benched_at is not None and now - new.benched_at > ladder_reset_after_s:
        new.bench_count = 0
    new.bench_count += 1
    new.status = BENCHED
    new.benched_at = now
    new.bench_until = now + backoff_seconds(new.bench_count, ladder)
    new.last_failed = list(failed)
    new.pinned_uuids = []
    new.admitted_at = None
    new.cards = {}
    new.failed_starts = 0
    return new


def apply_verdict(
    state: BoxState,
    verdict: CheckVerdict,
    now: float,
    ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S,
    ladder_reset_after_s: float = cfg.BENCH_LADDER_RESET_AFTER_S,
    proved: Optional[Sequence[str]] = None,
) -> BoxState:
    """The state after a full check. Pure: returns a new ``BoxState``. A pass at ADMIT makes every pinned card IDLE;
    a pass at IDLE returns CHECKING cards to IDLE and leaves busy cards alone. With ``proved`` (the cards the proof
    actually ran on) only those return: a card that reached CHECKING mid-round was not proved."""
    new = BoxState.from_dict(state.as_dict())
    new.last_check_at = now
    new.last_failed = list(verdict.failed)
    new.unreachable_count = 0
    if verdict.admitted:
        new.identity = identity_baseline(verdict) or new.identity
        if new.status == ADMIT:
            new.pinned_uuids = list(verdict.gpu_uuids)
            new.card_name = verdict.card_name
            new.admitted_at = now
            new.cards = {}
        for uuid in new.pinned_uuids:
            card = new.cards.get(uuid)
            if card is None or (card.state == CHECKING and (proved is None or uuid in proved)):
                new.cards[uuid] = CardState(IDLE, '', now)
        new.status = IDLE
        return new
    return _bench(new, now, verdict.failed, ladder, ladder_reset_after_s)


UNREACHABLE = 'ssh_unreachable'


def apply_unreachable(
    state: BoxState,
    now: float,
    bench_after: int = cfg.UNREACHABLE_BENCH_AFTER,
    bench_s: float = cfg.UNREACHABLE_BENCH_S,
) -> BoxState:
    """The state after a round in which SSH could not reach the box, or an in-lease heartbeat that got no answer (one
    counter for both): no verdict, the count goes up, and at ``bench_after`` in a row the box is BENCHED for a flat
    ``bench_s`` without climbing the fraud ladder. Pure."""
    new = BoxState.from_dict(state.as_dict())
    new.unreachable_count += 1
    if new.unreachable_count >= bench_after and new.status != BENCHED:
        new.status = BENCHED
        new.benched_at = now
        new.bench_until = now + bench_s
        new.last_failed = [UNREACHABLE]
        new.pinned_uuids = []
        new.admitted_at = None
        new.cards = {}
    return new


def mark_reachable(state: BoxState) -> BoxState:
    """A visit that got an answer (an in-lease heartbeat, pass or fail) resets the unreachable count, as a verdict does.
    Pure; the same object when there is nothing to reset."""
    if not state.unreachable_count:
        return state
    new = BoxState.from_dict(state.as_dict())
    new.unreachable_count = 0
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


# ---------------------------------------------------------------- cards ---------------------------------------------


def transition_card(state: BoxState, uuid: str, to: str, now: float, instance_id: Optional[str] = None) -> BoxState:
    """Move one card along ``TRANSITIONS``. Pure. ``instance_id`` is set on the way into STARTING and kept until the
    card reaches CHECKING. Raises ``CardTransitionError`` for a card the box has not pinned or a move not allowed."""
    if state.status != IDLE or uuid not in state.cards:
        raise CardTransitionError(f'{state.box_id}: no card {uuid} on an {state.status} box')
    card = state.cards[uuid]
    if to not in TRANSITIONS.get(card.state, ()):
        raise CardTransitionError(f'{state.box_id} card {uuid}: {card.state} -> {to} is not allowed')
    new = BoxState.from_dict(state.as_dict())
    keep = card.instance_id if to in BUSY else ''
    new.cards[uuid] = CardState(to, instance_id if instance_id is not None else keep, now)
    return new


def record_start(state: BoxState, ok: bool, now: float, bench_after: int = cfg.FAILED_STARTS_BENCH_AFTER) -> BoxState:
    """Count a lease start. A success resets the count; ``bench_after`` failures in a row bench the box on the ladder
    (Kimbo 9/14: a forged idle card must not keep idle pay by never managing to host a model). A single failed start is
    slow, not caught, and changes nothing else (``23`` §4a). Pure."""
    new = BoxState.from_dict(state.as_dict())
    if ok:
        new.failed_starts = 0
        return new
    new.failed_starts += 1
    if new.failed_starts >= bench_after and new.status != BENCHED:
        return _bench(new, now, [FAILED_STARTS])
    return new


def identity_baseline(verdict: CheckVerdict) -> dict:
    """The power limit per card and the NVML library md5 a passing full check saw: the heartbeat's "same card?"
    compares against these. Empty when the verdict carries neither."""
    out: dict = {}
    power = verdict.check('power_limit')
    if power is not None and power.passed:
        out['power_limits'] = {
            r['uuid']: r['limit_w'] for r in power.evidence.get('readings', []) if r.get('limit_w') is not None
        }
    nvml = verdict.check('nvml_digest')
    if nvml is not None and nvml.passed and nvml.evidence.get('md5'):
        out['nvml_md5'] = nvml.evidence['md5']
    return out


HEARTBEAT_FAILED = 'heartbeat_failed'
HEALTH_FAILED = 'health_failed'
# The heartbeat's three questions (``23`` §5), as they are named in a bench reason and in ``instances.json``.
SAME_CARD = 'same_card'
OUR_CONTAINER = 'our_container'
CARD_OURS_ALONE = 'card_ours_alone'


def add_event(state: BoxState, kind: str, now: float, keep: int = cfg.STANDING_EVENTS_KEEP, **detail) -> BoxState:
    """Append a dated standing event (WS-E folds them). Pure."""
    new = BoxState.from_dict(state.as_dict())
    new.standing_events = [*new.standing_events, {'at': now, 'kind': kind, **detail}][-keep:]
    return new


def apply_heartbeat_failure(state: BoxState, failed: Sequence[str], now: float, **detail) -> BoxState:
    """A failed in-lease heartbeat (``23`` §4a, §5): BENCHED on the fraud ladder, pay withheld from ``now``, and a
    ``heartbeat_failed`` standing event. Pure. A box already benched keeps its bench and only gains the event."""
    new = add_event(state, HEARTBEAT_FAILED, now, failed=list(failed), **detail)
    new.withheld_from = now
    new.unreachable_count = 0  # the box answered
    if new.status == BENCHED:
        return new
    return _bench(new, now, [f'heartbeat:{name}' for name in failed])


def provable_uuids(state: BoxState, reported: Sequence[str]) -> List[str]:
    """The reported cards the proof may run on this round: every card of a box at ADMIT; on an IDLE box, cards in IDLE
    or CHECKING plus any card that is not pinned (it fails the UUID pin anyway). A STARTING, LEASED or DRAINING card
    is skipped: it hosts, or is leaving, our workload."""
    if state.status != IDLE:
        return list(reported)
    return [uuid for uuid in reported if uuid not in state.cards or state.cards[uuid].state in PROVABLE]


OPERATOR_FIELDS = ('host', 'port', 'host_key', 'port_map')  # what `gitt controller admit` writes


class StateStore:
    """All boxes in one JSON file: ``{box_id: BoxState}``. Small enough to rewrite whole.

    The controller holds this in memory for as long as it runs, while an operator may ``admit`` beside it. So a save
    that finds the file changed since this store last read or wrote it merges first: boxes it does not know are
    added, and the operator's fields of the ones it does are taken from disk. Card and bench state stay the
    controller's."""

    def __init__(self, path):
        self.path = Path(path)
        self.boxes: Dict[str, BoxState] = {}
        self._mtime_ns = 0
        if self.path.exists():
            self.boxes = self._read()

    def _read(self) -> Dict[str, BoxState]:
        self._mtime_ns = self.path.stat().st_mtime_ns
        raw = json.loads(self.path.read_text() or '{}')
        return {k: BoxState.from_dict(v) for k, v in raw.items()}

    def get(self, box_id: str) -> BoxState:
        return self.boxes.get(box_id) or BoxState(box_id)

    def put(self, state: BoxState) -> None:
        self.boxes[state.box_id] = state
        self.save()

    def merge_from_disk(self) -> List[str]:
        """Pick up what someone else wrote since our last read or write. Returns the box ids added or changed."""
        if not self.path.exists() or self.path.stat().st_mtime_ns == self._mtime_ns:
            return []
        changed = []
        for box_id, theirs in self._read().items():
            ours = self.boxes.get(box_id)
            if ours is None:
                self.boxes[box_id] = theirs
                changed.append(box_id)
            elif any(getattr(ours, f) != getattr(theirs, f) for f in OPERATOR_FIELDS):
                for f in OPERATOR_FIELDS:
                    setattr(ours, f, getattr(theirs, f))
                changed.append(box_id)
        return changed

    def save(self) -> None:
        self.merge_from_disk()
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        tmp.write_text(json.dumps({k: v.as_dict() for k, v in sorted(self.boxes.items())}, indent=1))
        tmp.replace(self.path)
        self._mtime_ns = self.path.stat().st_mtime_ns

    def by_status(self, status: str) -> List[BoxState]:
        return [b for b in self.boxes.values() if b.status == status]
