"""
Tests for dynamic open PR threshold based on token score per tier.

Top contributors who have earned more token score in UNLOCKED tiers
get a higher threshold before the spam penalty applies.

Bonus = floor(token_score / required) for each unlocked tier (hierarchical):
- Bronze: floor(bronze_token_score / 200) - e.g., 400 score = +2 bonus
- Silver: floor(silver_token_score / 500) - requires Bronze bonus > 0
- Gold: floor(gold_token_score / 1000) - requires Bronze & Silver bonuses > 0

Run tests:
    pytest tests/validator/test_dynamic_open_pr_threshold.py -v
"""

from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAX_OPEN_PR_THRESHOLD,
)
from gittensor.validator.configurations.tier_config import Tier, TierStats
from gittensor.validator.evaluation.scoring import (
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
        """Without tier stats, threshold should be the base threshold (no bonus)."""
        assert calculate_open_pr_threshold(tier_stats=None) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_no_token_score_returns_base_threshold(self):
        """With no token score, threshold should be the base threshold."""
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_bronze_below_requirement_no_bonus(self):
        """Bronze token score below requirement doesn't grant bonus."""
        # Bronze unlocked: 70% credibility, 199 token score < 200 required
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, bronze_token_score=199.0)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_bronze_meets_requirement_gets_bonus(self):
        """Bronze token score meeting requirement grants +1 bonus."""
        # Bronze unlocked: 70% credibility, 200 token score = +1
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, bronze_token_score=200.0)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_bronze_double_requirement_gets_double_bonus(self):
        """400 Bronze token score grants +2 bonus (floor(400/200) = 2)."""
        # Bronze unlocked: 70% credibility
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, bronze_token_score=400.0)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_bronze_locked_ignores_token_score(self):
        """Bronze token score doesn't count when Bronze tier is locked."""
        # Bronze locked: 50% credibility (below 70% requirement)
        tier_stats = make_tier_stats(bronze_merged=5, bronze_closed=5, bronze_token_score=400.0)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_silver_meets_requirement_gets_bonus(self):
        """Silver token score meeting requirement grants +1 bonus (requires Bronze bonus > 0)."""
        # Bronze and Silver unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=200.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=500.0,
        )
        # Bronze bonus = 1, Silver bonus = 1
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_silver_requires_bronze_bonus(self):
        """Silver bonus requires Bronze bonus > 0."""
        # Bronze and Silver unlocked, but Bronze token score < 200
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=199.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=1000.0,
        )
        # Bronze bonus = 0, so Silver bonus = 0
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_silver_double_requirement_gets_double_bonus(self):
        """1000 Silver token score grants +2 bonus (floor(1000/500) = 2)."""
        # Bronze and Silver unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=200.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=1000.0,
        )
        # Bronze bonus = 1, Silver bonus = 2
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 3

    def test_silver_locked_ignores_token_score(self):
        """Silver token score doesn't count when Silver tier is locked."""
        # Bronze unlocked, Silver locked (below 65% credibility)
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=200.0,
            silver_merged=5,
            silver_closed=5,
            silver_token_score=1000.0,
        )
        # Only Bronze bonus = 1 (Silver locked)
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_gold_requires_bronze_and_silver_bonuses(self):
        """Gold bonus requires Bronze & Silver bonuses > 0."""
        # All tiers unlocked, but only Gold has token score
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=0.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=0.0,
            gold_merged=6,
            gold_closed=4,
            gold_token_score=1000.0,
        )
        # No Bronze or Silver bonus, so Gold bonus not granted
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_gold_bonus_with_all_requirements_met(self):
        """Gold bonus granted when Bronze & Silver bonuses > 0."""
        # All tiers unlocked with sufficient token scores
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=200.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=500.0,
            gold_merged=6,
            gold_closed=4,
            gold_token_score=1000.0,
        )
        # Bronze (+1) + Silver (+1) + Gold (+1) = 3
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 3

    def test_gold_multiplier_bonus(self):
        """2000 Gold token score grants +2 bonus (floor(2000/1000) = 2)."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=200.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=500.0,
            gold_merged=6,
            gold_closed=4,
            gold_token_score=2000.0,
        )
        # Bronze (+1) + Silver (+1) + Gold (+2) = 4
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 4

    def test_gold_locked_ignores_token_score(self):
        """Gold token score doesn't count when Gold tier is locked."""
        # Bronze and Silver unlocked, Gold locked
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=200.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=500.0,
            gold_merged=5,
            gold_closed=5,
            gold_token_score=2000.0,  # 50% < 60% required
        )
        # Bronze (+1) + Silver (+1) = 2, no Gold bonus
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_partial_requirements_partial_bonus(self):
        """Only bonuses for tiers with met requirements are granted."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=200.0,
            silver_merged=13,
            silver_closed=7,
            silver_token_score=250.0,  # < 500
            gold_merged=6,
            gold_closed=4,
            gold_token_score=0.0,
        )
        # Only Bronze (+1), Silver = floor(250/500) = 0
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_max_bonus_scenario(self):
        """Test high contributor with high token scores across all tiers."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=600.0,  # floor(600/200) = 3
            silver_merged=13,
            silver_closed=7,
            silver_token_score=1500.0,  # floor(1500/500) = 3
            gold_merged=6,
            gold_closed=4,
            gold_token_score=3000.0,  # floor(3000/1000) = 3
        )
        # Bronze (+3) + Silver (+3) + Gold (+3) = 9
        assert calculate_open_pr_threshold(tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 9

    def test_threshold_capped_at_max(self):
        """Test that threshold is capped at MAX_OPEN_PR_THRESHOLD."""
        # All tiers unlocked with very high token scores
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=2000.0,  # floor(2000/200) = 10
            silver_merged=13,
            silver_closed=7,
            silver_token_score=5000.0,  # floor(5000/500) = 10
            gold_merged=6,
            gold_closed=4,
            gold_token_score=10000.0,  # floor(10000/1000) = 10
        )
        # Would be 10 + 10 + 10 + 10 = 40, but capped at 30
        assert calculate_open_pr_threshold(tier_stats) == MAX_OPEN_PR_THRESHOLD


