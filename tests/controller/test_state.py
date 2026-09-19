# The MIT License (MIT)
# Copyright © 2025 Entrius

"""ADMIT / IDLE / BENCHED transitions, the UUID pin, the bench backoff ladder, and the JSON store."""

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.state import (
    ADMIT,
    BENCHED,
    CHECK_NOT_RUN,
    CHECKING,
    IDLE,
    LEASED,
    STARTING,
    BoxState,
    StateStore,
    add_event,
    apply_unreachable,
    apply_verdict,
    backoff_seconds,
    clean_seconds,
    due_for_check,
    ladder_rung,
    mark_reachable,
    not_run_retry_at,
    release_from_bench,
    request_release,
    transition_card,
)
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.standing import standing

H = 3600


def admit_verdict(uuids=('GPU-a',)):
    return CheckVerdict.from_checks(
        [CheckResult('gpu_spec', True)], list(uuids), 'NVIDIA GeForce RTX 5090', '580.65.06', now=0.0
    )


def bench_verdict(*failed):
    checks = [CheckResult(name, False, {'reason': 'x'}) for name in failed] or [CheckResult('gpu_spec', False)]
    return CheckVerdict.from_checks(checks, [], now=0.0)


def test_backoff_ladder():
    assert [backoff_seconds(n) for n in range(0, 7)] == [0, H, 4 * H, 16 * H, 64 * H, 64 * H, 64 * H]
    assert backoff_seconds(2, ladder=(10, 20)) == 20 and backoff_seconds(9, ladder=(10, 20)) == 20


def test_admit_pins_uuids_and_moves_to_idle():
    box = BoxState('hk1')
    assert box.status == ADMIT and due_for_check(box, 0.0)
    idle = apply_verdict(box, admit_verdict(['GPU-a', 'GPU-b']), now=100.0)
    assert idle.status == IDLE and idle.pinned_uuids == ['GPU-a', 'GPU-b'] and idle.admitted_at == 100.0
    assert idle.card_name == 'NVIDIA GeForce RTX 5090' and idle.last_check_at == 100.0 and idle.last_failed == []
    assert box.status == ADMIT and box.pinned_uuids == []  # pure: the input is untouched
    # an IDLE pass keeps the original pin and admission time
    again = apply_verdict(idle, admit_verdict(['GPU-a', 'GPU-b']), now=1000.0)
    assert again.pinned_uuids == ['GPU-a', 'GPU-b'] and again.admitted_at == 100.0 and again.last_check_at == 1000.0


def _until(box: BoxState) -> float:
    assert box.bench_until is not None
    return box.bench_until


def _benched_at(box: BoxState) -> float:
    assert box.benched_at is not None
    return box.benched_at


def test_bench_climbs_the_ladder_and_clears_the_pin():
    box = apply_verdict(BoxState('hk1'), admit_verdict(), now=0.0)
    b1 = apply_verdict(box, bench_verdict('power_limit'), now=10.0)
    assert b1.status == BENCHED and b1.bench_count == 1 and b1.bench_until == 10.0 + H
    assert b1.pinned_uuids == [] and b1.admitted_at is None and b1.last_failed == ['power_limit']
    assert not due_for_check(b1, 10.0 + H)  # benched boxes are not checked; release them first
    assert release_from_bench(b1, 10.0 + H - 1) is b1
    re = release_from_bench(b1, 10.0 + H)
    assert re.status == ADMIT and re.bench_until is None and re.bench_count == 1
    b2 = apply_verdict(re, bench_verdict('gpu_proof'), now=20.0 + H)
    assert b2.bench_count == 2 and b2.bench_until == 20.0 + H + 4 * H
    b3 = apply_verdict(release_from_bench(b2, _until(b2)), bench_verdict(), now=_until(b2))
    b4 = apply_verdict(release_from_bench(b3, _until(b3)), bench_verdict(), now=_until(b3))
    b5 = apply_verdict(release_from_bench(b4, _until(b4)), bench_verdict(), now=_until(b4))
    assert [_until(b) - _benched_at(b) for b in (b3, b4, b5)] == [16 * H, 64 * H, 64 * H]


