#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for tier credibility and unlocking logic.

Tests cover:
- TierStats properties (total_attempts, credibility, total_prs)
- calculate_tier_stats() with various PR combinations
- is_tier_unlocked() for all tiers and edge cases
- calculate_credibility_per_tier() with complex scenarios
"""

import pytest
from datetime import datetime, timezone
from typing import List

from gittensor.classes import PullRequest, PRState
from gittensor.validator.configurations.tier_config import (
    Tier,
    TierStats,
    TierConfig,
    TIERS,
)
from gittensor.validator.evaluation.credibility import (
    calculate_tier_stats,
    is_tier_unlocked,
    calculate_credibility_per_tier,
)


# ============================================================================
# Test Fixtures
# ============================================================================

def create_mock_pr(
    number: int,
    state: PRState,
    tier_config: TierConfig,
    earned_score: float = 100.0,
    collateral_score: float = 20.0,
) -> PullRequest:
    """Create a mock PullRequest for testing."""
    return PullRequest(
        number=number,
        repository_full_name="test/repo",
        uid=0,
        hotkey="test_hotkey",
        github_id="12345",
        title="Test PR",
        author_login="testuser",
        merged_at=datetime.now(timezone.utc) if state == PRState.MERGED else None,
        created_at=datetime.now(timezone.utc),
        pr_state=state,
        repository_tier_configuration=tier_config,
        earned_score=earned_score,
        collateral_score=collateral_score,
    )


# ============================================================================
# TierStats Tests
# ============================================================================

class TestTierStats:
    """Test TierStats dataclass properties."""

    def test_total_attempts_calculation(self):
        """Test that total_attempts = merged_count + closed_count."""
        stats = TierStats(merged_count=5, closed_count=3)
        assert stats.total_attempts == 8

    def test_total_attempts_zero(self):
        """Test total_attempts with no PRs."""
        stats = TierStats()
        assert stats.total_attempts == 0

    def test_total_prs_calculation(self):
        """Test that total_prs = merged + closed + open."""
        stats = TierStats(merged_count=5, closed_count=3, open_count=2)
        assert stats.total_prs == 10

    def test_credibility_calculation(self):
        """Test credibility = merged / (merged + closed)."""
        stats = TierStats(merged_count=7, closed_count=3)
        assert stats.credibility == 0.7

    def test_credibility_all_merged(self):
        """Test credibility = 1.0 when all PRs are merged."""
        stats = TierStats(merged_count=10, closed_count=0)
        assert stats.credibility == 1.0

    def test_credibility_all_closed(self):
        """Test credibility = 0.0 when all PRs are closed."""
        stats = TierStats(merged_count=0, closed_count=10)
        assert stats.credibility == 0.0

    def test_credibility_no_attempts(self):
        """Test credibility = 0.0 when there are no attempts."""
        stats = TierStats()
        assert stats.credibility == 0.0

    def test_credibility_with_open_prs(self):
        """Test that open PRs don't affect credibility calculation."""
        stats = TierStats(merged_count=5, closed_count=5, open_count=10)
        assert stats.credibility == 0.5
        assert stats.total_attempts == 10  # open PRs not included


# ============================================================================
# calculate_tier_stats Tests
# ============================================================================

