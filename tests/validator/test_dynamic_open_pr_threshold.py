"""
Tests for dynamic open PR threshold based on total token score across unlocked tiers.

Bonus = floor(total_unlocked_token_score / 500)
Example: 1500 token score across unlocked tiers / 500 = +3 bonus

Multiplier is binary: 1.0 if <= threshold, 0.0 otherwise

Run tests:
    pytest tests/validator/test_dynamic_open_pr_threshold.py -v
"""

from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAX_OPEN_PR_THRESHOLD,
)
from gittensor.validator.oss_contributions.tier_config import Tier, TierStats
from gittensor.validator.oss_contributions.scoring import (
    calculate_open_pr_threshold,
    calculate_pr_spam_penalty_multiplier,
)


def make_tier_stats(
    bronze_merged=0,
    bronze_closed=0,
    bronze_token_score=0.0,
    silver_merged=0,
    silver_closed=0,
    silver_token_score=0.0,
    gold_merged=0,
    gold_closed=0,
    gold_token_score=0.0,
):
    """Create tier stats with specified merged/closed counts and token scores."""
    stats = {tier: TierStats() for tier in Tier}
    stats[Tier.BRONZE].merged_count = bronze_merged
    stats[Tier.BRONZE].closed_count = bronze_closed
    stats[Tier.BRONZE].token_score = bronze_token_score
    stats[Tier.SILVER].merged_count = silver_merged
    stats[Tier.SILVER].closed_count = silver_closed
    stats[Tier.SILVER].token_score = silver_token_score
    stats[Tier.GOLD].merged_count = gold_merged
    stats[Tier.GOLD].closed_count = gold_closed
    stats[Tier.GOLD].token_score = gold_token_score
    # Set qualified unique repos to meet requirements (3 repos needed per tier)
    stats[Tier.BRONZE].qualified_unique_repo_count = 3
    stats[Tier.SILVER].qualified_unique_repo_count = 3
    stats[Tier.GOLD].qualified_unique_repo_count = 3
    return stats


class TestCalculateOpenPrThreshold:
    """Tests for calculate_open_pr_threshold function."""

    def test_no_tier_stats_returns_base_threshold(self):
        """Without tier stats, threshold should be the base threshold."""
        assert calculate_open_pr_threshold(tier_stats=None) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_no_token_score_returns_base_threshold(self):
        """With no token score, threshold should be the base threshold."""
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_below_500_no_bonus(self):
        """Token score below 500 doesn't grant bonus."""
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, bronze_token_score=499.0)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_500_token_score_gets_bonus(self):
        """500 token score grants +1 bonus."""
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, bronze_token_score=500.0)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_1000_token_score_gets_double_bonus(self):
        """1000 token score grants +2 bonus."""
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, bronze_token_score=1000.0)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_locked_tier_ignores_token_score(self):
        """Token score from locked tiers doesn't count."""
        # Bronze locked: 50% credibility (below 70% requirement)
        tier_stats = make_tier_stats(bronze_merged=5, bronze_closed=5, bronze_token_score=1000.0)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_sum_across_unlocked_tiers(self):
        """Token scores sum across all unlocked tiers."""
        # Bronze and Silver unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=300.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=700.0,
        )
        # Total: 300 + 700 = 1000 -> floor(1000/500) = +2
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_locked_tier_excluded_from_sum(self):
        """Only unlocked tier token scores are summed."""
        # Bronze unlocked, Silver locked (50% credibility)
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=500.0,
            silver_merged=5,
            silver_closed=5,
            silver_token_score=1000.0,
        )
        # Only Bronze counts: 500 -> +1 bonus
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_all_tiers_unlocked_sum(self):
        """All unlocked tiers contribute to the sum."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=500.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=500.0,
            gold_merged=6,
            gold_closed=4,
            gold_token_score=500.0,
        )
        # Total: 500 + 500 + 500 = 1500 -> floor(1500/500) = +3
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 3

    def test_threshold_capped_at_max(self):
        """Threshold is capped at MAX_OPEN_PR_THRESHOLD."""
        # All tiers unlocked with very high token scores
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=5000.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=5000.0,
            gold_merged=6,
            gold_closed=4,
            gold_token_score=5000.0,
        )
        # Total: 15000 -> floor(15000/500) = +30, base + 30 = 40, capped at 30
        assert calculate_open_pr_threshold(tier_stats) == MAX_OPEN_PR_THRESHOLD


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
        # Bronze unlocked with 1000 token score = +2 bonus
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, bronze_token_score=1000.0)

        # Base (10) + bonus (+2) = 12 threshold
        assert calculate_pr_spam_penalty_multiplier(12, tier_stats) == 1.0
        assert calculate_pr_spam_penalty_multiplier(13, tier_stats) == 0.0

    def test_locked_tier_no_bonus(self):
        """Token score in locked tiers doesn't increase threshold."""
        # Bronze locked (below 70% credibility)
        tier_stats = make_tier_stats(bronze_merged=5, bronze_closed=5, bronze_token_score=1000.0)

        # No bonus, threshold = 10
        assert calculate_pr_spam_penalty_multiplier(10, tier_stats) == 1.0
        assert calculate_pr_spam_penalty_multiplier(11, tier_stats) == 0.0

    def test_high_threshold_for_top_contributor(self):
        """Top contributor with high token score gets higher threshold."""
        # All tiers unlocked with token scores
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=1000.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=1000.0,
            gold_merged=6,
            gold_closed=4,
            gold_token_score=1000.0,
        )
        # Total: 3000 -> floor(3000/500) = +6 bonus
        # Threshold = 10 + 6 = 16

        assert calculate_pr_spam_penalty_multiplier(16, tier_stats) == 1.0
        assert calculate_pr_spam_penalty_multiplier(17, tier_stats) == 0.0
