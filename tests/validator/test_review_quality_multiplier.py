#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for PR review quality multiplier (issue #303).

Covers:
- calculate_review_quality_multiplier standalone function
- review_quality_multiplier field on PullRequest and its effect on earned_score
- get_pull_request_maintainer_changes_requested_count GitHub API function
"""

from unittest.mock import Mock, patch

import pytest
import requests

from gittensor.classes import PRState
from gittensor.constants import MAINTAINER_ASSOCIATIONS, REVIEW_PENALTY_RATE
from gittensor.utils.github_api_tools import get_pull_request_maintainer_changes_requested_count
from gittensor.validator.oss_contributions.scoring import calculate_review_quality_multiplier
from gittensor.validator.oss_contributions.tier_config import TIERS, Tier
from tests.validator.conftest import PRBuilder

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def builder():
    return PRBuilder()


@pytest.fixture
def bronze():
    return TIERS[Tier.BRONZE]


# ============================================================================
# Helpers
# ============================================================================


def _make_review(state: str, association: str) -> dict:
    return {'state': state, 'author_association': association}


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
            1: 0.88,
            2: 0.76,
            3: 0.64,
            4: 0.52,
            5: 0.40,
            6: 0.28,
            7: 0.16,
            8: 0.04,
        }
        for n, mult in expected.items():
            assert calculate_review_quality_multiplier(n) == pytest.approx(mult, abs=1e-9), f'n={n}'

    def test_floor_at_zero(self):
        """Multiplier must not go below 0.0 for extreme counts."""
        assert calculate_review_quality_multiplier(9) == 0.0

    def test_large_count_stays_at_zero(self):
        assert calculate_review_quality_multiplier(100) == 0.0

    def test_returns_float(self):
        assert isinstance(calculate_review_quality_multiplier(0), float)


# ============================================================================
# TestReviewQualityMultiplierOnPullRequest
# ============================================================================


class TestReviewQualityMultiplierOnPullRequest:
    """Tests for review_quality_multiplier field on PullRequest and its effect on earned_score."""

    def test_default_multiplier_is_one(self, builder, bronze):
        pr = builder.create(state=PRState.MERGED, tier=bronze)
        assert pr.review_quality_multiplier == 1.0

    def test_review_multiplier_reduces_earned_score(self, builder, bronze):
        pr = builder.create(state=PRState.MERGED, tier=bronze)
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

        assert score_one_review == pytest.approx(score_no_penalty * 0.88)

    def test_zero_multiplier_zeroes_earned_score(self, builder, bronze):
        pr = builder.create(state=PRState.MERGED, tier=bronze)
        pr.base_score = 50.0
        pr.repo_weight_multiplier = 1.0
        pr.issue_multiplier = 1.0
        pr.open_pr_spam_multiplier = 1.0
        pr.time_decay_multiplier = 1.0
        pr.credibility_multiplier = 1.0
        pr.review_quality_multiplier = 0.0

        assert pr.calculate_final_earned_score() == 0.0

    def test_multiplier_participates_in_product(self, builder, bronze):
        """review_quality_multiplier participates in the product of all multipliers."""
        pr = builder.create(state=PRState.MERGED, tier=bronze)
        pr.base_score = 80.0
        pr.repo_weight_multiplier = 1.0
        pr.issue_multiplier = 1.0
        pr.open_pr_spam_multiplier = 1.0
        pr.time_decay_multiplier = 1.0
        pr.credibility_multiplier = 1.0
        pr.review_quality_multiplier = calculate_review_quality_multiplier(3)  # 0.64

        earned = pr.calculate_final_earned_score()
        assert earned == pytest.approx(80.0 * 0.64)


# ============================================================================
# TestGetPullRequestMaintainerChangesRequestedCount
# ============================================================================


class TestGetPullRequestMaintainerChangesRequestedCount:
    """Tests for the GitHub API function that counts CHANGES_REQUESTED reviews from maintainers."""

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_no_reviews_returns_zero(self, mock_get):
        mock_get.return_value = Mock(status_code=200, **{'json.return_value': []})
        assert get_pull_request_maintainer_changes_requested_count('owner/repo', 1, 'token') == 0

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_counts_changes_requested_from_maintainers(self, mock_get):
        reviews = [_make_review('CHANGES_REQUESTED', assoc) for assoc in MAINTAINER_ASSOCIATIONS]
        mock_get.return_value = Mock(status_code=200, **{'json.return_value': reviews})
        assert get_pull_request_maintainer_changes_requested_count('owner/repo', 1, 'token') == len(
            MAINTAINER_ASSOCIATIONS
        )

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_ignores_non_maintainer_changes_requested(self, mock_get):
        reviews = [
            _make_review('CHANGES_REQUESTED', 'CONTRIBUTOR'),
            _make_review('CHANGES_REQUESTED', 'NONE'),
            _make_review('CHANGES_REQUESTED', 'OWNER'),  # only this counts
        ]
        mock_get.return_value = Mock(status_code=200, **{'json.return_value': reviews})
        assert get_pull_request_maintainer_changes_requested_count('owner/repo', 1, 'token') == 1

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_ignores_non_changes_requested_states(self, mock_get):
        reviews = [
            _make_review('APPROVED', 'OWNER'),
            _make_review('COMMENTED', 'COLLABORATOR'),
            _make_review('DISMISSED', 'OWNER'),
            _make_review('CHANGES_REQUESTED', 'OWNER'),  # only this counts
        ]
        mock_get.return_value = Mock(status_code=200, **{'json.return_value': reviews})
        assert get_pull_request_maintainer_changes_requested_count('owner/repo', 1, 'token') == 1

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_multiple_reviews_from_same_maintainer_count_separately(self, mock_get):
        reviews = [
            _make_review('CHANGES_REQUESTED', 'OWNER'),
            _make_review('CHANGES_REQUESTED', 'OWNER'),
            _make_review('CHANGES_REQUESTED', 'OWNER'),
        ]
        mock_get.return_value = Mock(status_code=200, **{'json.return_value': reviews})
        assert get_pull_request_maintainer_changes_requested_count('owner/repo', 1, 'token') == 3

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_api_error_returns_zero(self, mock_logging, mock_sleep, mock_get):
        """Fail-safe: any non-200 response returns 0 (no penalty applied)."""
        mock_get.return_value = Mock(status_code=500)
        assert get_pull_request_maintainer_changes_requested_count('owner/repo', 1, 'token') == 0

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_request_exception_returns_zero(self, mock_logging, mock_sleep, mock_get):
        """Fail-safe: network errors return 0 (no penalty applied)."""
        mock_get.side_effect = requests.exceptions.RequestException('timeout')
        assert get_pull_request_maintainer_changes_requested_count('owner/repo', 1, 'token') == 0

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_uses_per_page_100(self, mock_get):
        """Ensures pagination parameter is set to avoid missing reviews."""
        mock_get.return_value = Mock(status_code=200, **{'json.return_value': []})
        get_pull_request_maintainer_changes_requested_count('owner/repo', 42, 'token')
        _, kwargs = mock_get.call_args
        assert kwargs.get('params', {}).get('per_page') == 100

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_member_association_not_counted(self, mock_get):
        """MEMBER is not in MAINTAINER_ASSOCIATIONS and should not be counted."""
        reviews = [
            _make_review('CHANGES_REQUESTED', 'MEMBER'),
        ]
        mock_get.return_value = Mock(status_code=200, **{'json.return_value': reviews})
        assert get_pull_request_maintainer_changes_requested_count('owner/repo', 1, 'token') == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
