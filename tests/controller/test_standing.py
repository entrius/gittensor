# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Standing from event histories: clean lease time raises it, a hard failure resets it to probation, a soft one drops it
one level, trimming the event list never changes it, and it sets the lease cap. Plus the events the state functions now
record (a failed full check, an unreachable bench, a failed start)."""

import random

import pytest

from gittensor.controller.checks.state import (
    BENCHED,
    CHECK_FAILED,
    IDLE,
    START_FAILED,
    UNREACHABLE_BENCHED,
    BoxState,
    add_event,
    apply_unreachable,
    apply_verdict,
    record_start,
)
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.standing import (
    CLEAN_LEASE,
    FOLDED,
    PROBATION,
    STANDARD,
    TRUSTED,
    clean_seconds,
    lease_cap_s,
    rank,
    standing,
)

H = 3_600.0
N, M = 6 * H, 48 * H


def lease(at, hours):
    return {'at': at, 'kind': CLEAN_LEASE, 'leased_s': hours * H}


def test_a_new_box_is_on_probation_and_clean_lease_hours_raise_it():
    assert standing([]) == PROBATION
    events = [lease(i * H, 1) for i in range(1, 6)]
    assert standing(events) == PROBATION  # 5 h
    events.append(lease(7 * H, 1))
    assert standing(events) == STANDARD  # 6 h = N
    events += [lease((8 + i) * H, 1) for i in range(42)]
    assert clean_seconds(events) == M and standing(events) == TRUSTED


@pytest.mark.parametrize('hard', ['heartbeat_failed', 'check_failed', 'released'])
def test_a_hard_failure_resets_to_probation(hard):
    events = [lease(1, 50), {'at': 2, 'kind': hard}]
    assert standing(events) == PROBATION and clean_seconds(events) == 0
    assert standing([*events, lease(3, 6)]) == STANDARD  # climbs again from zero


@pytest.mark.parametrize('soft', ['health_failed', 'start_failed', 'drain_failed', 'unreachable_benched'])
def test_a_soft_failure_drops_one_level(soft):
    assert standing([lease(1, 50), {'at': 2, 'kind': soft}]) == STANDARD
    assert clean_seconds([lease(1, 50), {'at': 2, 'kind': soft}]) == N  # the start of standard, not a reset
    assert standing([lease(1, 10), {'at': 2, 'kind': soft}]) == PROBATION
    assert standing([lease(1, 3), {'at': 2, 'kind': soft}, lease(3, 5)]) == PROBATION  # 5 h after the drop


def test_order_is_by_date_future_events_wait_and_unknown_kinds_are_ignored():
    events = [{'at': 5, 'kind': 'heartbeat_failed'}, lease(1, 50), {'at': 3, 'kind': 'something_new'}]
    assert standing(events) == PROBATION  # the failure at t=5 comes after the lease at t=1
    assert standing(events, now=4) == TRUSTED


def test_trimming_the_event_list_folds_the_oldest_and_never_changes_standing():
    box = BoxState('hk')
    rng = random.Random(1)
    kinds = [CLEAN_LEASE] * 8 + ['health_failed', 'start_failed', 'heartbeat_failed']
    for i in range(400):
        kind = rng.choice(kinds)
        box = add_event(box, kind, float(i), keep=20, **({'leased_s': 3 * H} if kind == CLEAN_LEASE else {}))
    twin = BoxState('hk')  # the same history, never trimmed
    rng = random.Random(1)
    for i in range(400):
        kind = rng.choice(kinds)
        twin = add_event(twin, kind, float(i), keep=10_000, **({'leased_s': 3 * H} if kind == CLEAN_LEASE else {}))
    assert len(box.standing_events) == 20 and box.standing_events[0]['kind'] == FOLDED
    assert box.standing_events[0]['events'] + 19 == 400
    assert clean_seconds(box.standing_events) == clean_seconds(twin.standing_events)


def test_standing_sets_lease_priority_and_a_jittered_lease_cap():
    assert rank(TRUSTED) > rank(STANDARD) > rank(PROBATION) == rank('unknown')
    rng = random.Random(0)
    caps = {level: [lease_cap_s(level, rng) for _ in range(200)] for level in (PROBATION, STANDARD, TRUSTED)}
    assert all(0.4 * H <= c <= 0.6 * H for c in caps[PROBATION])
    assert all(0.8 * H <= c <= 1.2 * H for c in caps[STANDARD])
    assert all(1.6 * H <= c <= 2.4 * H for c in caps[TRUSTED])
    assert len({round(c) for c in caps[STANDARD]}) > 100  # jittered, not a fixed exit time


def test_the_state_functions_record_their_standing_events():
    failed = CheckVerdict(
        verdict='BENCH', checks=[CheckResult('gpu_proof', False, {'reason': 'too slow'})], gpu_uuids=[], card_name=''
    )
    benched = apply_verdict(BoxState('hk', status=IDLE), failed, 10.0)
    assert benched.status == BENCHED and benched.standing_events[-1]['kind'] == CHECK_FAILED

    box = BoxState('hk', status=IDLE)
    for t in range(3):
        box = apply_unreachable(box, float(t))
    assert box.status == BENCHED and [e['kind'] for e in box.standing_events] == [UNREACHABLE_BENCHED]

    box = record_start(BoxState('hk', status=IDLE), False, 5.0, reason='max_load_s')
    assert box.standing_events == [{'at': 5.0, 'kind': START_FAILED, 'reason': 'max_load_s'}]
    assert record_start(box, True, 6.0).standing_events == box.standing_events
    assert standing(box.standing_events) == PROBATION