class TestCalculateTierStats:
    """Test calculate_tier_stats function."""

    def test_empty_pr_lists(self):
        """Test with no PRs."""
        stats = calculate_tier_stats([], [], [])

        # All tiers should have zero counts
        for tier in Tier:
            assert stats[tier].merged_count == 0
            assert stats[tier].closed_count == 0
            assert stats[tier].open_count == 0

    def test_count_merged_prs_per_tier(self):
        """Test counting merged PRs per tier."""
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(3, PRState.MERGED, TIERS[Tier.SILVER]),
            create_mock_pr(4, PRState.MERGED, TIERS[Tier.GOLD]),
        ]

        stats = calculate_tier_stats(merged_prs, [], [])

        assert stats[Tier.BRONZE].merged_count == 2
        assert stats[Tier.SILVER].merged_count == 1
        assert stats[Tier.GOLD].merged_count == 1

    def test_count_closed_prs_per_tier(self):
        """Test counting closed PRs per tier."""
        closed_prs = [
            create_mock_pr(1, PRState.CLOSED, TIERS[Tier.BRONZE]),
            create_mock_pr(2, PRState.CLOSED, TIERS[Tier.SILVER]),
            create_mock_pr(3, PRState.CLOSED, TIERS[Tier.SILVER]),
        ]

        stats = calculate_tier_stats([], closed_prs, [])

        assert stats[Tier.BRONZE].closed_count == 1
        assert stats[Tier.SILVER].closed_count == 2
        assert stats[Tier.GOLD].closed_count == 0

    def test_count_open_prs_per_tier(self):
        """Test counting open PRs per tier."""
        open_prs = [
            create_mock_pr(1, PRState.OPEN, TIERS[Tier.BRONZE]),
            create_mock_pr(2, PRState.OPEN, TIERS[Tier.BRONZE]),
            create_mock_pr(3, PRState.OPEN, TIERS[Tier.GOLD]),
        ]

        stats = calculate_tier_stats([], [], open_prs)

        assert stats[Tier.BRONZE].open_count == 2
        assert stats[Tier.SILVER].open_count == 0
        assert stats[Tier.GOLD].open_count == 1

    def test_scoring_details_not_included_by_default(self):
        """Test that scoring details are not included by default."""
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE], earned_score=100.0),
        ]

        stats = calculate_tier_stats(merged_prs, [], [])

        assert stats[Tier.BRONZE].earned_score == 0.0

    def test_scoring_details_included_when_requested(self):
        """Test that scoring details are included when requested."""
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE], earned_score=100.0),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.BRONZE], earned_score=150.0),
        ]
        open_prs = [
            create_mock_pr(3, PRState.OPEN, TIERS[Tier.BRONZE], collateral_score=20.0),
        ]

        stats = calculate_tier_stats(merged_prs, [], open_prs, include_scoring_details=True)

        assert stats[Tier.BRONZE].earned_score == 250.0
        assert stats[Tier.BRONZE].collateral_score == 20.0

    def test_mixed_pr_states_and_tiers(self):
        """Test with mixed PR states across multiple tiers."""
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.SILVER]),
        ]
        closed_prs = [
            create_mock_pr(3, PRState.CLOSED, TIERS[Tier.BRONZE]),
        ]
        open_prs = [
            create_mock_pr(4, PRState.OPEN, TIERS[Tier.GOLD]),
        ]

        stats = calculate_tier_stats(merged_prs, closed_prs, open_prs)

        assert stats[Tier.BRONZE].merged_count == 1
        assert stats[Tier.BRONZE].closed_count == 1
        assert stats[Tier.BRONZE].open_count == 0
        assert stats[Tier.BRONZE].total_attempts == 2

        assert stats[Tier.SILVER].merged_count == 1
        assert stats[Tier.SILVER].closed_count == 0

        assert stats[Tier.GOLD].open_count == 1

    def test_pr_without_tier_configuration_ignored(self):
        """Test that PRs without tier configuration are ignored."""
        pr_without_tier = PullRequest(
            number=1,
            repository_full_name="test/repo",
            uid=0,
            hotkey="test_hotkey",
            github_id="12345",
            title="Test",
            author_login="testuser",
            merged_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            pr_state=PRState.MERGED,
            repository_tier_configuration=None,
        )

        stats = calculate_tier_stats([pr_without_tier], [], [])

        # All counts should be zero since PR has no tier config
        for tier in Tier:
            assert stats[tier].merged_count == 0


# ============================================================================
# is_tier_unlocked Tests
# ============================================================================

