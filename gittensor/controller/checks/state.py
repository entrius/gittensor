# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Per-box and per-card state: ADMIT / IDLE / BENCHED boxes, the UUID pin, the bench backoff ladder, and the card
state machine (IDLE / STARTING / LEASED / DRAINING / CHECKING, ``23`` §4a).

Pure functions over a ``BoxState`` plus a tiny JSON store; the controller is the single writer. A box enters at ADMIT;
its first passing full check pins its UUIDs, moves it to IDLE and makes every pinned card IDLE. Any BENCH verdict
sends the box to BENCHED for the next rung of the ladder (1 h -> 4 h -> 16 h -> 64 h, ``23`` §5), clears the pin and
the cards, and when the bench expires it re-enters through ADMIT. Clean time steps the ladder back down
(``ladder_rung``). A check that could not be carried out is a strike, not a bench (``apply_not_run``).

The card, not the box, is the unit of placement (``23`` §4a, §7): a start takes an IDLE card to STARTING, then LEASED
at its first healthy probe (or CHECKING on a failed start); a drain takes LEASED through DRAINING to CHECKING; the
next passing proof returns a CHECKING card to IDLE. The proof round only proves IDLE and CHECKING cards.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from gittensor.agent.config import WORKLOAD_PORT_RANGE
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks import why as w
from gittensor.controller.checks.verdict import NOT_RUN, CheckVerdict

ADMIT = 'ADMIT'
IDLE = 'IDLE'
BENCHED = 'BENCHED'

# Card states. IDLE is shared with the box status of the same name.
STARTING = 'STARTING'
LEASED = 'LEASED'
DRAINING = 'DRAINING'
CHECKING = 'CHECKING'
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

# Standing events (folded by gittensor/controller/standing.py, which names the same strings).
CHECK_FAILED = 'check_failed'
UNREACHABLE_BENCHED = 'unreachable_benched'
START_FAILED = 'start_failed'
DRAIN_FAILED = 'drain_failed'
CLEAN_LEASE = 'clean_lease'
CHECK_NOT_RUN = 'check_not_run'


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
    bench_count: int = 0  # rungs climbed as of the last bench; ``ladder_rung`` is what clean time has left of it
    benched_at: Optional[float] = None
    bench_until: Optional[float] = None
    last_check_at: Optional[float] = None
    last_failed: List[str] = field(default_factory=list)
    # Per failed check name, the phrase the public fleet page shows for it, already rendered (``checks/why.py``):
    # one place decides what is public, and ``publish.py`` only has to copy it. Written wherever ``last_failed`` is.
    # Files written before this field existed load with it empty; the page then names the check, as it used to.
    last_failed_why: Dict[str, str] = field(default_factory=dict)
    admitted_at: Optional[float] = None
    unreachable_count: int = 0  # consecutive rounds with no verdict because SSH failed; reset by any verdict
    # Consecutive checks that could not be carried out (``apply_not_run``), and when the last one was. A pass or a
    # bench starts the count over.
    not_run_count: int = 0
    not_run_at: Optional[float] = None
    # The clean clock behind ``ladder_rung`` runs from ``admitted_at``. It stops while the box is unreachable or has a
    # strike: ``clean_paused_at`` is when it stopped, ``clean_paused_s`` the stops already over.
    clean_paused_at: Optional[float] = None
    clean_paused_s: float = 0.0
    # Where the box's agent sshd answers, and its host key pinned at admission (``gitt controller admit``). Files
    # written before these fields existed load with the defaults.
    host: str = ''
    port: int = 0
    host_key: str = ''
    cards: Dict[str, CardState] = field(default_factory=dict)  # pinned uuid -> card state
    failed_starts: int = 0  # consecutive failed lease starts on this box; FAILED_STARTS_BENCH_AFTER benches it
    # Host port -> the port a remapping host (a Lium pod) shows it as, applied to an instance record's ``port``. Only a
    # public-bind instance under the gateway's --allow-direct is addressed by it; empty on a real miner box.
    port_map: Dict[str, int] = field(default_factory=dict)
    # [low, high] host ports instances are given, inclusive. Empty: the range every `gitt up` box keeps free
    # (``WORKLOAD_PORT_RANGE``); a dev box whose provider has other ports free sets it at admit.
    workload_ports: List[int] = field(default_factory=list)
    # Who put the box here: 'chain' (discovery read its endpoint off the metagraph and removes it when the hotkey
    # deregisters) or 'operator' (`gitt controller admit`; discovery leaves it alone). '' is a file from before this.
    source: str = ''
    # The endpoint the chain now publishes when it differs from host:port and the host key there is not the pinned one
    # (or did not answer): {'host', 'port', 'host_key', 'at'}. Not re-pinned silently; every round counts it as an
    # unreachable round until `gitt controller admit --force-rekey` or the pinned key answers at the new address.
    endpoint_changed: Dict[str, object] = field(default_factory=dict)
    # What the last passing full check saw, for the in-lease heartbeat's "same card?": {'power_limits': {uuid: W},
    # 'nvml_md5': md5}.
    identity: Dict[str, object] = field(default_factory=dict)
    # Dated events WS-E folds into standing: {'at', 'kind', ...}. Kept across a bench.
    standing_events: List[dict] = field(default_factory=list)
    # When a hard in-lease failure (a heartbeat) stopped this box's pay; WS-F withholds the leased accrual from it.
    withheld_from: Optional[float] = None
    # `gitt controller release`: {'at', 'reason'}. An operator field (merged from disk beside a running controller);
    # ``release_from_bench`` honours it for a bench that began before it.
    release_request: Dict[str, object] = field(default_factory=dict)
    # `gitt controller remove`: {'at', 'reason'}. An operator field like the release request; the controller drops the
    # box on its next pass once nothing runs on it (``remove_requested``), the one-shot at once.
    remove_request: Dict[str, object] = field(default_factory=dict)
    # The hotkey's UID on the subnet, as discovery last read it off the metagraph (any box, whoever admitted it); None
    # when the hotkey is not registered, or discovery has not run. Only shown (status, the public document).
    uid: Optional[int] = None

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

    def workload_port_range(self) -> range:
        low, high = self.workload_ports if len(self.workload_ports) == 2 else WORKLOAD_PORT_RANGE
        return range(int(low), int(high) + 1)


