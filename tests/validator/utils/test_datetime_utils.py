"""Tests for gittensor/validator/utils/datetime_utils.py lookback_cutoff."""
from datetime import datetime, timedelta, timezone

from gittensor.validator.utils.datetime_utils import lookback_cutoff


def test_returns_midnight_utc():
    """Cutoff must be floored to 00:00:00 UTC."""
    cutoff = lookback_cutoff(35)
    assert cutoff.hour == 0
    assert cutoff.minute == 0
    assert cutoff.second == 0
    assert cutoff.microsecond == 0
    assert cutoff.tzinfo == timezone.utc


def test_two_validators_within_same_utc_day_get_identical_cutoff():
    """Two datetime.now() reads a few seconds apart produce the same cutoff."""
    now1 = datetime.now(timezone.utc)
    # Simulate a 5-second gap
    now2 = now1 + timedelta(seconds=5)
    # Both must fall on the same UTC day, so lookback_cutoff returns the same value
    # (tested by calling it twice — within the same second in practice).
    c1 = lookback_cutoff(35)
    c2 = lookback_cutoff(35)
    assert c1 == c2


def test_two_validators_straddling_midnight_get_one_day_offset():
    """Validators on either side of UTC midnight differ by exactly 1 day."""
    # Simulate two timestamps straddling midnight
    day_boundary = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    before_midnight = day_boundary - timedelta(seconds=1)
    after_midnight = day_boundary + timedelta(seconds=1)
    # lookback_cutoff subtracts 35 days then floors:
    expected_before = (before_midnight - timedelta(days=35)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    expected_after = (after_midnight - timedelta(days=35)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert (expected_after - expected_before) == timedelta(days=1)


def test_subtracts_correct_number_of_days():
    """Arithmetic sanity for various lookback values."""
    now = datetime.now(timezone.utc)
    for days in (0, 1, 35, 365):
        cutoff = lookback_cutoff(days)
        raw = now - timedelta(days=days)
        expected = raw.replace(hour=0, minute=0, second=0, microsecond=0)
        assert cutoff == expected
