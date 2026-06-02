"""Tests for calculate_time_decay and the round-scoped reference-time anchor."""

from datetime import datetime, timezone

import pytest

datetime_utils = pytest.importorskip('gittensor.validator.utils.datetime_utils')
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')

calculate_time_decay = datetime_utils.calculate_time_decay
set_scoring_reference_time = datetime_utils.set_scoring_reference_time
ResolvedTimeDecay = load_weights.ResolvedTimeDecay


@pytest.fixture(autouse=True)
def _clear_reference_time():
    """Ensure module-level reference time is always reset between tests."""
    set_scoring_reference_time(None)
    yield
    set_scoring_reference_time(None)


def _default_decay() -> ResolvedTimeDecay:
    return ResolvedTimeDecay(
        grace_period_hours=1.0,
        sigmoid_steepness=0.1,
        sigmoid_midpoint_days=30,
        min_multiplier=0.1,
    )


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


class TestCalculateTimeDecay:
    def test_within_grace_period_returns_one(self):
        ref = _utc(2026, 5, 1, 12)
        merged = _utc(2026, 5, 1, 11, 30)  # 30 min ago, grace=1h
        set_scoring_reference_time(ref)
        assert calculate_time_decay(merged, _default_decay()) == 1.0

    def test_old_pr_returns_low_multiplier(self):
        ref = _utc(2026, 6, 1)
        merged = _utc(2026, 1, 1)  # ~150 days ago, well past midpoint
        set_scoring_reference_time(ref)
        result = calculate_time_decay(merged, _default_decay())
        assert result < 0.2

    def test_recent_pr_returns_high_multiplier(self):
        ref = _utc(2026, 5, 2)
        merged = _utc(2026, 5, 1)  # 1 day ago
        set_scoring_reference_time(ref)
        result = calculate_time_decay(merged, _default_decay())
        assert result > 0.9

    def test_result_never_below_min_multiplier(self):
        ref = _utc(2030, 1, 1)
        merged = _utc(2020, 1, 1)  # 10 years ago
        set_scoring_reference_time(ref)
        result = calculate_time_decay(merged, _default_decay())
        assert result == pytest.approx(_default_decay().min_multiplier)


class TestScoringReferenceTime:
    def test_same_reference_yields_identical_results(self):
        """Two calls with the same pinned time produce the same multiplier."""
        merged = _utc(2026, 4, 1)
        ref = _utc(2026, 5, 1)
        set_scoring_reference_time(ref)
        r1 = calculate_time_decay(merged, _default_decay())
        r2 = calculate_time_decay(merged, _default_decay())
        assert r1 == r2

    def test_different_reference_times_yield_different_results(self):
        """Validators with different wall-clock times get different results
        without a pinned reference — this is the bug the anchor fixes."""
        merged = _utc(2026, 4, 15)
        decay = _default_decay()

        set_scoring_reference_time(_utc(2026, 5, 1))
        r_early = calculate_time_decay(merged, decay)

        set_scoring_reference_time(_utc(2026, 5, 15))
        r_late = calculate_time_decay(merged, decay)

        assert r_early != r_late

    def test_pinned_time_isolates_from_wall_clock(self):
        """With a pinned reference, the result is deterministic regardless of
        when the test runs."""
        merged = _utc(2026, 4, 1)
        ref = _utc(2026, 5, 1)
        set_scoring_reference_time(ref)
        result = calculate_time_decay(merged, _default_decay())
        # Re-run immediately — wall clock has advanced but result is identical
        result2 = calculate_time_decay(merged, _default_decay())
        assert result == result2

    def test_cleared_reference_falls_back_to_wall_clock(self):
        """After set_scoring_reference_time(None), calculate_time_decay uses
        datetime.now() — result is still a valid float in [min, 1.0]."""
        set_scoring_reference_time(None)
        merged = _utc(2026, 1, 1)
        result = calculate_time_decay(merged, _default_decay())
        decay = _default_decay()
        assert decay.min_multiplier <= result <= 1.0
