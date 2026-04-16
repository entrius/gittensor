#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for datetime parsing helpers."""

from datetime import datetime, timezone

import pytest

datetime_utils = pytest.importorskip(
    'gittensor.validator.utils.datetime_utils',
    reason='Requires gittensor package with all dependencies',
)

parse_github_iso_to_utc = datetime_utils.parse_github_iso_to_utc
parse_github_timestamp_to_cst = datetime_utils.parse_github_timestamp_to_cst


class TestParseGithubIsoToUtc:
    """Test suite for parse_github_iso_to_utc."""

    def test_z_suffix_returns_utc(self):
        result = parse_github_iso_to_utc('2024-01-15T10:30:00Z')
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert result.tzinfo == timezone.utc

    def test_numeric_offset_is_normalized_to_utc(self):
        result = parse_github_iso_to_utc('2024-01-15T05:30:00-05:00')
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert result.tzinfo == timezone.utc

    def test_naive_input_is_treated_as_utc(self):
        result = parse_github_iso_to_utc('2024-01-15T10:30:00')
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert result.tzinfo == timezone.utc

    def test_equivalent_timestamps_in_different_zones_are_equal(self):
        utc = parse_github_iso_to_utc('2024-01-15T10:30:00Z')
        offset = parse_github_iso_to_utc('2024-01-15T05:30:00-05:00')
        assert utc == offset

    def test_idempotent_via_isoformat_round_trip(self):
        first = parse_github_iso_to_utc('2024-01-15T10:30:00Z')
        second = parse_github_iso_to_utc(first.isoformat())
        assert first == second
        assert second.tzinfo == timezone.utc

    def test_surrounding_whitespace_is_trimmed(self):
        result = parse_github_iso_to_utc('  2024-01-15T10:30:00Z  ')
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


class TestParseGithubTimestampToCstDeprecation:
    """The CST wrapper is retained one cycle for backwards compatibility."""

    def test_emits_deprecation_warning(self):
        with pytest.warns(DeprecationWarning, match='parse_github_iso_to_utc'):
            parse_github_timestamp_to_cst('2024-01-15T10:30:00Z')

    def test_returns_same_moment_in_time_as_utc_helper(self):
        with pytest.warns(DeprecationWarning):
            cst = parse_github_timestamp_to_cst('2024-01-15T10:30:00Z')
        utc = parse_github_iso_to_utc('2024-01-15T10:30:00Z')
        assert cst == utc
