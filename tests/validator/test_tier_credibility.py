# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for tier credibility and unlocking logic.

Uses pytest fixtures from conftest.py for clean, reusable test data.

Run tests:
    pytest tests/validator/test_tier_credibility.py -v

Run specific test class:
    pytest tests/validator/test_tier_credibility.py::TestTierUnlocking -v
"""

import pytest

from gittensor.classes import PRState
from gittensor.validator.configurations.tier_config import TIERS, Tier, TierStats
from gittensor.validator.evaluation.credibility import (
    calculate_credibility_per_tier,
    calculate_tier_stats,
    is_tier_unlocked,
)

# ============================================================================
# TierStats Tests
# ============================================================================


class TestTierStats:
    """Test TierStats dataclass properties."""

    def test_total_attempts_calculation(self):
        stats = TierStats(merged_count=5, closed_count=3)
        assert stats.total_attempts == 8

    def test_total_attempts_zero(self):
        stats = TierStats()
        assert stats.total_attempts == 0

    def test_total_prs_includes_open(self):
        stats = TierStats(merged_count=5, closed_count=3, open_count=2)
        assert stats.total_prs == 10

    def test_credibility_formula(self):
        stats = TierStats(merged_count=7, closed_count=3)
        assert stats.credibility == 0.7

    def test_credibility_100_percent(self):
        stats = TierStats(merged_count=10, closed_count=0)
        assert stats.credibility == 1.0

    def test_credibility_0_percent(self):
        stats = TierStats(merged_count=0, closed_count=10)
        assert stats.credibility == 0.0

    def test_credibility_no_attempts_is_zero(self):
        stats = TierStats()
        assert stats.credibility == 0.0

    def test_open_prs_dont_affect_credibility(self):
        stats = TierStats(merged_count=5, closed_count=5, open_count=100)
        assert stats.credibility == 0.5
        assert stats.total_attempts == 10  # Excludes open


# ============================================================================
# calculate_tier_stats Tests
# ============================================================================


class TestCalculateTierStats:
    """Test calculate_tier_stats function."""

    def test_empty_lists(self):
        stats = calculate_tier_stats([], [], [])
        for tier in Tier:
            assert stats[tier].merged_count == 0
            assert stats[tier].closed_count == 0
            assert stats[tier].open_count == 0

    def test_counts_merged_per_tier(self, pr_factory, bronze_config, silver_config, gold_config):
        merged = [
            pr_factory.merged(bronze_config),
            pr_factory.merged(bronze_config),
            pr_factory.merged(silver_config),
            pr_factory.merged(gold_config),
        ]

        stats = calculate_tier_stats(merged, [], [])

        assert stats[Tier.BRONZE].merged_count == 2
        assert stats[Tier.SILVER].merged_count == 1
        assert stats[Tier.GOLD].merged_count == 1

    def test_counts_closed_per_tier(self, pr_factory, bronze_config, silver_config):
        closed = [
            pr_factory.closed(bronze_config),
            pr_factory.closed(silver_config),
            pr_factory.closed(silver_config),
        ]

        stats = calculate_tier_stats([], closed, [])

        assert stats[Tier.BRONZE].closed_count == 1
        assert stats[Tier.SILVER].closed_count == 2
        assert stats[Tier.GOLD].closed_count == 0

    def test_counts_open_per_tier(self, pr_factory, bronze_config, gold_config):
        open_prs = [
            pr_factory.open(bronze_config),
            pr_factory.open(bronze_config),
            pr_factory.open(gold_config),
        ]

        stats = calculate_tier_stats([], [], open_prs)

        assert stats[Tier.BRONZE].open_count == 2
        assert stats[Tier.SILVER].open_count == 0
        assert stats[Tier.GOLD].open_count == 1

    def test_scoring_details_off_by_default(self, pr_factory, bronze_config):
        merged = [pr_factory.merged(bronze_config, earned_score=999.0)]
        stats = calculate_tier_stats(merged, [], [])
        assert stats[Tier.BRONZE].earned_score == 0.0

    def test_scoring_details_included_when_requested(self, pr_factory, bronze_config):
        merged = [
            pr_factory.merged(bronze_config, earned_score=100.0),
            pr_factory.merged(bronze_config, earned_score=150.0),
        ]
        open_prs = [pr_factory.open(bronze_config, collateral_score=25.0)]

        stats = calculate_tier_stats(merged, [], open_prs, include_scoring_details=True)

        assert stats[Tier.BRONZE].earned_score == 250.0
        assert stats[Tier.BRONZE].collateral_score == 25.0

    def test_ignores_prs_without_tier_config(self, pr_factory, bronze_config):
        from datetime import datetime, timezone

        from gittensor.classes import PullRequest

        pr_no_tier = PullRequest(
            number=1,
            repository_full_name="test/repo",
            uid=0,
            hotkey="test",
            github_id="123",
            title="No tier",
            author_login="test",
            merged_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            pr_state=PRState.MERGED,
            repository_tier_configuration=None,
        )

        stats = calculate_tier_stats([pr_no_tier], [], [])

        for tier in Tier:
            assert stats[tier].merged_count == 0


# ============================================================================
# is_tier_unlocked Tests
# ============================================================================


class TestTierUnlocking:
    """Test is_tier_unlocked function."""

    def test_bronze_always_unlocked(self, empty_tier_stats):
        assert is_tier_unlocked(Tier.BRONZE, empty_tier_stats) is True

    def test_silver_requires_3_merges(self):
        # 2 merges - not enough
        stats = {
            Tier.BRONZE: TierStats(),
            Tier.SILVER: TierStats(merged_count=2, closed_count=0),
            Tier.GOLD: TierStats(),
        }
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # 3 merges - exactly enough
        stats[Tier.SILVER] = TierStats(merged_count=3, closed_count=0)
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_silver_requires_50_percent_credibility(self):
        # 49% - not enough
        stats = {
            Tier.BRONZE: TierStats(),
            Tier.SILVER: TierStats(merged_count=49, closed_count=51),
            Tier.GOLD: TierStats(),
        }
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # 50% - exactly enough
        stats[Tier.SILVER] = TierStats(merged_count=3, closed_count=3)
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_silver_requires_both_conditions(self):
        stats = {
            Tier.BRONZE: TierStats(),
            Tier.SILVER: TierStats(merged_count=3, closed_count=7),  # 30%
            Tier.GOLD: TierStats(),
        }
        # Has merges but low credibility
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Has credibility but not merges
        stats[Tier.SILVER] = TierStats(merged_count=2, closed_count=1)
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Has both
        stats[Tier.SILVER] = TierStats(merged_count=3, closed_count=2)
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_gold_requires_5_merges(self, silver_unlocked_stats):
        # 4 merges - not enough
        silver_unlocked_stats[Tier.GOLD] = TierStats(merged_count=4, closed_count=0)
        assert is_tier_unlocked(Tier.GOLD, silver_unlocked_stats) is False

        # 5 merges - exactly enough
        silver_unlocked_stats[Tier.GOLD] = TierStats(merged_count=5, closed_count=0)
        assert is_tier_unlocked(Tier.GOLD, silver_unlocked_stats) is True

    def test_gold_requires_70_percent_credibility(self, silver_unlocked_stats):
        # 69% - not enough
        silver_unlocked_stats[Tier.GOLD] = TierStats(merged_count=69, closed_count=31)
        assert is_tier_unlocked(Tier.GOLD, silver_unlocked_stats) is False

        # 70% - exactly enough
        silver_unlocked_stats[Tier.GOLD] = TierStats(merged_count=7, closed_count=3)
        assert is_tier_unlocked(Tier.GOLD, silver_unlocked_stats) is True

    def test_gold_requires_silver_unlocked(self):
        # Gold has perfect stats, but Silver is locked
        stats = {
            Tier.BRONZE: TierStats(),
            Tier.SILVER: TierStats(merged_count=2, closed_count=0),  # Only 2 merges
            Tier.GOLD: TierStats(merged_count=10, closed_count=0),  # Perfect
        }
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Unlock Silver
        stats[Tier.SILVER] = TierStats(merged_count=3, closed_count=0)
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_cascading_unlock(self, gold_unlocked_stats):
        assert is_tier_unlocked(Tier.BRONZE, gold_unlocked_stats) is True
        assert is_tier_unlocked(Tier.SILVER, gold_unlocked_stats) is True
        assert is_tier_unlocked(Tier.GOLD, gold_unlocked_stats) is True


# ============================================================================
# calculate_credibility_per_tier Tests
# ============================================================================


class TestCredibilityCalculation:
    """Test calculate_credibility_per_tier function."""

    def test_no_activity_returns_empty(self):
        result = calculate_credibility_per_tier([], [])
        assert result == {}

    def test_single_tier_credibility(self, pr_factory, bronze_config):
        merged = pr_factory.merged_batch(bronze_config, count=3)
        closed = pr_factory.closed_batch(bronze_config, count=1)

        result = calculate_credibility_per_tier(merged, closed)

        assert result[Tier.BRONZE] == 0.75

    def test_below_activation_threshold_returns_1(self, pr_factory, bronze_config):
        # Only 1 attempt (threshold is 2)
        merged = [pr_factory.merged(bronze_config)]
        result = calculate_credibility_per_tier(merged, [])
        assert result[Tier.BRONZE] == 1.0

    def test_at_activation_threshold_calculates(self, pr_factory, bronze_config):
        # Exactly 2 attempts
        merged = [pr_factory.merged(bronze_config)]
        closed = [pr_factory.closed(bronze_config)]

        result = calculate_credibility_per_tier(merged, closed)

        assert result[Tier.BRONZE] == 0.5

    def test_locked_tier_returns_zero(self, pr_factory, silver_config):
        # Silver has PRs but doesn't meet requirements
        merged = pr_factory.merged_batch(silver_config, count=2)  # Need 3
        closed = [pr_factory.closed(silver_config)]

        result = calculate_credibility_per_tier(merged, closed)

        assert result[Tier.SILVER] == 0.0

    def test_100_percent_credibility(self, pr_factory, bronze_config):
        merged = pr_factory.merged_batch(bronze_config, count=5)
        result = calculate_credibility_per_tier(merged, [])
        assert result[Tier.BRONZE] == 1.0

    def test_0_percent_credibility(self, pr_factory, bronze_config):
        closed = pr_factory.closed_batch(bronze_config, count=5)
        result = calculate_credibility_per_tier([], closed)
        assert result[Tier.BRONZE] == 0.0


# ============================================================================
# Tier Demotion Tests
# ============================================================================


class TestTierDemotion:
    """Test tier demotion scenarios."""

    def test_gold_demoted_when_credibility_drops(self, demoted_from_gold_miner):
        """Gold locks when credibility drops below 70%."""
        stats = calculate_tier_stats(demoted_from_gold_miner.merged, demoted_from_gold_miner.closed)
        credibility = calculate_credibility_per_tier(demoted_from_gold_miner.merged, demoted_from_gold_miner.closed)

        # Silver still OK
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert credibility[Tier.SILVER] == 1.0

        # Gold LOCKED
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert credibility[Tier.GOLD] == 0.0

    def test_gold_demoted_not_enough_merges(self, pr_factory, silver_config, gold_config):
        """Gold locks when merge count drops below 5."""
        merged = pr_factory.merged_batch(silver_config, count=3) + pr_factory.merged_batch(
            gold_config, count=4
        )  # Need 5

        stats = calculate_tier_stats(merged, [])
        credibility = calculate_credibility_per_tier(merged, [])

        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert credibility[Tier.GOLD] == 0.0

    def test_silver_demotion_cascades_to_gold(self, cascade_demoted_miner):
        """When Silver locks, Gold also locks (even with perfect Gold stats)."""
        stats = calculate_tier_stats(cascade_demoted_miner.merged, cascade_demoted_miner.closed)
        credibility = calculate_credibility_per_tier(cascade_demoted_miner.merged, cascade_demoted_miner.closed)

        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert credibility[Tier.SILVER] == 0.0
        assert credibility[Tier.GOLD] == 0.0

    def test_silver_demoted_when_credibility_drops(self, demoted_from_silver_miner):
        """Silver locks when credibility drops below 50%."""
        stats = calculate_tier_stats(demoted_from_silver_miner.merged, demoted_from_silver_miner.closed)

        assert is_tier_unlocked(Tier.SILVER, stats) is False

    def test_recovery_from_demotion(self, pr_factory, silver_config, gold_config):
        """Miner can recover from demotion by getting more merges."""
        # Initially demoted: 5/8 = 62.5%
        merged = pr_factory.merged_batch(silver_config, count=3) + pr_factory.merged_batch(gold_config, count=5)
        closed = pr_factory.closed_batch(gold_config, count=3)

        stats = calculate_tier_stats(merged, closed)
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Recovery: add 2 more merges -> 7/10 = 70%
        merged.extend(pr_factory.merged_batch(gold_config, count=2))

        stats = calculate_tier_stats(merged, closed)
        credibility = calculate_credibility_per_tier(merged, closed)

        assert is_tier_unlocked(Tier.GOLD, stats) is True
        assert credibility[Tier.GOLD] == 0.7

    def test_spam_destroys_all_tiers(self, spammer_miner):
        """Massive closed PRs tanks credibility everywhere."""
        stats = calculate_tier_stats(spammer_miner.merged, spammer_miner.closed)
        credibility = calculate_credibility_per_tier(spammer_miner.merged, spammer_miner.closed)

        # Bronze: still unlocked but terrible credibility
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert credibility[Tier.BRONZE] == pytest.approx(0.2, abs=0.01)

        # Silver & Gold: LOCKED
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False

    def test_gradual_decline(self, pr_factory, silver_config, gold_config):
        """Miner starts strong then declines."""
        # Phase 1: Strong start
        merged = pr_factory.merged_batch(silver_config, count=5) + pr_factory.merged_batch(gold_config, count=8)

        stats = calculate_tier_stats(merged, [])
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Phase 2: Decline
        closed = pr_factory.closed_batch(gold_config, count=5)

        stats = calculate_tier_stats(merged, closed)
        credibility = calculate_credibility_per_tier(merged, closed)

        # Gold now LOCKED (8/13 = 61.5%)
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert credibility[Tier.GOLD] == 0.0

        # Silver still OK
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert credibility[Tier.SILVER] == 1.0


# ============================================================================
# Mixed Performance Tests
# ============================================================================


class TestMixedPerformance:
    """Test miners with varying performance across tiers."""

    def test_mixed_tier_performance(self, mixed_performance_miner):
        """Different credibility at each tier."""
        stats = calculate_tier_stats(mixed_performance_miner.merged, mixed_performance_miner.closed)
        credibility = calculate_credibility_per_tier(mixed_performance_miner.merged, mixed_performance_miner.closed)

        # Bronze: 90%
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert credibility[Tier.BRONZE] == pytest.approx(0.9, abs=0.01)

        # Silver: 55% (above 50% threshold)
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert credibility[Tier.SILVER] == pytest.approx(0.55, abs=0.01)

        # Gold: 60% (below 70% threshold) - LOCKED
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert credibility[Tier.GOLD] == 0.0

    def test_perfect_miner(self, perfect_miner):
        """100% credibility everywhere."""
        stats = calculate_tier_stats(perfect_miner.merged, [])
        credibility = calculate_credibility_per_tier(perfect_miner.merged, [])

        for tier in Tier:
            if tier in credibility:
                assert credibility[tier] == 1.0


# ============================================================================
# Edge Cases & Boundary Tests
# ============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_exactly_at_silver_threshold(self, silver_threshold_miner):
        """Test exactly 50% at Silver."""
        stats = calculate_tier_stats(silver_threshold_miner.merged, silver_threshold_miner.closed)
        credibility = calculate_credibility_per_tier(silver_threshold_miner.merged, silver_threshold_miner.closed)

        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert credibility[Tier.SILVER] == 0.5

    def test_exactly_at_gold_threshold(self, gold_threshold_miner):
        """Test exactly 70% at Gold."""
        stats = calculate_tier_stats(gold_threshold_miner.merged, gold_threshold_miner.closed)
        credibility = calculate_credibility_per_tier(gold_threshold_miner.merged, gold_threshold_miner.closed)

        assert is_tier_unlocked(Tier.GOLD, stats) is True
        assert credibility[Tier.GOLD] == 0.7

    def test_one_below_merge_threshold(self, pr_factory, silver_config, gold_config):
        """Just one merge short at each tier."""
        merged = [
            *pr_factory.merged_batch(silver_config, count=2),  # Need 3
            *pr_factory.merged_batch(gold_config, count=4),  # Need 5
        ]

        stats = calculate_tier_stats(merged, [])

        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False

    def test_credibility_just_below_threshold(self, pr_factory, silver_config):
        """49.9% credibility (just below 50%)."""
        merged = pr_factory.merged_batch(silver_config, count=499)
        closed = pr_factory.closed_batch(silver_config, count=501)

        stats = calculate_tier_stats(merged, closed)

        assert stats[Tier.SILVER].credibility == pytest.approx(0.499, abs=0.001)
        assert is_tier_unlocked(Tier.SILVER, stats) is False

    def test_single_pr_at_each_tier(self, pr_factory, bronze_config, silver_config, gold_config):
        """Single PR behavior depends on tier unlock status."""
        # Bronze: always unlocked, 1 PR = 1.0 (below activation threshold)
        merged = [pr_factory.merged(bronze_config)]
        credibility = calculate_credibility_per_tier(merged, [])
        assert credibility[Tier.BRONZE] == 1.0

        # Silver: NOT unlocked (need 3 bronze merges + 50% cred), so 0.0
        merged = [pr_factory.merged(silver_config)]
        credibility = calculate_credibility_per_tier(merged, [])
        assert credibility[Tier.SILVER] == 0.0

        # Gold: NOT unlocked (need silver unlocked + 5 merges + 70% cred), so 0.0
        merged = [pr_factory.merged(gold_config)]
        credibility = calculate_credibility_per_tier(merged, [])
        assert credibility[Tier.GOLD] == 0.0

    def test_activation_threshold_boundary(self, pr_factory, bronze_config):
        """Test 1 attempt vs 2 attempts."""
        # 1 attempt: below threshold
        merged = [pr_factory.merged(bronze_config)]
        cred = calculate_credibility_per_tier(merged, [])
        assert cred[Tier.BRONZE] == 1.0

        # 2 attempts: at threshold, calculates actual
        closed = [pr_factory.closed(bronze_config)]
        cred = calculate_credibility_per_tier(merged, closed)
        assert cred[Tier.BRONZE] == 0.5

    def test_large_numbers(self, pr_factory, silver_config, gold_config):
        """Large PR counts for precision testing."""
        merged = pr_factory.merged_batch(silver_config, count=100) + pr_factory.merged_batch(gold_config, count=1000)
        closed = pr_factory.closed_batch(gold_config, count=429)

        stats = calculate_tier_stats(merged, closed)

        assert stats[Tier.GOLD].merged_count == 1000
        assert stats[Tier.GOLD].closed_count == 429
        # 1000/1429 = 69.98% - just below 70%
        assert stats[Tier.GOLD].credibility == pytest.approx(0.6998, abs=0.001)


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegration:
    """Integration tests using pre-built miner scenarios."""

    def test_new_miner_only_bronze(self, new_miner):
        """New miner has no tiers unlocked except Bronze."""
        stats = calculate_tier_stats(new_miner.merged, new_miner.closed)

        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False

    def test_bronze_miner_scenario(self, bronze_miner):
        """Bronze-only miner."""
        stats = calculate_tier_stats(bronze_miner.merged, bronze_miner.closed)
        cred = calculate_credibility_per_tier(bronze_miner.merged, bronze_miner.closed)

        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert cred[Tier.BRONZE] == 0.75

    def test_silver_miner_scenario(self, silver_unlocked_miner):
        """Silver miner."""
        stats = calculate_tier_stats(silver_unlocked_miner.merged, silver_unlocked_miner.closed)
        cred = calculate_credibility_per_tier(silver_unlocked_miner.merged, silver_unlocked_miner.closed)

        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert cred[Tier.SILVER] == pytest.approx(0.67, abs=0.01)

    def test_gold_miner_scenario(self, gold_unlocked_miner):
        """Gold miner."""
        stats = calculate_tier_stats(gold_unlocked_miner.merged, gold_unlocked_miner.closed)
        cred = calculate_credibility_per_tier(gold_unlocked_miner.merged, gold_unlocked_miner.closed)

        assert is_tier_unlocked(Tier.GOLD, stats) is True
        assert cred[Tier.GOLD] == 0.7

    def test_open_prs_tracked_separately(self, miner_with_open_prs):
        """Open PRs are counted but don't affect credibility."""
        stats = calculate_tier_stats(miner_with_open_prs.merged, miner_with_open_prs.closed, miner_with_open_prs.open)

        # Open PRs are counted
        assert stats[Tier.BRONZE].open_count == 2
        assert stats[Tier.SILVER].open_count == 3

        # But don't affect credibility calculation
        cred = calculate_credibility_per_tier(miner_with_open_prs.merged, miner_with_open_prs.closed)
        assert cred[Tier.BRONZE] == 0.75  # 3/(3+1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
