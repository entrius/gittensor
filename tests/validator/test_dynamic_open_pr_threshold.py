"""
Tests for dynamic open PR threshold based on total token score.

Bonus = floor(total_token_score / 300)
Example: 900 total token score / 300 = +3 bonus

Multiplier is binary: 1.0 if <= threshold, 0.0 otherwise

Run tests:
    pytest tests/validator/test_dynamic_open_pr_threshold.py -v
"""

from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAX_OPEN_PR_THRESHOLD,
)
from gittensor.validator.oss_contributions.scoring import (
    calculate_open_pr_threshold,
    calculate_pr_spam_penalty_multiplier,
)


class TestCalculateOpenPrThreshold:
    """Tests for calculate_open_pr_threshold function."""

    def test_no_token_score_returns_base_threshold(self):
        """Without token score, threshold should be the base threshold."""
        assert calculate_open_pr_threshold() == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_zero_token_score_returns_base_threshold(self):
        """With zero token score, threshold should be the base threshold."""
        assert calculate_open_pr_threshold(0.0) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_below_300_no_bonus(self):
        """Token score below 300 doesn't grant bonus."""
        assert calculate_open_pr_threshold(299.0) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_300_token_score_gets_bonus(self):
        """300 token score grants +1 bonus."""
        assert calculate_open_pr_threshold(300.0) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_600_token_score_gets_double_bonus(self):
        """600 token score grants +2 bonus."""
        assert calculate_open_pr_threshold(600.0) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_900_token_score_gets_triple_bonus(self):
        """900 token score grants +3 bonus."""
        assert calculate_open_pr_threshold(900.0) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 3

    def test_threshold_capped_at_max(self):
        """Threshold is capped at MAX_OPEN_PR_THRESHOLD."""
        assert calculate_open_pr_threshold(50000.0) == MAX_OPEN_PR_THRESHOLD


class TestCalculatePrSpamPenaltyMultiplier:
    """Tests for calculate_pr_spam_penalty_multiplier function (binary multiplier)."""

    def test_no_penalty_below_threshold(self):
        """No penalty when open PRs are below threshold."""
        assert calculate_pr_spam_penalty_multiplier(5) == 1.0

    def test_no_penalty_at_threshold(self):
        """No penalty when open PRs are exactly at threshold."""
        assert calculate_pr_spam_penalty_multiplier(10) == 1.0

    def test_zero_multiplier_above_threshold(self):
        """Multiplier is 0.0 when open PRs exceed threshold."""
        assert calculate_pr_spam_penalty_multiplier(11) == 0.0

    def test_zero_multiplier_well_above_threshold(self):
        """Multiplier is 0.0 regardless of how far above threshold."""
        assert calculate_pr_spam_penalty_multiplier(20) == 0.0

    def test_bonus_increases_threshold(self):
        """Token score bonus increases the threshold."""
        # 600 token score = +2 bonus -> threshold = 12
        assert calculate_pr_spam_penalty_multiplier(12, 600.0) == 1.0
        assert calculate_pr_spam_penalty_multiplier(13, 600.0) == 0.0

    def test_high_threshold_for_top_contributor(self):
        """Top contributor with high token score gets higher threshold."""
        # 1800 token score -> floor(1800/300) = +6 bonus -> threshold = 16
        assert calculate_pr_spam_penalty_multiplier(16, 1800.0) == 1.0
        assert calculate_pr_spam_penalty_multiplier(17, 1800.0) == 0.0
