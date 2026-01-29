"""
Tests for dynamic open PR threshold based on merged PR history.

Top contributors who have merged more PRs in UNLOCKED tiers
get a higher threshold before the spam penalty applies.

Bonus = floor(merged_prs / required) for each unlocked tier:
- Bronze: floor(bronze_prs / 20) - e.g., 40 PRs = +2 bonus
- Silver: floor(silver_prs / 10) - e.g., 20 PRs = +2 bonus
- Gold: floor(gold_prs / 5) - requires Bronze & Silver bonuses > 0

Run tests:
    pytest tests/validator/test_dynamic_open_pr_threshold.py -v
"""

from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAX_OPEN_PR_THRESHOLD,
)
from gittensor.validator.configurations.tier_config import TIERS, Tier, TierStats
from gittensor.validator.evaluation.scoring import (
    calculate_open_pr_threshold,
    calculate_pr_spam_penalty_multiplier,
)


class MockPR:
    """Mock PullRequest for testing threshold calculation."""

    def __init__(self, tier: Tier = None):
        self.repository_tier_configuration = TIERS[tier] if tier else None


def make_tier_stats(bronze_merged=0, bronze_closed=0, silver_merged=0, silver_closed=0, gold_merged=0, gold_closed=0):
    """Create tier stats with specified merged/closed counts for testing tier unlock."""
    stats = {tier: TierStats() for tier in Tier}
    stats[Tier.BRONZE].merged_count = bronze_merged
    stats[Tier.BRONZE].closed_count = bronze_closed
    stats[Tier.SILVER].merged_count = silver_merged
    stats[Tier.SILVER].closed_count = silver_closed
    stats[Tier.GOLD].merged_count = gold_merged
    stats[Tier.GOLD].closed_count = gold_closed
    # Set qualified unique repos to meet requirements (3 repos needed per tier)
    stats[Tier.BRONZE].qualified_unique_repo_count = 3
    stats[Tier.SILVER].qualified_unique_repo_count = 3
    stats[Tier.GOLD].qualified_unique_repo_count = 3
    # Set token scores to meet requirements
    stats[Tier.SILVER].token_score = 300.0
    stats[Tier.GOLD].token_score = 500.0
    return stats


