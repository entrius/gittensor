#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for PR review quality multiplier (issue #303)."""

import pytest

from gittensor.constants import REVIEW_PENALTY_RATE
from gittensor.validator.oss_contributions.scoring import (
    calculate_review_collateral_multiplier,
    calculate_review_quality_multiplier,
)


class TestCalculateReviewQualityMultiplier:
    """Tests for the standalone calculate_review_quality_multiplier function."""

    def test_no_reviews_returns_one(self):
        assert calculate_review_quality_multiplier(0, REVIEW_PENALTY_RATE) == 1.0

    def test_one_review_applies_single_penalty(self):
        result = calculate_review_quality_multiplier(1, REVIEW_PENALTY_RATE)
        assert result == pytest.approx(1.0 - REVIEW_PENALTY_RATE)

    def test_two_reviews_cumulative(self):
        result = calculate_review_quality_multiplier(2, REVIEW_PENALTY_RATE)
        assert result == pytest.approx(1.0 - 2 * REVIEW_PENALTY_RATE)

    def test_table_values(self):
        """Verify expected values across the penalty range at the default rate."""
        expected = {0: 1.00, 1: 0.85, 2: 0.70, 3: 0.55, 4: 0.40, 5: 0.25, 6: 0.10}
        for n, mult in expected.items():
            assert calculate_review_quality_multiplier(n, REVIEW_PENALTY_RATE) == pytest.approx(mult, abs=1e-9), (
                f'n={n}'
            )

    def test_floor_at_zero(self):
        assert calculate_review_quality_multiplier(7, REVIEW_PENALTY_RATE) == 0.0

    def test_large_count_stays_at_zero(self):
        assert calculate_review_quality_multiplier(100, REVIEW_PENALTY_RATE) == 0.0

    def test_returns_float(self):
        assert isinstance(calculate_review_quality_multiplier(0, REVIEW_PENALTY_RATE), float)

    def test_per_repo_rate_overrides_default(self):
        # A repo-configured rate replaces the global default.
        assert calculate_review_quality_multiplier(1, 0.25) == pytest.approx(0.75)
        assert calculate_review_quality_multiplier(2, 0.25) == pytest.approx(0.50)


class TestCalculateReviewCollateralMultiplier:
    """Tests for the collateral-only review multiplier for OPEN PRs."""

    def test_no_reviews_returns_one(self):
        assert calculate_review_collateral_multiplier(0, REVIEW_PENALTY_RATE) == 1.0

    def test_one_review_increases_collateral_multiplier(self):
        assert calculate_review_collateral_multiplier(1, REVIEW_PENALTY_RATE) == pytest.approx(
            1.0 + REVIEW_PENALTY_RATE
        )

    def test_table_values(self):
        expected = {0: 1.00, 1: 1.15, 2: 1.30, 3: 1.45}
        for n, mult in expected.items():
            assert calculate_review_collateral_multiplier(n, REVIEW_PENALTY_RATE) == pytest.approx(mult, abs=1e-9), (
                f'n={n}'
            )

    def test_caps_at_two(self):
        assert calculate_review_collateral_multiplier(7, REVIEW_PENALTY_RATE) == pytest.approx(2.0)
        assert calculate_review_collateral_multiplier(100, REVIEW_PENALTY_RATE) == pytest.approx(2.0)

    def test_per_repo_rate_overrides_default(self):
        assert calculate_review_collateral_multiplier(1, 0.25) == pytest.approx(1.25)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
