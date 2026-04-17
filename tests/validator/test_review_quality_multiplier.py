#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for PR review quality multiplier (issue #303).

Covers:
- calculate_review_quality_multiplier standalone function
- review_quality_multiplier field on PullRequest and its effect on earned_score
"""

from math import ceil

import pytest

from gittensor.classes import PRState, PullRequest
from gittensor.constants import REVIEW_PENALTY_RATE
from gittensor.utils.github_api_tools import _MAX_CHANGES_REQUESTED_REVIEWS
from gittensor.validator.oss_contributions.scoring import calculate_review_quality_multiplier
from tests.validator.conftest import PRBuilder

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def builder():
    return PRBuilder()


# ============================================================================
# TestCalculateReviewQualityMultiplier
# ============================================================================


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
        """Multiplier must not go below 0.0 for extreme counts."""
        assert calculate_review_quality_multiplier(7) == 0.0

    def test_large_count_stays_at_zero(self):
        assert calculate_review_quality_multiplier(100) == 0.0

    def test_returns_float(self):
        assert isinstance(calculate_review_quality_multiplier(0), float)


# ============================================================================
# TestReviewQualityMultiplierOnPullRequest
# ============================================================================


class TestReviewQualityMultiplierOnPullRequest:
    """Tests for review_quality_multiplier field on PullRequest and its effect on earned_score."""

    def test_default_multiplier_is_one(self, builder):
        pr = builder.create(state=PRState.MERGED)
        assert pr.review_quality_multiplier == 1.0

    def test_default_changes_requested_count_is_zero(self, builder):
        pr = builder.create(state=PRState.MERGED)
        assert pr.changes_requested_count == 0

    def test_review_multiplier_reduces_earned_score(self, builder):
        pr = builder.create(state=PRState.MERGED)
        pr.base_score = 100.0
        pr.repo_weight_multiplier = 1.0
        pr.issue_multiplier = 1.0
        pr.open_pr_spam_multiplier = 1.0
        pr.time_decay_multiplier = 1.0
        pr.credibility_multiplier = 1.0

        pr.review_quality_multiplier = 1.0
        score_no_penalty = pr.calculate_final_earned_score()

        pr.review_quality_multiplier = calculate_review_quality_multiplier(1)
        score_one_review = pr.calculate_final_earned_score()

        assert score_one_review == pytest.approx(score_no_penalty * 0.85)

    def test_zero_multiplier_zeroes_earned_score(self, builder):
        pr = builder.create(state=PRState.MERGED)
        pr.base_score = 50.0
        pr.repo_weight_multiplier = 1.0
        pr.issue_multiplier = 1.0
        pr.open_pr_spam_multiplier = 1.0
        pr.time_decay_multiplier = 1.0
        pr.credibility_multiplier = 1.0
        pr.review_quality_multiplier = 0.0

        assert pr.calculate_final_earned_score() == 0.0

    def test_multiplier_participates_in_product(self, builder):
        """review_quality_multiplier participates in the product of all multipliers."""
        pr = builder.create(state=PRState.MERGED)
        pr.base_score = 80.0
        pr.repo_weight_multiplier = 1.0
        pr.issue_multiplier = 1.0
        pr.open_pr_spam_multiplier = 1.0
        pr.time_decay_multiplier = 1.0
        pr.credibility_multiplier = 1.0
        pr.review_quality_multiplier = calculate_review_quality_multiplier(3)  # 0.55

        earned = pr.calculate_final_earned_score()
        assert earned == pytest.approx(80.0 * 0.55)


# ============================================================================
# TestChangesRequestedCountFromGraphQL
# ============================================================================


def _make_graphql_pr(state: str, changes_requested_reviews: list, merged_at: str = '2025-06-01T00:00:00Z') -> dict:
    """Build minimal GraphQL PR response data for from_graphql_response"""
    return {
        'number': 1,
        'title': 'Test PR',
        'state': state,
        'additions': 10,
        'deletions': 5,
        'createdAt': '2025-06-01T00:00:00Z',
        'mergedAt': merged_at if state == 'MERGED' else None,
        'author': {'login': 'testuser'},
        'repository': {'name': 'repo', 'owner': {'login': 'owner'}},
        'changesRequestedReviews': {'nodes': changes_requested_reviews},
    }


class TestChangesRequestedCountFromGraphQL:
    """Tests that from_graphql_response correctly parses changesRequestedReviews into changes_requested_count"""

    def test_merged_pr_counts_only_maintainer_reviews(self):
        pr_data = _make_graphql_pr(
            'MERGED',
            [
                {'authorAssociation': 'OWNER'},
                {'authorAssociation': 'CONTRIBUTOR'},
                {'authorAssociation': 'COLLABORATOR'},
                {'authorAssociation': 'NONE'},
            ],
        )
        pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='hk', github_id='123')
        assert pr.changes_requested_count == 2

    def test_non_merged_pr_does_not_parse_reviews(self):
        pr_data = _make_graphql_pr('OPEN', [{'authorAssociation': 'OWNER'}])
        pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='hk', github_id='123')
        assert pr.changes_requested_count == 0


def test_max_changes_requested_reviews_matches_penalty_rate():
    # Tripwire: the GraphQL fetch cap must stay aligned with REVIEW_PENALTY_RATE so that any
    # review beyond the cap is already forced to a 0.0 multiplier by calculate_review_quality_multiplier
    assert _MAX_CHANGES_REQUESTED_REVIEWS == ceil(1 / REVIEW_PENALTY_RATE)
    assert calculate_review_quality_multiplier(_MAX_CHANGES_REQUESTED_REVIEWS) == 0.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
