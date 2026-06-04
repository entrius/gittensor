"""Tests for scoring time-decay anchoring."""

from datetime import datetime, timedelta, timezone

from gittensor.validator.utils.datetime_utils import calculate_time_decay
from gittensor.validator.utils.load_weights import ResolvedTimeDecay, resolve_time_decay


def test_calculate_time_decay_uses_shared_reference_time():
    """Same merged_at and reference_time must yield identical multipliers."""
    time_decay = resolve_time_decay(None)
    merged_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    reference = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    first = calculate_time_decay(merged_at, time_decay, reference_time=reference)
    second = calculate_time_decay(merged_at, time_decay, reference_time=reference)

    assert first == second


def test_calculate_time_decay_differs_when_reference_advances():
    """Advancing the anchor changes decay — proves wall-clock drift is real."""
    cfg = ResolvedTimeDecay(
        grace_period_hours=12,
        sigmoid_midpoint_days=10.0,
        sigmoid_steepness=0.4,
        min_multiplier=0.05,
    )
    merged_at = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    # Inside the 12h grace window → full multiplier.
    inside_grace_ref = merged_at + timedelta(hours=6)
    # Well past grace → sigmoid decay applies.
    past_grace_ref = merged_at + timedelta(days=15)

    inside_grace = calculate_time_decay(merged_at, cfg, reference_time=inside_grace_ref)
    past_grace = calculate_time_decay(merged_at, cfg, reference_time=past_grace_ref)

    assert inside_grace == 1.0
    assert past_grace < inside_grace