class TestIsTierUnlocked:
    """Test is_tier_unlocked function."""

    def test_bronze_always_unlocked(self):
        """Test that Bronze tier is always unlocked."""
        stats = {
            Tier.BRONZE: TierStats(merged_count=0, closed_count=0),
            Tier.SILVER: TierStats(merged_count=0, closed_count=0),
            Tier.GOLD: TierStats(merged_count=0, closed_count=0),
        }

        assert is_tier_unlocked(Tier.BRONZE, stats) is True

    def test_silver_requires_3_merges(self):
        """Test that Silver requires 3 merged PRs."""
        # Just below threshold (2 merges)
        stats = {
            Tier.BRONZE: TierStats(merged_count=0, closed_count=0),
            Tier.SILVER: TierStats(merged_count=2, closed_count=0),
            Tier.GOLD: TierStats(merged_count=0, closed_count=0),
        }
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Exactly at threshold (3 merges)
        stats[Tier.SILVER] = TierStats(merged_count=3, closed_count=0)
        assert is_tier_unlocked(Tier.SILVER, stats) is True

        # Above threshold (4 merges)
        stats[Tier.SILVER] = TierStats(merged_count=4, closed_count=0)
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_silver_requires_50_percent_credibility(self):
        """Test that Silver requires 0.50 credibility."""
        # Just below threshold (49% credibility)
        stats = {
            Tier.BRONZE: TierStats(merged_count=0, closed_count=0),
            Tier.SILVER: TierStats(merged_count=49, closed_count=51),  # 49% credibility
            Tier.GOLD: TierStats(merged_count=0, closed_count=0),
        }
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Exactly at threshold (50% credibility)
        stats[Tier.SILVER] = TierStats(merged_count=3, closed_count=3)  # 50% credibility
        assert is_tier_unlocked(Tier.SILVER, stats) is True

        # Above threshold (60% credibility)
        stats[Tier.SILVER] = TierStats(merged_count=6, closed_count=4)  # 60% credibility
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_silver_requires_both_merges_and_credibility(self):
        """Test that Silver requires BOTH merge count AND credibility."""
        # Has merges but low credibility
        stats = {
            Tier.BRONZE: TierStats(merged_count=0, closed_count=0),
            Tier.SILVER: TierStats(merged_count=3, closed_count=7),  # 30% credibility
            Tier.GOLD: TierStats(merged_count=0, closed_count=0),
        }
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Has credibility but not enough merges
        stats[Tier.SILVER] = TierStats(merged_count=2, closed_count=1)  # 67% credibility but only 2 merges
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Has both
        stats[Tier.SILVER] = TierStats(merged_count=3, closed_count=2)  # 60% credibility and 3 merges
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_gold_requires_5_merges(self):
        """Test that Gold requires 5 merged PRs."""
        # Silver is unlocked, Gold just below threshold (4 merges)
        stats = {
            Tier.BRONZE: TierStats(merged_count=0, closed_count=0),
            Tier.SILVER: TierStats(merged_count=3, closed_count=0),  # Silver unlocked
            Tier.GOLD: TierStats(merged_count=4, closed_count=0),     # 4 merges (need 5)
        }
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Exactly at threshold (5 merges)
        stats[Tier.GOLD] = TierStats(merged_count=5, closed_count=0)
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_gold_requires_70_percent_credibility(self):
        """Test that Gold requires 0.70 credibility."""
        # Silver unlocked, Gold just below 70% (69%)
        stats = {
            Tier.BRONZE: TierStats(merged_count=0, closed_count=0),
            Tier.SILVER: TierStats(merged_count=3, closed_count=0),
            Tier.GOLD: TierStats(merged_count=69, closed_count=31),  # 69% credibility
        }
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Exactly at threshold (70%)
        stats[Tier.GOLD] = TierStats(merged_count=7, closed_count=3)  # 70% credibility
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Above threshold (80%)
        stats[Tier.GOLD] = TierStats(merged_count=8, closed_count=2)  # 80% credibility
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_gold_requires_silver_unlocked(self):
        """Test that Gold cannot be unlocked if Silver requirements aren't met."""
        # Gold meets its own requirements but Silver doesn't
        stats = {
            Tier.BRONZE: TierStats(merged_count=0, closed_count=0),
            Tier.SILVER: TierStats(merged_count=2, closed_count=0),  # Only 2 merges (need 3)
            Tier.GOLD: TierStats(merged_count=5, closed_count=0),     # Has 5 merges
        }
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Now unlock Silver
        stats[Tier.SILVER] = TierStats(merged_count=3, closed_count=0)
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_gold_cascading_requirements(self):
        """Test full cascading unlock: Bronze -> Silver -> Gold."""
        # All requirements met
        stats = {
            Tier.BRONZE: TierStats(merged_count=0, closed_count=0),        # Always unlocked
            Tier.SILVER: TierStats(merged_count=3, closed_count=3),        # 50% credibility, 3 merges
            Tier.GOLD: TierStats(merged_count=7, closed_count=3),          # 70% credibility, 7 merges
        }

        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is True


