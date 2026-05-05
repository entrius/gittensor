# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for shared datetime helpers — focus on the cross-validator-deterministic
``lookback_cutoff`` quantization (issue #1003)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from gittensor.validator.utils.datetime_utils import lookback_cutoff


class TestLookbackCutoff:
    def test_returns_midnight_utc(self):
        # Given any wall-clock read mid-day, the cutoff must floor to 00:00:00 UTC.
        with patch('gittensor.validator.utils.datetime_utils.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 6, 14, 37, 23, 451000, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            cutoff = lookback_cutoff(35)

        assert cutoff == datetime(2026, 4, 1, 0, 0, 0, 0, tzinfo=timezone.utc)
        assert cutoff.tzinfo == timezone.utc

    def test_two_validators_within_same_utc_day_get_identical_cutoff(self):
        # The bug being fixed: two validators reading datetime.now() seconds apart
        # used to disagree on the cutoff, flipping a boundary PR's include/exclude.
        v1_now = datetime(2026, 5, 6, 14, 37, 23, 451000, tzinfo=timezone.utc)
        v2_now = v1_now + timedelta(seconds=5)

        with patch('gittensor.validator.utils.datetime_utils.datetime') as mock_dt:
            mock_dt.now.return_value = v1_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            v1_cutoff = lookback_cutoff(35)

        with patch('gittensor.validator.utils.datetime_utils.datetime') as mock_dt:
            mock_dt.now.return_value = v2_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            v2_cutoff = lookback_cutoff(35)

        assert v1_cutoff == v2_cutoff

    def test_two_validators_straddling_midnight_get_one_day_offset(self):
        # Validators reading on opposite sides of UTC-midnight resolve to cutoffs
        # 1 day apart. This is the residual divergence — coarser than seconds and
        # accepted as the design tradeoff (issue #1003 suggested fix shape).
        v1_now = datetime(2026, 5, 6, 23, 59, 59, tzinfo=timezone.utc)
        v2_now = datetime(2026, 5, 7, 0, 0, 1, tzinfo=timezone.utc)

        with patch('gittensor.validator.utils.datetime_utils.datetime') as mock_dt:
            mock_dt.now.return_value = v1_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            v1_cutoff = lookback_cutoff(35)

        with patch('gittensor.validator.utils.datetime_utils.datetime') as mock_dt:
            mock_dt.now.return_value = v2_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            v2_cutoff = lookback_cutoff(35)

        assert v2_cutoff - v1_cutoff == timedelta(days=1)

    def test_subtracts_correct_number_of_days(self):
        with patch('gittensor.validator.utils.datetime_utils.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            assert lookback_cutoff(0) == datetime(2026, 5, 6, 0, 0, 0, tzinfo=timezone.utc)
            assert lookback_cutoff(1) == datetime(2026, 5, 5, 0, 0, 0, tzinfo=timezone.utc)
            assert lookback_cutoff(35) == datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
            assert lookback_cutoff(365) == datetime(2025, 5, 6, 0, 0, 0, tzinfo=timezone.utc)