class TestCalculatePrSpamPenaltyMultiplier:
    """Tests for calculate_pr_spam_penalty_multiplier function."""

    def test_no_penalty_below_threshold(self):
        """No penalty when open PRs are at or below threshold."""
        assert calculate_pr_spam_penalty_multiplier(5) == 1.0
        assert calculate_pr_spam_penalty_multiplier(10) == 1.0

    def test_penalty_above_threshold(self):
        """Penalty applied when open PRs exceed threshold."""
        # 11 open PRs = 1 excess, 1 * 0.5 = 0.5 penalty, multiplier = 0.5
        assert calculate_pr_spam_penalty_multiplier(11) == 0.5

    def test_penalty_capped_at_minimum(self):
        """Penalty should not go below minimum multiplier."""
        # 15 open PRs = 5 excess, 5 * 0.5 = 2.5 penalty, multiplier = -1.5 -> capped at 0.0
        assert calculate_pr_spam_penalty_multiplier(15) == 0.0

    def test_top_contributor_with_bronze_bonus(self):
        """Top contributor with 400 Bronze token score gets +2 threshold."""
        # Bronze unlocked with 400 token score = +2
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, bronze_token_score=400.0)

        # Base (10) + Bronze (+2) = 12 threshold
        # 12 open PRs = no penalty
        assert calculate_pr_spam_penalty_multiplier(12, tier_stats) == 1.0

        # 13 open PRs = 1 excess
        assert calculate_pr_spam_penalty_multiplier(13, tier_stats) == 0.5

    def test_contributor_with_locked_tier_gets_no_bonus(self):
        """Contributors with token score in locked tiers don't get bonus threshold."""
        # Bronze locked (below 70% credibility)
        tier_stats = make_tier_stats(bronze_merged=5, bronze_closed=5, bronze_token_score=400.0)

        # 11 open PRs = 1 excess (base threshold = 10)
        assert calculate_pr_spam_penalty_multiplier(11, tier_stats) == 0.5

    def test_high_threshold_for_top_contributor(self):
        """Top contributor with multiplied bonuses gets higher threshold."""
        # All tiers unlocked with token scores
        tier_stats = make_tier_stats(
            bronze_merged=7,
            bronze_closed=3,
            bronze_token_score=400.0,  # +2
            silver_merged=13,
            silver_closed=7,
            silver_token_score=1000.0,  # +2
            gold_merged=6,
            gold_closed=4,
            gold_token_score=2000.0,  # +2
        )
        # Base (10) + Bronze (+2) + Silver (+2) + Gold (+2) = 16 threshold

        # 16 open PRs = no penalty
        assert calculate_pr_spam_penalty_multiplier(16, tier_stats) == 1.0

        # 17 open PRs = 1 excess
        assert calculate_pr_spam_penalty_multiplier(17, tier_stats) == 0.5
