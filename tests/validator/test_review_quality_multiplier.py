#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for PR review quality multiplier (issue #303)."""

from math import ceil

import pytest

from gittensor.constants import (
    MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER,
    REVIEW_PENALTY_RATE,
)
from gittensor.utils.github_api_tools import _MAX_CHANGES_REQUESTED_REVIEWS
from gittensor.validator.oss_contributions.scoring import (
    calculate_review_collateral_multiplier,
    calculate_review_quality_multiplier,
)


class TestCalculateReviewQualityMultiplier:
    """Tests for the standalone calculate_review_quality_multiplier function."""

    def test_no_reviews_returns_one(self):
        assert calculate_review_quality_multiplier(0) == 1.0

    def test_one_review_applies_single_penalty(self):
        result = calculate_review_quality_multiplier(1)
        assert result == pytest.approx(1.0 - REVIEW_PENALTY_RATE)

    def test_two_reviews_cumulative(self):
        result = calculate_review_quality_multiplier(2)
        assert result == pytest.approx(1.0 - 2 * REVIEW_PENALTY_RATE)

    def test_table_values(self):
        """Verify expected values across the penalty range."""
        expected = {
            0: 1.00,
            1: 0.85,
            2: 0.70,
            3: 0.55,
            4: 0.40,
            5: 0.25,
            6: 0.10,
        }
        for n, mult in expected.items():
            assert calculate_review_quality_multiplier(n) == pytest.approx(mult, abs=1e-9), f'n={n}'

    def test_floor_at_zero(self):
        assert calculate_review_quality_multiplier(7) == 0.0

    def test_large_count_stays_at_zero(self):
        assert calculate_review_quality_multiplier(100) == 0.0

    def test_returns_float(self):
        assert isinstance(calculate_review_quality_multiplier(0), float)


class TestCalculateReviewCollateralMultiplier:
    """Tests for the collateral-only review multiplier for OPEN PRs."""

    def test_no_reviews_returns_one(self):
        assert calculate_review_collateral_multiplier(0) == 1.0

    def test_one_review_increases_collateral_multiplier(self):
        assert calculate_review_collateral_multiplier(1) == pytest.approx(1.0 + REVIEW_PENALTY_RATE)

    def test_table_values(self):
        expected = {
            0: 1.00,
            1: 1.15,
            2: 1.30,
            3: 1.45,
        }
        for n, mult in expected.items():
            assert calculate_review_collateral_multiplier(n) == pytest.approx(mult, abs=1e-9), f'n={n}'

    def test_caps_at_two(self):
        assert calculate_review_collateral_multiplier(7) == pytest.approx(2.0)
        assert calculate_review_collateral_multiplier(100) == pytest.approx(2.0)


def test_max_changes_requested_reviews_covers_review_multipliers():
    # Tripwire: the GraphQL fetch cap must stay aligned with every review-count-based multiplier.
    penalty_cap = ceil(1 / REVIEW_PENALTY_RATE)
    collateral_cap = ceil((MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER - 1.0) / REVIEW_PENALTY_RATE)

    assert _MAX_CHANGES_REQUESTED_REVIEWS == max(penalty_cap, collateral_cap)
    assert calculate_review_quality_multiplier(_MAX_CHANGES_REQUESTED_REVIEWS) == 0.0
    assert calculate_review_collateral_multiplier(_MAX_CHANGES_REQUESTED_REVIEWS) == pytest.approx(
        MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
