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

from gittensor.classes import MinerEvaluation, PRState, PullRequest
from gittensor.constants import (
    MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER,
    OPEN_PR_COLLATERAL_PERCENT,
    REVIEW_PENALTY_RATE,
)
from gittensor.utils.github_api_tools import _MAX_CHANGES_REQUESTED_REVIEWS
from gittensor.validator.oss_contributions.scoring import (
    calculate_open_pr_collateral_score,
    calculate_pr_multipliers,
    calculate_review_collateral_multiplier,
    calculate_review_quality_multiplier,
)
from gittensor.validator.utils.load_weights import RepositoryConfig
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

    def test_open_pr_also_counts_maintainer_reviews(self):
        pr_data = _make_graphql_pr(
            'OPEN',
            [
                {'authorAssociation': 'OWNER'},
                {'authorAssociation': 'CONTRIBUTOR'},
                {'authorAssociation': 'MEMBER'},
            ],
        )
        pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='hk', github_id='123')
        assert pr.changes_requested_count == 2


class TestReviewCollateralMultiplierOnOpenPRCollateral:
    def _prepare_open_pr(self, builder):
        pr = builder.create(state=PRState.OPEN)
        pr.base_score = 100.0
        pr.repo_weight_multiplier = 1.0
        pr.issue_multiplier = 1.0
        pr.label_multiplier = 1.0
        pr.changes_requested_count = 0
        return pr

    def test_clean_open_pr_collateral_unchanged(self, builder):
        pr = self._prepare_open_pr(builder)
        baseline = calculate_open_pr_collateral_score(pr)
        pr.changes_requested_count = 0
        assert calculate_open_pr_collateral_score(pr) == pytest.approx(baseline)

    def test_open_pr_with_changes_requested_increases_collateral(self, builder):
        pr = self._prepare_open_pr(builder)
        baseline = calculate_open_pr_collateral_score(pr)
        pr.changes_requested_count = 3
        adjusted = calculate_open_pr_collateral_score(pr)
        assert adjusted == pytest.approx(baseline * 1.45)

    def test_open_pr_review_collateral_multiplier_caps_at_two(self, builder):
        pr = self._prepare_open_pr(builder)
        baseline = calculate_open_pr_collateral_score(pr)
        pr.changes_requested_count = 100
        assert calculate_open_pr_collateral_score(pr) == pytest.approx(baseline * 2.0)


def _make_repo_config() -> dict:
    return {'test/repo': RepositoryConfig(weight=1.0, label_multipliers={'fix': 1.25})}


def _make_eval() -> MinerEvaluation:
    return MinerEvaluation(uid=0, hotkey='hk', github_id='1')


class TestReviewCollateralThroughScoringPipeline:
    def test_open_pr_collateral_uses_collateral_review_multiplier(self, builder):
        pr = builder.create(state=PRState.OPEN, repo='test/repo')
        pr.base_score = 80.0
        pr.label = 'fix'
        pr.changes_requested_count = 3

        calculate_pr_multipliers(pr, _make_eval(), _make_repo_config())

        assert pr.label_multiplier == pytest.approx(1.25)

        collateral_projection = (
            pr.base_score
            * pr.repo_weight_multiplier
            * pr.issue_multiplier
            * pr.label_multiplier
            * calculate_review_collateral_multiplier(pr.changes_requested_count)
        )
        expected_collateral = collateral_projection * OPEN_PR_COLLATERAL_PERCENT

        assert calculate_open_pr_collateral_score(pr) == pytest.approx(expected_collateral)


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