class TestCalculateOpenPrThreshold:
    """Tests for calculate_open_pr_threshold function."""

    def test_no_tier_stats_returns_base_threshold(self):
        """Without tier stats, threshold should be the base threshold (no bonus)."""
        prs = [MockPR(Tier.GOLD) for _ in range(10)]
        assert calculate_open_pr_threshold(prs, tier_stats=None) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_no_merged_prs_returns_base_threshold(self):
        """With no merged PRs, threshold should be the base threshold."""
        tier_stats = make_tier_stats()
        assert calculate_open_pr_threshold([], tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_bronze_below_requirement_no_bonus(self):
        """Bronze PRs below requirement don't grant bonus."""
        # Bronze unlocked: 70% credibility
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3)
        # 19 bronze PRs < 20 required = floor(19/20) = 0
        prs = [MockPR(Tier.BRONZE) for _ in range(19)]
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_bronze_meets_requirement_gets_bonus(self):
        """Bronze PRs meeting requirement grant +1 bonus."""
        # Bronze unlocked: 70% credibility
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3)
        # 20 bronze PRs = floor(20/20) = +1
        prs = [MockPR(Tier.BRONZE) for _ in range(20)]
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_bronze_double_requirement_gets_double_bonus(self):
        """40 Bronze PRs grant +2 bonus (floor(40/20) = 2)."""
        # Bronze unlocked: 70% credibility
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3)
        # 40 bronze PRs = floor(40/20) = +2
        prs = [MockPR(Tier.BRONZE) for _ in range(40)]
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_bronze_locked_ignores_bronze_prs(self):
        """Bronze PRs don't count when Bronze tier is locked."""
        # Bronze locked: 50% credibility (below 70% requirement)
        tier_stats = make_tier_stats(bronze_merged=5, bronze_closed=5)
        prs = [MockPR(Tier.BRONZE) for _ in range(40)]
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_silver_meets_requirement_gets_bonus(self):
        """Silver PRs meeting requirement grant +1 bonus (requires Bronze bonus > 0)."""
        # Bronze and Silver unlocked
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, silver_merged=13, silver_closed=7)
        # 20 Bronze + 10 Silver: Bronze bonus = 1, Silver bonus = 1
        prs = [MockPR(Tier.BRONZE) for _ in range(20)] + [MockPR(Tier.SILVER) for _ in range(10)]
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_silver_requires_bronze_bonus(self):
        """Silver bonus requires Bronze bonus > 0."""
        # Bronze and Silver unlocked
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, silver_merged=13, silver_closed=7)
        # 19 Bronze (bonus=0) + 20 Silver: No Silver bonus because Bronze bonus = 0
        prs = [MockPR(Tier.BRONZE) for _ in range(19)] + [MockPR(Tier.SILVER) for _ in range(20)]
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_silver_double_requirement_gets_double_bonus(self):
        """20 Silver PRs grant +2 bonus (floor(20/10) = 2), requires Bronze bonus."""
        # Bronze and Silver unlocked
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, silver_merged=13, silver_closed=7)
        # 20 Bronze + 20 Silver: Bronze bonus = 1, Silver bonus = 2
        prs = [MockPR(Tier.BRONZE) for _ in range(20)] + [MockPR(Tier.SILVER) for _ in range(20)]
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 3

    def test_silver_locked_ignores_silver_prs(self):
        """Silver PRs don't count when Silver tier is locked."""
        # Bronze unlocked, Silver locked (below 65% credibility)
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3, silver_merged=5, silver_closed=5)
        prs = [MockPR(Tier.BRONZE) for _ in range(20)] + [MockPR(Tier.SILVER) for _ in range(20)]
        # Only Bronze bonus = 1 (Silver locked)
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_gold_requires_bronze_and_silver_bonuses(self):
        """Gold bonus requires Bronze & Silver bonuses > 0."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7, bronze_closed=3,
            silver_merged=13, silver_closed=7,
            gold_merged=6, gold_closed=4
        )
        # Only 5 gold PRs, missing Bronze and Silver requirements
        prs = [MockPR(Tier.GOLD) for _ in range(5)]
        # No Bronze or Silver bonus, so Gold bonus not granted
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD

    def test_gold_bonus_with_all_requirements_met(self):
        """Gold bonus granted when Bronze & Silver bonuses > 0."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7, bronze_closed=3,
            silver_merged=13, silver_closed=7,
            gold_merged=6, gold_closed=4
        )
        # 20 Bronze + 10 Silver + 5 Gold = all requirements met
        prs = (
            [MockPR(Tier.BRONZE) for _ in range(20)]
            + [MockPR(Tier.SILVER) for _ in range(10)]
            + [MockPR(Tier.GOLD) for _ in range(5)]
        )
        # Bronze (+1) + Silver (+1) + Gold (+1) = 3
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 3

    def test_gold_multiplier_bonus(self):
        """10 Gold PRs grant +2 bonus (floor(10/5) = 2)."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7, bronze_closed=3,
            silver_merged=13, silver_closed=7,
            gold_merged=6, gold_closed=4
        )
        # 20 Bronze + 10 Silver + 10 Gold
        prs = (
            [MockPR(Tier.BRONZE) for _ in range(20)]
            + [MockPR(Tier.SILVER) for _ in range(10)]
            + [MockPR(Tier.GOLD) for _ in range(10)]
        )
        # Bronze (+1) + Silver (+1) + Gold (+2) = 4
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 4

    def test_gold_locked_ignores_gold_prs(self):
        """Gold PRs don't count when Gold tier is locked."""
        # Bronze and Silver unlocked, Gold locked
        tier_stats = make_tier_stats(
            bronze_merged=7, bronze_closed=3,
            silver_merged=13, silver_closed=7,
            gold_merged=5, gold_closed=5  # 50% < 60% required
        )
        prs = (
            [MockPR(Tier.BRONZE) for _ in range(20)]
            + [MockPR(Tier.SILVER) for _ in range(10)]
            + [MockPR(Tier.GOLD) for _ in range(10)]  # Gold locked, doesn't count
        )
        # Bronze (+1) + Silver (+1) = 2, no Gold bonus
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 2

    def test_partial_requirements_partial_bonus(self):
        """Only bonuses for tiers with met requirements are granted."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7, bronze_closed=3,
            silver_merged=13, silver_closed=7,
            gold_merged=6, gold_closed=4
        )
        # 20 Bronze (meets), 5 Silver (below 10), 0 Gold
        prs = (
            [MockPR(Tier.BRONZE) for _ in range(20)]
            + [MockPR(Tier.SILVER) for _ in range(5)]
        )
        # Only Bronze (+1), Silver = floor(5/10) = 0
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 1

    def test_max_bonus_scenario(self):
        """Test high contributor with many PRs across all tiers."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7, bronze_closed=3,
            silver_merged=13, silver_closed=7,
            gold_merged=6, gold_closed=4
        )
        # 60 Bronze + 30 Silver + 15 Gold
        prs = (
            [MockPR(Tier.BRONZE) for _ in range(60)]  # floor(60/20) = 3
            + [MockPR(Tier.SILVER) for _ in range(30)]  # floor(30/10) = 3
            + [MockPR(Tier.GOLD) for _ in range(15)]  # floor(15/5) = 3
        )
        # Bronze (+3) + Silver (+3) + Gold (+3) = 9
        assert calculate_open_pr_threshold(prs, tier_stats) == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + 9

    def test_threshold_capped_at_max(self):
        """Test that threshold is capped at MAX_OPEN_PR_THRESHOLD."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7, bronze_closed=3,
            silver_merged=13, silver_closed=7,
            gold_merged=6, gold_closed=4
        )
        # 200 Bronze + 100 Silver + 50 Gold = would be +10 +10 +10 = +30 bonus
        # But capped at MAX_OPEN_PR_THRESHOLD (30)
        prs = (
            [MockPR(Tier.BRONZE) for _ in range(200)]  # floor(200/20) = 10
            + [MockPR(Tier.SILVER) for _ in range(100)]  # floor(100/10) = 10
            + [MockPR(Tier.GOLD) for _ in range(50)]  # floor(50/5) = 10
        )
        # Would be 10 + 30 = 40, but capped at 30
        assert calculate_open_pr_threshold(prs, tier_stats) == MAX_OPEN_PR_THRESHOLD


class TestCalculatePrSpamPenaltyMultiplier:
    """Tests for calculate_pr_spam_penalty_multiplier function."""

    def test_no_penalty_below_threshold(self):
        """No penalty when open PRs are at or below threshold."""
        merged_prs = []
        assert calculate_pr_spam_penalty_multiplier(5, merged_prs) == 1.0
        assert calculate_pr_spam_penalty_multiplier(10, merged_prs) == 1.0

    def test_penalty_above_threshold(self):
        """Penalty applied when open PRs exceed threshold."""
        merged_prs = []
        # 11 open PRs = 1 excess, 1 * 0.5 = 0.5 penalty, multiplier = 0.5
        assert calculate_pr_spam_penalty_multiplier(11, merged_prs) == 0.5

    def test_penalty_capped_at_minimum(self):
        """Penalty should not go below minimum multiplier."""
        merged_prs = []
        # 15 open PRs = 5 excess, 5 * 0.5 = 2.5 penalty, multiplier = -1.5 -> capped at 0.0
        assert calculate_pr_spam_penalty_multiplier(15, merged_prs) == 0.0

    def test_top_contributor_with_bronze_bonus(self):
        """Top contributor with 40 Bronze PRs gets +2 threshold."""
        # Bronze unlocked
        tier_stats = make_tier_stats(bronze_merged=7, bronze_closed=3)
        # 40 bronze PRs = floor(40/20) = +2
        merged_prs = [MockPR(Tier.BRONZE) for _ in range(40)]

        # Base (10) + Bronze (+2) = 12 threshold
        # 12 open PRs = no penalty
        assert calculate_pr_spam_penalty_multiplier(12, merged_prs, tier_stats) == 1.0

        # 13 open PRs = 1 excess
        assert calculate_pr_spam_penalty_multiplier(13, merged_prs, tier_stats) == 0.5

    def test_contributor_with_locked_tier_gets_no_bonus(self):
        """Contributors with PRs in locked tiers don't get bonus threshold."""
        # Bronze locked (below 70% credibility)
        tier_stats = make_tier_stats(bronze_merged=5, bronze_closed=5)
        # 40 bronze PRs, but Bronze is locked so no bonus
        merged_prs = [MockPR(Tier.BRONZE) for _ in range(40)]

        # 11 open PRs = 1 excess (base threshold = 10)
        assert calculate_pr_spam_penalty_multiplier(11, merged_prs, tier_stats) == 0.5

    def test_high_threshold_for_top_contributor(self):
        """Top contributor with multiplied bonuses gets higher threshold."""
        # All tiers unlocked
        tier_stats = make_tier_stats(
            bronze_merged=7, bronze_closed=3,
            silver_merged=13, silver_closed=7,
            gold_merged=6, gold_closed=4
        )
        # 40 Bronze + 20 Silver + 10 Gold
        # Base (10) + Bronze (+2) + Silver (+2) + Gold (+2) = 16 threshold
        merged_prs = (
            [MockPR(Tier.BRONZE) for _ in range(40)]
            + [MockPR(Tier.SILVER) for _ in range(20)]
            + [MockPR(Tier.GOLD) for _ in range(10)]
        )

        # 16 open PRs = no penalty
        assert calculate_pr_spam_penalty_multiplier(16, merged_prs, tier_stats) == 1.0

        # 17 open PRs = 1 excess
        assert calculate_pr_spam_penalty_multiplier(17, merged_prs, tier_stats) == 0.5
