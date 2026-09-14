# The MIT License (MIT)
# Copyright © 2025 Entrius

"""ADMIT / IDLE / BENCHED transitions, the UUID pin, the bench backoff ladder, and the JSON store."""

from gittensor.controller.checks.state import (
    ADMIT,
    BENCHED,
    IDLE,
    BoxState,
    StateStore,
    apply_verdict,
    backoff_seconds,
    due_for_check,
    release_from_bench,
)
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict

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
    b3 = apply_verdict(release_from_bench(b2, b2.bench_until), bench_verdict(), now=b2.bench_until)
    b4 = apply_verdict(release_from_bench(b3, b3.bench_until), bench_verdict(), now=b3.bench_until)
    b5 = apply_verdict(release_from_bench(b4, b4.bench_until), bench_verdict(), now=b4.bench_until)
    assert [b.bench_until - b.benched_at for b in (b3, b4, b5)] == [16 * H, 64 * H, 64 * H]


def test_ladder_resets_after_a_clean_stretch():
    box = apply_verdict(BoxState('hk1'), admit_verdict(), now=0.0)
    b1 = apply_verdict(box, bench_verdict(), now=0.0)
    b2 = apply_verdict(release_from_bench(b1, H), bench_verdict(), now=H)
    assert b2.bench_count == 2
    idle = apply_verdict(release_from_bench(b2, b2.bench_until), admit_verdict(), now=b2.bench_until)
    late = apply_verdict(idle, bench_verdict(), now=b2.bench_until + 8 * 86_400)
    assert late.bench_count == 1 and late.bench_until - late.benched_at == H
    soon = apply_verdict(idle, bench_verdict(), now=b2.bench_until + 86_400)
    assert soon.bench_count == 3


def test_due_for_check_interval():
    idle = apply_verdict(BoxState('hk1'), admit_verdict(), now=0.0)
    assert not due_for_check(idle, 100.0) and due_for_check(idle, 900.0) and due_for_check(idle, 50.0, interval_s=10)


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