# ============================================================================
# calculate_credibility_per_tier Tests
# ============================================================================

class TestCalculateCredibilityPerTier:
    """Test calculate_credibility_per_tier function."""

    def test_no_activity_returns_empty_dict(self):
        """Test that tiers with no PRs are not in the result."""
        result = calculate_credibility_per_tier([], [])

        # Should be empty dict since no PRs
        assert result == {}

    def test_single_tier_with_activity(self):
        """Test credibility calculation for a single tier."""
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(3, PRState.MERGED, TIERS[Tier.BRONZE]),
        ]
        closed_prs = [
            create_mock_pr(4, PRState.CLOSED, TIERS[Tier.BRONZE]),
        ]

        result = calculate_credibility_per_tier(merged_prs, closed_prs)

        # 3 merged / (3 merged + 1 closed) = 0.75
        assert result[Tier.BRONZE] == 0.75

    def test_below_activation_threshold_returns_1_0(self):
        """Test that credibility = 1.0 when below activation threshold (2 attempts)."""
        # Only 1 attempt (below threshold of 2)
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE]),
        ]

        result = calculate_credibility_per_tier(merged_prs, [])

        # Should return 1.0 because below 2 activation attempts
        assert result[Tier.BRONZE] == 1.0

    def test_at_activation_threshold_calculates_credibility(self):
        """Test that credibility is calculated when at/above threshold (2 attempts)."""
        # Exactly 2 attempts
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE]),
        ]
        closed_prs = [
            create_mock_pr(2, PRState.CLOSED, TIERS[Tier.BRONZE]),
        ]

        result = calculate_credibility_per_tier(merged_prs, closed_prs)

        # 1 merged / 2 attempts = 0.5
        assert result[Tier.BRONZE] == 0.5

    def test_tier_not_unlocked_returns_0_0(self):
        """Test that credibility = 0.0 for locked tiers."""
        # Silver has PRs but doesn't meet requirements (need 3 merges + 50% credibility)
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.SILVER]),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.SILVER]),  # Only 2 merges
        ]
        closed_prs = [
            create_mock_pr(3, PRState.CLOSED, TIERS[Tier.SILVER]),
        ]

        result = calculate_credibility_per_tier(merged_prs, closed_prs)

        # Silver not unlocked (need 3 merges), should return 0.0
        assert result[Tier.SILVER] == 0.0

    def test_multiple_tiers_with_different_credibilities(self):
        """Test credibility calculation across multiple tiers."""
        merged_prs = [
            # Bronze: 4 merged
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(3, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(4, PRState.MERGED, TIERS[Tier.BRONZE]),
            # Silver: 3 merged (unlocks Silver)
            create_mock_pr(5, PRState.MERGED, TIERS[Tier.SILVER]),
            create_mock_pr(6, PRState.MERGED, TIERS[Tier.SILVER]),
            create_mock_pr(7, PRState.MERGED, TIERS[Tier.SILVER]),
            # Gold: 5 merged (unlocks Gold)
            create_mock_pr(8, PRState.MERGED, TIERS[Tier.GOLD]),
            create_mock_pr(9, PRState.MERGED, TIERS[Tier.GOLD]),
            create_mock_pr(10, PRState.MERGED, TIERS[Tier.GOLD]),
            create_mock_pr(11, PRState.MERGED, TIERS[Tier.GOLD]),
            create_mock_pr(12, PRState.MERGED, TIERS[Tier.GOLD]),
        ]
        closed_prs = [
            # Bronze: 1 closed -> 4/(4+1) = 0.8
            create_mock_pr(13, PRState.CLOSED, TIERS[Tier.BRONZE]),
            # Silver: 3 closed -> 3/(3+3) = 0.5
            create_mock_pr(14, PRState.CLOSED, TIERS[Tier.SILVER]),
            create_mock_pr(15, PRState.CLOSED, TIERS[Tier.SILVER]),
            create_mock_pr(16, PRState.CLOSED, TIERS[Tier.SILVER]),
            # Gold: 2 closed -> 5/(5+2) = 0.714
            create_mock_pr(17, PRState.CLOSED, TIERS[Tier.GOLD]),
            create_mock_pr(18, PRState.CLOSED, TIERS[Tier.GOLD]),
        ]

        result = calculate_credibility_per_tier(merged_prs, closed_prs)

        assert result[Tier.BRONZE] == pytest.approx(0.8, abs=0.01)
        assert result[Tier.SILVER] == pytest.approx(0.5, abs=0.01)
        assert result[Tier.GOLD] == pytest.approx(0.714, abs=0.01)

    def test_tier_progression_scenario(self):
        """Test realistic tier progression scenario."""
        # Scenario: Miner starts in Bronze, unlocks Silver, then Gold

        # Bronze: 5 merged, 2 closed = 71% credibility
        # Silver: 4 merged, 1 closed = 80% credibility (unlocks - has 3+ merges, 50%+ credibility)
        # Gold: 6 merged, 1 closed = 86% credibility (unlocks - has 5+ merges, 70%+ credibility)

        merged_prs = [
            *[create_mock_pr(i, PRState.MERGED, TIERS[Tier.BRONZE]) for i in range(1, 6)],      # 5 Bronze
            *[create_mock_pr(i, PRState.MERGED, TIERS[Tier.SILVER]) for i in range(10, 14)],    # 4 Silver
            *[create_mock_pr(i, PRState.MERGED, TIERS[Tier.GOLD]) for i in range(20, 26)],      # 6 Gold
        ]
        closed_prs = [
            *[create_mock_pr(i, PRState.CLOSED, TIERS[Tier.BRONZE]) for i in range(6, 8)],      # 2 Bronze
            create_mock_pr(14, PRState.CLOSED, TIERS[Tier.SILVER]),                             # 1 Silver
            create_mock_pr(26, PRState.CLOSED, TIERS[Tier.GOLD]),                               # 1 Gold
        ]

        result = calculate_credibility_per_tier(merged_prs, closed_prs)

        # Bronze: 5/(5+2) = 0.714
        assert result[Tier.BRONZE] == pytest.approx(5/7, abs=0.01)

        # Silver: 4/(4+1) = 0.8 (unlocked)
        assert result[Tier.SILVER] == pytest.approx(0.8, abs=0.01)

        # Gold: 6/(6+1) = 0.857 (unlocked)
        assert result[Tier.GOLD] == pytest.approx(6/7, abs=0.01)

    def test_all_merged_prs(self):
        """Test credibility = 1.0 when all PRs are merged."""
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.BRONZE]),
            create_mock_pr(3, PRState.MERGED, TIERS[Tier.BRONZE]),
        ]

        result = calculate_credibility_per_tier(merged_prs, [])

        assert result[Tier.BRONZE] == 1.0

    def test_all_closed_prs(self):
        """Test credibility = 0.0 when all PRs are closed."""
        closed_prs = [
            create_mock_pr(1, PRState.CLOSED, TIERS[Tier.BRONZE]),
            create_mock_pr(2, PRState.CLOSED, TIERS[Tier.BRONZE]),
            create_mock_pr(3, PRState.CLOSED, TIERS[Tier.BRONZE]),
        ]

        result = calculate_credibility_per_tier([], closed_prs)

        # All closed = 0.0 credibility
        assert result[Tier.BRONZE] == 0.0

    def test_edge_case_exactly_at_silver_threshold(self):
        """Test Silver unlock at exact threshold: 3 merges, 50% credibility."""
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.SILVER]),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.SILVER]),
            create_mock_pr(3, PRState.MERGED, TIERS[Tier.SILVER]),
        ]
        closed_prs = [
            create_mock_pr(4, PRState.CLOSED, TIERS[Tier.SILVER]),
            create_mock_pr(5, PRState.CLOSED, TIERS[Tier.SILVER]),
            create_mock_pr(6, PRState.CLOSED, TIERS[Tier.SILVER]),
        ]

        result = calculate_credibility_per_tier(merged_prs, closed_prs)

        # 3 merged / 6 attempts = 0.5 (exactly at threshold)
        assert result[Tier.SILVER] == 0.5

    def test_edge_case_exactly_at_gold_threshold(self):
        """Test Gold unlock at exact threshold: 5 merges, 70% credibility."""
        # Need Silver unlocked first
        merged_prs = [
            # Silver: 3 merged
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.SILVER]),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.SILVER]),
            create_mock_pr(3, PRState.MERGED, TIERS[Tier.SILVER]),
            # Gold: 7 merged (to get exactly 70% with 3 closed)
            *[create_mock_pr(i, PRState.MERGED, TIERS[Tier.GOLD]) for i in range(10, 17)],
        ]
        closed_prs = [
            # Gold: 3 closed -> 7/(7+3) = 0.7
            *[create_mock_pr(i, PRState.CLOSED, TIERS[Tier.GOLD]) for i in range(20, 23)],
        ]

        result = calculate_credibility_per_tier(merged_prs, closed_prs)

        # 7 merged / 10 attempts = 0.7 (exactly at threshold)
        assert result[Tier.GOLD] == 0.7


