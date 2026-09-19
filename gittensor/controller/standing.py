# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Standing: probation -> standard -> trusted (vault ``24`` §3 WS-E, ``23`` §5).

A box's standing is a **pure fold of its dated events** (``BoxState.standing_events``), with no state of its own: one
number, the clean lease-seconds since the last reset, walked through the events in time order.

* ``clean_lease`` (every normal drain; ``leased_s`` = that lease's time LEASED) adds its seconds.
* A **hard** event resets to zero, i.e. probation: a failed heartbeat, a failed full check or GPU proof, an operator
  releasing the box from a bench (not a ``forgiven`` release: that bench was our fault, and it is neutral).
* A **soft** event drops one level: a health-probe replacement, a failed start (a missed ``max_load_s``, a missing
  pre-staged image), a failed drain, an unreachable bench, a check that could not be carried out (``check_not_run``:
  the proof container would not start). Trusted falls to the start of standard, standard to zero.
* ``folded`` is what ``add_event`` leaves when it trims the oldest events: their fold, as a clean-seconds value.
* Neutral, recorded but not folded: ``instance_stopped`` (our container gone after the agent was unreachable: a
  clean leave or a reboot, Kimbo 9/16) and ``instance_unreachable`` (a lease ended by missed heartbeats).

The level is ``trusted`` from ``STANDING_TRUSTED_AFTER_S``, ``standard`` from ``STANDING_STANDARD_AFTER_S``, probation
below (and for a new box with no events). Standing sets lease priority (``rank``) and lease length (``lease_cap_s``).
Unknown kinds are ignored, so a newer controller's events never break an older fold.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping

from gittensor.controller.checks import config as cfg

PROBATION = 'probation'
STANDARD = 'standard'
TRUSTED = 'trusted'
LEVELS = (PROBATION, STANDARD, TRUSTED)

CLEAN_LEASE = 'clean_lease'
START_FAILED = 'start_failed'
DRAIN_FAILED = 'drain_failed'
CHECK_FAILED = 'check_failed'
UNREACHABLE_BENCHED = 'unreachable_benched'
RELEASED = 'released'
FOLDED = 'folded'
# Kinds written in checks/state.py (the heartbeat and the health replacement), named here only as strings: state.py
# imports this module to fold trimmed events.
HARD = frozenset({'heartbeat_failed', CHECK_FAILED, RELEASED})
SOFT = frozenset({'health_failed', START_FAILED, DRAIN_FAILED, UNREACHABLE_BENCHED, 'check_not_run'})


def level_of(clean_s: float, standard_after_s: float, trusted_after_s: float) -> str:
    if clean_s >= trusted_after_s:
        return TRUSTED
    if clean_s >= standard_after_s:
        return STANDARD
    return PROBATION


def clean_seconds(
    events: Iterable[Mapping],
    now: float | None = None,
    standard_after_s: float = cfg.STANDING_STANDARD_AFTER_S,
    trusted_after_s: float = cfg.STANDING_TRUSTED_AFTER_S,
) -> float:
    """The fold. Events dated after ``now`` (when given) do not count yet."""
    clean = 0.0
    for event in sorted(events, key=lambda e: float(e.get('at') or 0.0)):
        if now is not None and float(event.get('at') or 0.0) > now:
            break
        kind = event.get('kind')
        if kind == FOLDED:
            clean = max(0.0, float(event.get('clean_s') or 0.0))
        elif kind == CLEAN_LEASE:
            clean += max(0.0, float(event.get('leased_s') or 0.0))
        elif kind == RELEASED and event.get('forgiven'):
            continue
        elif kind in HARD:
            clean = 0.0
        elif kind in SOFT:
            level = level_of(clean, standard_after_s, trusted_after_s)
            clean = standard_after_s if level == TRUSTED else 0.0
    return clean


def standing(
    events: Iterable[Mapping],
    now: float | None = None,
    standard_after_s: float = cfg.STANDING_STANDARD_AFTER_S,
    trusted_after_s: float = cfg.STANDING_TRUSTED_AFTER_S,
) -> str:
    return level_of(clean_seconds(events, now, standard_after_s, trusted_after_s), standard_after_s, trusted_after_s)


def rank(level: str) -> int:
    """Lease priority: higher leases first (trusted 2, standard 1, probation 0)."""
    return LEVELS.index(level) if level in LEVELS else 0


def lease_cap_s(
    level: str,
    rng: random.Random,
    base_s: float = cfg.LEASE_CAP_S,
    jitter: float = cfg.LEASE_CAP_JITTER,
    multipliers: Mapping[str, float] = cfg.LEASE_CAP_MULTIPLIER,
) -> float:
    """A lease's cap: short on probation, longer once trusted, jittered so the exit cannot be predicted."""
    return base_s * float(multipliers.get(level, 1.0)) * rng.uniform(1.0 - jitter, 1.0 + jitter)


def fold_into_one(events: list[dict]) -> dict:
    """The one ``folded`` event that stands for ``events`` (the oldest, trimmed off): it restores their clean seconds
    exactly, so trimming never changes a box's standing."""
    return {
        'at': max((float(e.get('at') or 0.0) for e in events), default=0.0),
        'kind': FOLDED,
        'clean_s': clean_seconds(events),
        'events': sum(int(e.get('events') or 1) for e in events),
    }