def backoff_seconds(bench_count: int, ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S) -> int:
    """Bench length for the ``bench_count``-th bench (1-based); past the last rung it stays at the last rung."""
    if bench_count < 1:
        return 0
    return int(ladder[min(bench_count, len(ladder)) - 1])


def clean_seconds(state: BoxState, now: float) -> float:
    """How long the box has been admitted and answering since its last bench, leased or idle. Zero on a box that is not
    IDLE; the time it spent unreachable or on a strike does not count."""
    if state.status != IDLE or state.admitted_at is None:
        return 0.0
    until = state.clean_paused_at if state.clean_paused_at is not None else now
    return max(0.0, until - state.admitted_at - state.clean_paused_s)


def ladder_rung(
    state: BoxState,
    now: float,
    step_down_s: float = cfg.BENCH_LADDER_STEP_DOWN_S,
    clean_slate_s: float = cfg.BENCH_LADDER_CLEAN_SLATE_S,
) -> int:
    """The rungs the box still stands on: ``bench_count`` less one per ``step_down_s`` of clean time, none at all from
    ``clean_slate_s`` (Kimbo 9/19). The next bench is rung ``ladder_rung + 1``."""
    clean = clean_seconds(state, now)
    if clean >= clean_slate_s:
        return 0
    return max(0, state.bench_count - int(clean // step_down_s))


def _pause_clean(new: BoxState, now: float) -> None:
    if new.clean_paused_at is None and new.status == IDLE:
        new.clean_paused_at = now


def _resume_clean(new: BoxState, now: float) -> None:
    if new.clean_paused_at is not None and not new.unreachable_count and not new.not_run_count:
        new.clean_paused_s += max(0.0, now - new.clean_paused_at)
        new.clean_paused_at = None


def _bench(
    new: BoxState,
    now: float,
    failed: Sequence[str],
    ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S,
    floor: int = 0,
    why: Optional[Mapping[str, str]] = None,
) -> BoxState:
    """Climb the ladder from the rung clean time has left, clear the pin and the cards, wait it out. With ``floor``
    the rung is at least that before it climbs, whatever it was. ``why`` is the public phrase per failed name for a
    bench that has evidence to classify (a check verdict); without it the name alone picks the phrase, which is all a
    heartbeat bench, an unreachable box or too many failed starts has to say anyway."""
    new.bench_count = max(ladder_rung(new, now), floor) + 1
    new.status = BENCHED
    new.benched_at = now
    new.bench_until = now + backoff_seconds(new.bench_count, ladder)
    _set_failed(new, failed, why)
    new.pinned_uuids = []
    new.admitted_at = None
    new.cards = {}
    new.failed_starts = 0
    new.not_run_count = 0
    new.not_run_at = None
    new.clean_paused_at = None
    new.clean_paused_s = 0.0
    return new


def _set_failed(new: BoxState, failed: Sequence[str], why: Optional[Mapping[str, str]] = None) -> None:
    """``last_failed`` and its public phrases together — they are one fact and must never drift apart. A name with
    no phrase is simply absent from the map; the page names the check for it."""
    new.last_failed = list(failed)
    rendered = dict(why) if why is not None else w.for_names(list(failed))
    new.last_failed_why = {name: rendered[name] for name in new.last_failed if rendered.get(name)}


def apply_verdict(
    state: BoxState,
    verdict: CheckVerdict,
    now: float,
    ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S,
    proved: Optional[Sequence[str]] = None,
) -> BoxState:
    """The state after a full check. Pure: returns a new ``BoxState``. A pass at ADMIT makes every pinned card IDLE;
    a pass at IDLE returns CHECKING cards to IDLE and leaves busy cards alone. With ``proved`` (the cards the proof
    actually ran on) only those return: a card that reached CHECKING mid-round was not proved. A verdict with nothing
    failed and a check that could not run is a strike (``apply_not_run``)."""
    why = w.from_results(verdict.checks)
    if verdict.verdict == NOT_RUN:
        return apply_not_run(state, verdict.not_run, now, ladder=ladder, proved=proved, why=why)
    new = BoxState.from_dict(state.as_dict())
    new.last_check_at = now
    _set_failed(new, verdict.failed, why)
    new.unreachable_count = 0
    if verdict.admitted:
        new.identity = identity_baseline(verdict) or new.identity
        new.not_run_count = 0
        new.not_run_at = None
        if new.status == ADMIT:
            new.pinned_uuids = list(verdict.gpu_uuids)
            new.card_name = verdict.card_name
            new.admitted_at = now
            new.clean_paused_at = None
            new.clean_paused_s = 0.0
            new.cards = {}
        _resume_clean(new, now)
        for uuid in new.pinned_uuids:
            card = new.cards.get(uuid)
            if card is None or (card.state == CHECKING and (proved is None or uuid in proved)):
                new.cards[uuid] = CardState(IDLE, '', now)
        new.status = IDLE
        return new
    new = add_event(new, CHECK_FAILED, now, failed=list(verdict.failed))
    return _bench(new, now, verdict.failed, ladder, why=why)


def apply_not_run(
    state: BoxState,
    not_run: Sequence[str],
    now: float,
    bench_after: int = cfg.COULD_NOT_RUN_BENCH_AFTER,
    ladder: Sequence[int] = cfg.BENCH_BACKOFF_LADDER_S,
    proved: Optional[Sequence[str]] = None,
    why: Optional[Mapping[str, str]] = None,
) -> BoxState:
    """The state after a check that could not be carried out (Kimbo 9/19): a strike. No answer was judged, so it is no
    caught cheat, and no proof either: ``last_check_at`` does not move, and the cards the proof was for (``proved``;
    every IDLE card without it) go to CHECKING, unpaid and not leasable until a proof passes. Busy cards keep their
    lease. A box at ADMIT stays there. ``bench_after`` strikes in a row are a failed check on the ladder: a box must not
    dodge a proof by breaking its own container runtime. Pure."""
    new = BoxState.from_dict(state.as_dict())
    new.unreachable_count = 0  # the box answered
    new.not_run_count += 1
    new.not_run_at = now
    if new.not_run_count >= bench_after:
        new = add_event(new, CHECK_FAILED, now, failed=list(not_run), not_run_rounds=new.not_run_count)
        return _bench(new, now, list(not_run), ladder, why=why)
    new = add_event(new, CHECK_NOT_RUN, now, not_run=list(not_run), strike=new.not_run_count)
    _pause_clean(new, now)
    for uuid, card in new.cards.items():
        if card.state == IDLE and (proved is None or uuid in proved):
            new.cards[uuid] = CardState(CHECKING, '', now)
    return new


def not_run_retry_at(state: BoxState, retry_s: float = cfg.COULD_NOT_RUN_RETRY_S) -> Optional[float]:
    """When a box with a strike may be proved again (one try a round, so three strikes take three rounds); None for a
    box with no strike."""
    if not state.not_run_count or state.not_run_at is None:
        return None
    return state.not_run_at + retry_s


UNREACHABLE = 'ssh_unreachable'


def apply_unreachable(
    state: BoxState,
    now: float,
    bench_after: int = cfg.UNREACHABLE_BENCH_AFTER,
    bench_s: float = cfg.UNREACHABLE_BENCH_S,
) -> BoxState:
    """The state after a proof round in which SSH could not reach the box: no verdict, the count goes up, and at
    ``bench_after`` in a row the box is BENCHED for a flat ``bench_s`` without climbing the fraud ladder. A missed
    in-lease heartbeat is counted on the instance instead (``heartbeat.py``; Kimbo 9/16). Pure."""
    new = BoxState.from_dict(state.as_dict())
    new.unreachable_count += 1
    _pause_clean(new, now)
    if new.unreachable_count >= bench_after and new.status != BENCHED:
        new = add_event(new, UNREACHABLE_BENCHED, now, rounds=new.unreachable_count)
        new.status = BENCHED
        new.benched_at = now
        new.bench_until = now + bench_s
        _set_failed(new, [UNREACHABLE])
        new.pinned_uuids = []
        new.admitted_at = None
        new.cards = {}
        new.not_run_count = 0
        new.not_run_at = None
        new.clean_paused_at = None
        new.clean_paused_s = 0.0
    return new


def mark_reachable(state: BoxState, now: Optional[float] = None) -> BoxState:
    """A visit that got an answer (an in-lease heartbeat, pass or fail) resets the unreachable count, as a verdict does,
    and with ``now`` starts the clean clock again. Pure; the same object when there is nothing to reset."""
    if not state.unreachable_count:
        return state
    new = BoxState.from_dict(state.as_dict())
    new.unreachable_count = 0
    if now is not None:
        _resume_clean(new, now)
    return new


RELEASED = 'released'  # the standing event of an operator ending a bench early
HEARTBEAT_FAILED = 'heartbeat_failed'
_BENCH_EVENTS = (CHECK_FAILED, HEARTBEAT_FAILED, UNREACHABLE_BENCHED)  # what a bench writes at ``benched_at``


def request_release(state: BoxState, now: float, reason: str, forgive: bool = False) -> BoxState:
    """``gitt controller release``: ask for this bench to end now; ``forgive`` also takes it off the box's record (the
    fault was ours). Pure; ``release_from_bench`` applies it."""
    new = BoxState.from_dict(state.as_dict())
    new.release_request = {'at': now, 'reason': reason, **({'forgive': True} if forgive else {})}
    return new


def release_requested(state: BoxState) -> bool:
    """A BENCHED box an operator released after its bench began (a request from an earlier bench does not count)."""
    at = state.release_request.get('at')
    return state.status == BENCHED and isinstance(at, (int, float)) and at >= (state.benched_at or 0.0)


def request_remove(state: BoxState, now: float, reason: str) -> BoxState:
    """``gitt controller remove``: ask for this box to be forgotten. Pure; whoever holds the state applies it."""
    new = BoxState.from_dict(state.as_dict())
    new.remove_request = {'at': now, 'reason': reason}
    return new


def remove_requested(state: BoxState) -> bool:
    return isinstance(state.remove_request.get('at'), (int, float))


def release_from_bench(state: BoxState, now: float) -> BoxState:
    """BENCHED -> ADMIT once the bench has expired, or at once when an operator released it (Kimbo 9/15: a ``released``
    standing event with the reason; the ladder rung stays). An operator's release also clears ``withheld_from`` (Kimbo
    9/16: the operator has judged the bench wrong or the test over, so the leased pay withheld over the box's UTC day
    +-1 is given back; the ledger is append-only and ``settle_window`` recomputes from the current field); the event
    records what was cleared. A release with ``forgive`` (Kimbo 9/19: the bench was our fault) also gives the rung
    back, drops the standing event the bench wrote, and is itself neutral to standing. An expired bench keeps its
    withheld window. Otherwise unchanged."""
    early = release_requested(state)
    if state.status != BENCHED or (not early and (state.bench_until is None or now < state.bench_until)):
        return state
    new = BoxState.from_dict(state.as_dict())
    new.status = ADMIT
    new.bench_until = None
    if early:
        request = state.release_request
        forgiven = bool(request.get('forgive'))
        if forgiven:
            if state.last_failed not in ([UNREACHABLE], [DEREGISTERED]):  # those benches never climbed the ladder
                new.bench_count = max(0, new.bench_count - 1)
            new.standing_events = [
                e
                for e in new.standing_events
                if not (e.get('at') == state.benched_at and e.get('kind') in _BENCH_EVENTS)
            ]
        new = add_event(
            new,
            RELEASED,
            now,
            reason=request.get('reason', ''),
            requested_at=request['at'],
            bench_until=state.bench_until,
            withheld_from=state.withheld_from,  # what the release gave back (None: nothing was withheld)
            **({'forgiven': True} if forgiven else {}),
        )
        new.withheld_from = None
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


def record_start(
    state: BoxState, ok: bool, now: float, bench_after: int = cfg.FAILED_STARTS_BENCH_AFTER, **detail
) -> BoxState:
    """Count a lease start. A success resets the count; ``bench_after`` failures in a row bench the box on the ladder
    (Kimbo 9/14: a forged idle card must not keep idle pay by never managing to host a model). A single failed start is
    slow, not caught: a ``start_failed`` standing event and nothing else (``23`` §4a). Pure."""
    new = BoxState.from_dict(state.as_dict())
    if ok:
        new.failed_starts = 0
        return new
    new = add_event(new, START_FAILED, now, **detail)
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


HEALTH_FAILED = 'health_failed'
# The heartbeat's three questions (``23`` §5), as they are named in a bench reason and in ``instances.json``.
SAME_CARD = 'same_card'
OUR_CONTAINER = 'our_container'
CARD_OURS_ALONE = 'card_ours_alone'


def add_event(state: BoxState, kind: str, now: float, keep: int = cfg.STANDING_EVENTS_KEEP, **detail) -> BoxState:
    """Append a dated standing event (WS-E folds them). Past ``keep`` the oldest are replaced by one ``folded`` event
    that carries their fold, so trimming never changes the box's standing. Pure."""
    from gittensor.controller.standing import fold_into_one  # standing.py imports config only; no cycle at load

    new = BoxState.from_dict(state.as_dict())
    events = [*new.standing_events, {'at': now, 'kind': kind, **detail}]
    if len(events) > keep:
        cut = len(events) - keep + 1
        events = [fold_into_one(events[:cut]), *events[cut:]]
    new.standing_events = events
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


INSTANCE_STOPPED = 'instance_stopped'
INSTANCE_UNREACHABLE = 'instance_unreachable'


def _end_lease(state: BoxState, kind: str, uuid: str, now: float, **detail) -> BoxState:
    new = add_event(state, kind, now, uuid=uuid, **detail)
    if new.status == IDLE and uuid in new.cards and new.cards[uuid].state in BUSY:
        new = transition_card(new, uuid, CHECKING, now)
    return new


def apply_instance_stopped(state: BoxState, uuid: str, now: float, **detail) -> BoxState:
    """A lease that ended because our container was gone once the agent answered again after being unreachable
    (Kimbo 9/16: a clean leave, a reboot, not a cheat): an ``instance_stopped`` standing event (neutral: the fold
    neither resets nor drops standing on it), the card to CHECKING for the one-box probe, nothing withheld, no bench.
    A container that vanishes while the agent answered throughout stays ``apply_heartbeat_failure``. Pure."""
    return _end_lease(state, INSTANCE_STOPPED, uuid, now, **detail)


def apply_instance_unreachable(state: BoxState, uuid: str, now: float, **detail) -> BoxState:
    """A lease that ended because the heartbeat could not reach the box ``HEARTBEAT_UNREACHABLE_AFTER`` times in a row
    (Kimbo 9/16): an ``instance_unreachable`` standing event (neutral), the card to CHECKING (the reconciler undeploys
    the instance when the box answers again, then the one-box probe re-proves the card), nothing withheld, no bench.
    Pure."""
    return _end_lease(state, INSTANCE_UNREACHABLE, uuid, now, **detail)


EXTERNAL_USE = 'external_use'


def external_uses(state: BoxState, now: float, window_s: float = cfg.EXTERNAL_USE_WINDOW_S) -> List[dict]:
    """The box's ``external_use`` events inside the window ending at ``now``."""
    return [
        e
        for e in state.standing_events
        if e.get('kind') == EXTERNAL_USE and now - window_s < float(e.get('at') or 0.0) <= now
    ]


def apply_external_use(
    state: BoxState,
    uuid: str,
    now: float,
    bench_after: int = cfg.EXTERNAL_USE_BENCH_AFTER,
    window_s: float = cfg.EXTERNAL_USE_WINDOW_S,
    floor: int = cfg.EXTERNAL_USE_BENCH_RUNG,
    **detail,
) -> BoxState:
    """A detection by the lease accounting check (Kimbo 9/18): one ``external_use`` SOFT standing event with the
    reason and the numbers. The card stays LEASED here: the caller drains it through the planned drain, and it comes
    back IDLE after the usual re-proof. The ``bench_after``-th inside ``window_s`` is a bench with the failed reason
    ``external_use`` that enters the ladder no lower than ``floor`` + 1. Nothing withheld either way. Pure."""
    new = add_event(state, EXTERNAL_USE, now, uuid=uuid, reason=cfg.EXTERNAL_USE_REASON, **detail)
    if new.status != IDLE or len(external_uses(new, now, window_s)) < bench_after:
        return new
    return _bench(new, now, [EXTERNAL_USE], floor=floor)


def lease_cooldown_until(state: BoxState, cooldown_s: float = cfg.EXTERNAL_USE_COOLDOWN_S) -> Optional[float]:
    """Until when the box takes no new lease after its last ``external_use`` event; None when it never had one."""
    ats = [float(e.get('at') or 0.0) for e in state.standing_events if e.get('kind') == EXTERNAL_USE]
    return max(ats) + cooldown_s if ats else None


def provable_uuids(state: BoxState, reported: Sequence[str]) -> List[str]:
    """The reported cards the proof may run on this round: every card of a box at ADMIT; on an IDLE box, cards in IDLE
    or CHECKING plus any card that is not pinned (it fails the UUID pin anyway). A STARTING, LEASED or DRAINING card
    is skipped: it hosts, or is leaving, our workload."""
    if state.status != IDLE:
        return list(reported)
    return [uuid for uuid in reported if uuid not in state.cards or state.cards[uuid].state in PROVABLE]


# What `gitt controller admit`, `release` and discovery write.
OPERATOR_FIELDS = (
    'host',
    'port',
    'host_key',
    'port_map',
    'release_request',
    'remove_request',
    'workload_ports',
    'source',
    'endpoint_changed',
)


DEREGISTERED = 'deregistered'


def apply_deregistered(state: BoxState, now: float) -> BoxState:
    """The hotkey left the metagraph: BENCHED with no end (``bench_until`` None never releases), cards cleared, so the
    reconciler drains what runs there; discovery removes the box once nothing is left on it. Off the fraud ladder. Pure.
    """
    new = BoxState.from_dict(state.as_dict())
    new.status = BENCHED
    new.benched_at = now
    new.bench_until = None
    _set_failed(new, [DEREGISTERED])
    new.pinned_uuids = []
    new.admitted_at = None
    new.cards = {}
    return new


class StateStore:
    """All boxes in one JSON file: ``{box_id: BoxState}``. Small enough to rewrite whole.

    The controller holds this in memory for as long as it runs, while an operator may ``admit`` beside it. So a save
    that finds the file changed since this store last read or wrote it merges first: boxes it does not know are
    added, and the operator's fields of the ones it does are taken from disk. Card and bench state stay the
    controller's. A box this store removed is not merged back from an older file."""

    def __init__(self, path):
        self.path = Path(path)
        self.boxes: Dict[str, BoxState] = {}
        self._mtime_ns = 0
        self._removed: set = set()
        if self.path.exists():
            self.boxes = self._read()

    def _read(self) -> Dict[str, BoxState]:
        self._mtime_ns = self.path.stat().st_mtime_ns
        raw = json.loads(self.path.read_text() or '{}')
        return {k: BoxState.from_dict(v) for k, v in raw.items()}

    def get(self, box_id: str) -> BoxState:
        return self.boxes.get(box_id) or BoxState(box_id)

    def put(self, state: BoxState) -> None:
        self._removed.discard(state.box_id)
        self.boxes[state.box_id] = state
        self.save()

    def remove(self, box_id: str) -> None:
        if self.boxes.pop(box_id, None) is not None:
            self._removed.add(box_id)
            self.save()

    def merge_from_disk(self) -> List[str]:
        """Pick up what someone else wrote since our last read or write. Returns the box ids added or changed."""
        if not self.path.exists() or self.path.stat().st_mtime_ns == self._mtime_ns:
            return []
        changed = []
        for box_id, theirs in self._read().items():
            ours = self.boxes.get(box_id)
            if box_id in self._removed:
                continue
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