def test_the_ladder_steps_down_with_clean_time():
    """Kimbo 9/19: one rung per 6 clean hours, none left at 48 h. The old rule (7 days since the last bench began, all
    or nothing) left a box that had been flawless for 6 days one miss from 64 h."""
    box = apply_verdict(BoxState('hk1'), admit_verdict(), now=0.0)
    b1 = apply_verdict(box, bench_verdict(), now=0.0)
    b2 = apply_verdict(release_from_bench(b1, H), bench_verdict(), now=H)
    b3 = apply_verdict(release_from_bench(b2, _until(b2)), bench_verdict(), now=_until(b2))
    assert b3.bench_count == 3 and ladder_rung(b3, _until(b3)) == 3  # a bench is not clean time
    t0 = _until(b3)
    idle = apply_verdict(release_from_bench(b3, t0), admit_verdict(), now=t0)
    assert [ladder_rung(idle, t0 + h * H) for h in (0, 5.9, 6, 12, 18, 47, 48)] == [3, 3, 2, 1, 0, 0, 0]
    soon = apply_verdict(idle, bench_verdict(), now=t0 + H)
    assert soon.bench_count == 4 and _until(soon) - _benched_at(soon) == 64 * H
    later = apply_verdict(idle, bench_verdict(), now=t0 + 13 * H)  # two rungs earned back: the third bench again
    assert later.bench_count == 2 and _until(later) - _benched_at(later) == 4 * H
    clean = apply_verdict(idle, bench_verdict(), now=t0 + 48 * H)
    assert clean.bench_count == 1 and _until(clean) - _benched_at(clean) == H
    # past the top of the ladder the clean slate still comes at 48 h, not at six hours a rung
    deep = BoxState.from_dict({**idle.as_dict(), 'bench_count': 9})
    assert ladder_rung(deep, t0 + 47 * H) == 2 and ladder_rung(deep, t0 + 48 * H) == 0


def test_time_offline_is_not_clean_time():
    """The old 7-day reset counted wall-clock time, so a box that sat offline for a week came back with a clean ladder.
    The clean clock stops while the box is unreachable or has a strike, and a bench starts it over."""
    box = apply_verdict(BoxState('hk1'), admit_verdict(), now=0.0)
    benched = apply_verdict(box, bench_verdict(), now=0.0)
    idle = apply_verdict(release_from_bench(benched, H), admit_verdict(), now=H)
    assert clean_seconds(idle, H + 2 * H) == 2 * H
    gone = apply_unreachable(idle, H + 2 * H)
    assert clean_seconds(gone, H + 30 * H) == 2 * H and ladder_rung(gone, H + 30 * H) == 1
    back = mark_reachable(gone, H + 30 * H)  # a heartbeat answered
    assert back.unreachable_count == 0 and clean_seconds(back, H + 34 * H) == 6 * H
    assert ladder_rung(back, H + 34 * H) == 0
    assert mark_reachable(gone).clean_paused_at is not None  # without a time the clock is left alone
    assert clean_seconds(benched, 99 * H) == 0.0 and clean_seconds(BoxState('new'), 99 * H) == 0.0


def not_run_verdict():
    checks = [CheckResult('gpu_spec', True), CheckResult('gpu_proof', False, {'reason': 'x'}, not_run=True)]
    return CheckVerdict.from_checks(checks, ['GPU-a', 'GPU-b'], now=0.0)