# ============================================================================
# Integration Tests (Cross-function)
# ============================================================================

class TestTierCredibilityIntegration:
    """Integration tests combining multiple functions."""

    def test_full_pipeline_bronze_only(self):
        """Test full pipeline with only Bronze tier activity."""
        merged_prs = [
            create_mock_pr(1, PRState.MERGED, TIERS[Tier.BRONZE], earned_score=100.0),
            create_mock_pr(2, PRState.MERGED, TIERS[Tier.BRONZE], earned_score=150.0),
        ]
        closed_prs = [
            create_mock_pr(3, PRState.CLOSED, TIERS[Tier.BRONZE]),
        ]

        # Calculate stats
        stats = calculate_tier_stats(merged_prs, closed_prs, include_scoring_details=True)
        assert stats[Tier.BRONZE].merged_count == 2
        assert stats[Tier.BRONZE].closed_count == 1
        assert stats[Tier.BRONZE].earned_score == 250.0

        # Check unlock
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Calculate credibility
        credibility = calculate_credibility_per_tier(merged_prs, closed_prs)
        assert credibility[Tier.BRONZE] == pytest.approx(2/3, abs=0.01)

    def test_full_pipeline_unlock_silver(self):
        """Test full pipeline unlocking Silver tier."""
        merged_prs = [
            *[create_mock_pr(i, PRState.MERGED, TIERS[Tier.SILVER]) for i in range(1, 7)],  # 6 merged
        ]
        closed_prs = [
            *[create_mock_pr(i, PRState.CLOSED, TIERS[Tier.SILVER]) for i in range(10, 14)],  # 4 closed
        ]

        # Calculate stats
        stats = calculate_tier_stats(merged_prs, closed_prs)
        assert stats[Tier.SILVER].merged_count == 6
        assert stats[Tier.SILVER].closed_count == 4
        assert stats[Tier.SILVER].credibility == 0.6  # 6/10

        # Check unlock (should be unlocked: 6 merges > 3, 60% credibility > 50%)
        assert is_tier_unlocked(Tier.SILVER, stats) is True

        # Calculate credibility
        credibility = calculate_credibility_per_tier(merged_prs, closed_prs)
        assert credibility[Tier.SILVER] == 0.6

    def test_full_pipeline_unlock_gold(self):
        """Test full pipeline unlocking Gold tier."""
        merged_prs = [
            # Silver: 3 merged (unlocks Silver)
            *[create_mock_pr(i, PRState.MERGED, TIERS[Tier.SILVER]) for i in range(1, 4)],
            # Gold: 7 merged
            *[create_mock_pr(i, PRState.MERGED, TIERS[Tier.GOLD]) for i in range(10, 17)],
        ]
        closed_prs = [
            # Gold: 3 closed -> 7/(7+3) = 0.7
            *[create_mock_pr(i, PRState.CLOSED, TIERS[Tier.GOLD]) for i in range(20, 23)],
        ]

        # Calculate stats
        stats = calculate_tier_stats(merged_prs, closed_prs)

        # Check unlocks
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Calculate credibility
        credibility = calculate_credibility_per_tier(merged_prs, closed_prs)
        assert credibility[Tier.SILVER] == 1.0  # All merged
        assert credibility[Tier.GOLD] == 0.7


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