def test_a_check_that_could_not_run_is_a_strike_not_a_bench():
    box = apply_verdict(BoxState('hk1'), admit_verdict(['GPU-a', 'GPU-b']), now=0.0)
    box = transition_card(transition_card(box, 'GPU-b', STARTING, 1.0, 'i-1'), 'GPU-b', LEASED, 2.0)
    one = apply_verdict(box, not_run_verdict(), now=100.0, proved=['GPU-a'])
    assert one.status == IDLE and one.bench_count == 0 and one.not_run_count == 1 and one.not_run_at == 100.0
    assert one.last_check_at == 0.0  # no proof: idle pay lapses with the old one
    assert one.cards['GPU-a'].state == CHECKING  # unpaid, not leasable, re-proved
    assert one.cards['GPU-b'].state == LEASED and one.cards['GPU-b'].instance_id == 'i-1'  # a lease is not touched
    assert [e['kind'] for e in one.standing_events] == [CHECK_NOT_RUN] and one.standing_events[0]['strike'] == 1
    assert not_run_retry_at(one) == 100.0 + cfg.COULD_NOT_RUN_RETRY_S and not_run_retry_at(box) is None
    # a pass clears the count
    ok = apply_verdict(one, admit_verdict(['GPU-a', 'GPU-b']), now=200.0, proved=['GPU-a'])
    assert ok.not_run_count == 0 and ok.not_run_at is None and ok.cards['GPU-a'].state == IDLE
    # three in a row: a failed check on the ladder, and the count starts over after the bench
    two = apply_verdict(one, not_run_verdict(), now=1300.0, proved=['GPU-a'])
    assert two.status == IDLE and two.not_run_count == 2
    three = apply_verdict(two, not_run_verdict(), now=2500.0, proved=['GPU-a'])
    assert three.status == BENCHED and three.bench_count == 1 and three.bench_until == 2500.0 + H
    assert three.last_failed == ['gpu_proof'] and three.not_run_count == 0 and three.cards == {}
    assert three.standing_events[-1]['kind'] == 'check_failed' and three.standing_events[-1]['not_run_rounds'] == 3
    again = apply_verdict(release_from_bench(three, _until(three)), not_run_verdict(), now=_until(three))
    assert again.status == ADMIT and again.not_run_count == 1 and again.bench_count == 1  # three more tries


def test_a_strike_stops_the_clean_clock_until_the_next_pass():
    box = apply_verdict(BoxState('hk1'), admit_verdict(), now=0.0)
    struck = apply_verdict(box, not_run_verdict(), now=2 * H)
    assert clean_seconds(struck, 5 * H) == 2 * H
    passed = apply_verdict(struck, admit_verdict(), now=5 * H)
    assert clean_seconds(passed, 6 * H) == 3 * H


def test_a_forgiven_release_gives_the_rung_and_the_standing_back():
    box = apply_verdict(BoxState('hk1'), admit_verdict(), now=0.0)
    box = add_event(box, 'clean_lease', 50.0, leased_s=7 * H)
    benched = apply_verdict(box, bench_verdict('nvml_digest'), now=100.0)
    assert benched.bench_count == 1 and standing(benched.standing_events) == 'probation'
    kept = release_from_bench(request_release(benched, 200.0, 'test over'), 300.0)
    assert kept.bench_count == 1 and standing(kept.standing_events) == 'probation'
    forgiven = release_from_bench(request_release(benched, 200.0, 'our allowlist', forgive=True), 300.0)
    assert forgiven.status == ADMIT and forgiven.bench_count == 0
    assert [e['kind'] for e in forgiven.standing_events] == ['clean_lease', 'released']
    assert forgiven.standing_events[-1]['forgiven'] is True and standing(forgiven.standing_events) == 'standard'
    # a flat unreachable bench never climbed the ladder: nothing to give back
    flat = box
    for n in range(3):
        flat = apply_unreachable(flat, 1000.0 + n)
    flat = BoxState.from_dict({**flat.as_dict(), 'bench_count': 2})
    assert release_from_bench(request_release(flat, 2000.0, 'x', forgive=True), 2001.0).bench_count == 2


def test_due_for_check_interval():
    idle = apply_verdict(BoxState('hk1'), admit_verdict(), now=0.0)
    assert not due_for_check(idle, 100.0) and due_for_check(idle, 1200.0) and due_for_check(idle, 50.0, interval_s=10)


def test_state_store_roundtrip(tmp_path):
    store = StateStore(tmp_path / 'boxes.json')
    assert store.get('hk1').status == ADMIT and store.boxes == {}
    store.put(apply_verdict(BoxState('hk1'), admit_verdict(['GPU-a']), now=5.0))
    store.put(apply_verdict(BoxState('hk2'), bench_verdict('nvml_digest'), now=5.0))
    reopened = StateStore(tmp_path / 'boxes.json')
    assert reopened.get('hk1').status == IDLE and reopened.get('hk1').pinned_uuids == ['GPU-a']
    assert reopened.get('hk2').status == BENCHED and reopened.get('hk2').last_failed == ['nvml_digest']
    assert [b.box_id for b in reopened.by_status(BENCHED)] == ['hk2']
    assert BoxState.from_dict({'box_id': 'x', 'status': IDLE, 'unknown_field': 1}).status == IDLE
