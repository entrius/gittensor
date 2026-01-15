# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for advanced tier requirements including:
- Credibility threshold behavior
- Lower tier credibility cascade
- Lookback expiry scenarios
- Unique repository requirements
- Low-value PR handling
- PRs without tier configuration
- Open PRs and unique repos
- Scoring details

Uses pytest fixtures from conftest.py for clean, reusable test data.

Run tests:
    pytest tests/validator/test_tier_requirements.py -v

Run specific test class:
    pytest tests/validator/test_tier_requirements.py::TestUniqueRepoRequirement -v
"""

import pytest

from gittensor.validator.configurations.tier_config import (
    TIERS,
    Tier,
)
from gittensor.validator.evaluation.credibility import (
    calculate_credibility_per_tier,
    calculate_tier_stats,
    is_tier_unlocked,
)


class TestCredibilityThresholdBehavior:
    """
    Test credibility behavior around activation threshold and tier requirements.

    Key behaviors:
    - Below activation threshold: credibility = 1.0 (benefit of the doubt)
    - At/above activation threshold: actual credibility is calculated
    - Tier unlock requires both merge count AND credibility threshold
    """

    def test_at_activation_threshold_calculates_actual_credibility(self, pr_factory, bronze_config):
        """
        When tier is unlocked and at/above activation threshold, actual credibility is calculated.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_credibility = bronze_tier_config.required_credibility

        # Create PRs that unlock Bronze at exactly the credibility requirement (unique repos)
        # merged / (merged + closed) = required_credibility
        # For required_repos merged, closed = merged * (1 - required_credibility) / required_credibility
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility)

        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)
        closed = pr_factory.closed_batch(bronze_config, count=closed_count, unique_repos=True)

        credibility = calculate_credibility_per_tier(merged, closed)

        expected = required_repos / (required_repos + closed_count)
        assert credibility[Tier.BRONZE] == pytest.approx(expected, abs=0.01)
        assert credibility[Tier.BRONZE] >= required_credibility

    def test_above_activation_threshold_calculates_actual_credibility(self, pr_factory, bronze_config):
        """
        Above activation threshold, actual credibility is calculated.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count

        # Unlock Bronze with perfect credibility (no closed PRs, unique repos)
        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)

        credibility = calculate_credibility_per_tier(merged, [])

        # 100% credibility since no closed PRs
        assert credibility[Tier.BRONZE] == 1.0

    def test_lower_tier_credibility_below_requirement_locks_higher_tiers(self, pr_factory, silver_config, gold_config):
        """
        When lower tier credibility drops below its requirement, higher tiers lock.

        Scenario:
        - Silver requires X% credibility (from config)
        - Miner has enough Silver merges but credibility below requirement
        - Silver locks → Gold cascades to locked
        """
        silver_tier_config = TIERS[Tier.SILVER]
        required_repos = silver_tier_config.required_unique_repos_count
        required_credibility = silver_tier_config.required_credibility

        # Calculate closed count to drop just below required credibility
        # credibility = merged / (merged + closed)
        # We want: merged / (merged + closed) < required_credibility
        # With required_repos merged, we need enough closed to drop below threshold
        merged_count = required_repos
        # To get credibility just below threshold:
        # merged / total < required_credibility
        # merged < required_credibility * total
        # merged < required_credibility * (merged + closed)
        # merged - required_credibility * merged < required_credibility * closed
        # merged * (1 - required_credibility) < required_credibility * closed
        # closed > merged * (1 - required_credibility) / required_credibility
        closed_count = int(merged_count * (1 - required_credibility) / required_credibility) + 1

        silver_merged = pr_factory.merged_batch(silver_config, count=merged_count)
        silver_closed = pr_factory.closed_batch(silver_config, count=closed_count)

        # Add perfect Gold stats
        gold_merged = pr_factory.merged_batch(gold_config, count=10)

        stats = calculate_tier_stats(silver_merged + gold_merged, silver_closed)

        # Verify Silver credibility is below requirement
        assert stats[Tier.SILVER].credibility < required_credibility
        # Verify Silver has enough merges
        assert stats[Tier.SILVER].merged_count >= required_repos

        # Silver should be locked (credibility too low)
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        # Gold cascades to locked
        assert is_tier_unlocked(Tier.GOLD, stats) is False

    def test_tier_unlocked_when_credibility_exactly_at_requirement(self, pr_factory, bronze_config, silver_config):
        """
        Tier unlocks when credibility is exactly at the requirement (with unique repos).
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        required_repos = silver_tier_config.required_unique_repos_count
        required_credibility = silver_tier_config.required_credibility

        # Calculate exact counts for required_credibility
        # closed = merged * (1 - required_credibility) / required_credibility
        merged_count = required_repos
        closed_count = int(merged_count * (1 - required_credibility) / required_credibility)

        # Verify our math: merged / (merged + closed) should equal required_credibility
        expected_credibility = merged_count / (merged_count + closed_count)

        # Need Bronze unlocked first (with unique repos)
        bronze_merged = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )
        silver_merged = pr_factory.merged_batch(silver_config, count=merged_count, unique_repos=True)
        silver_closed = pr_factory.closed_batch(silver_config, count=closed_count, unique_repos=True)

        stats = calculate_tier_stats(bronze_merged + silver_merged, silver_closed)

        assert stats[Tier.SILVER].credibility == pytest.approx(expected_credibility, abs=0.01)
        assert stats[Tier.SILVER].credibility >= required_credibility
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_tier_unlocked_when_credibility_above_requirement(
        self, pr_factory, bronze_config, silver_config, gold_config
    ):
        """
        Tier unlocks when credibility is above the requirement (with unique repos).
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]
        required_repos = gold_tier_config.required_unique_repos_count
        required_credibility = gold_tier_config.required_credibility

        # Get well above threshold
        merged_count = required_repos + 5  # Extra buffer
        # For 90% credibility with merged_count merges:
        # 0.9 = merged / (merged + closed)
        # closed = merged * (1 - 0.9) / 0.9 = merged / 9
        closed_count = merged_count // 9

        # Unlock Bronze and Silver first (with unique repos)
        bronze_merged = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )
        silver_merged = pr_factory.merged_batch(
            silver_config, count=silver_tier_config.required_unique_repos_count, unique_repos=True
        )

        gold_merged = pr_factory.merged_batch(gold_config, count=merged_count, unique_repos=True)
        gold_closed = pr_factory.closed_batch(gold_config, count=closed_count, unique_repos=True)

        stats = calculate_tier_stats(bronze_merged + silver_merged + gold_merged, gold_closed)

        assert stats[Tier.GOLD].credibility > required_credibility
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_high_merges_low_credibility_still_locks(self, pr_factory, silver_config):
        """
        Having many merges doesn't help if credibility is below requirement.
        """
        silver_tier_config = TIERS[Tier.SILVER]
        required_repos = silver_tier_config.required_unique_repos_count
        required_credibility = silver_tier_config.required_credibility

        # Way more merges than required, but terrible credibility
        merged_count = required_repos * 5
        # Calculate closed to get credibility just below requirement
        closed_count = int(merged_count * (1 - required_credibility) / required_credibility) + 2

        merged = pr_factory.merged_batch(silver_config, count=merged_count)
        closed = pr_factory.closed_batch(silver_config, count=closed_count)

        stats = calculate_tier_stats(merged, closed)

        # Plenty of merges
        assert stats[Tier.SILVER].merged_count > required_repos
        # But credibility below threshold
        assert stats[Tier.SILVER].credibility < required_credibility
        # Still locked
        assert is_tier_unlocked(Tier.SILVER, stats) is False


class TestLowerTierCredibilityCascade:
    """
    Test cascade locking when lower tier credibility falls below requirements.
    """

    def test_silver_credibility_drop_locks_gold(self, pr_factory, silver_config, gold_config):
        """
        Gold locks when Silver credibility drops below Silver's requirement.
        """
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        silver_required_repos = silver_tier_config.required_unique_repos_count
        silver_required_credibility = silver_tier_config.required_credibility
        gold_required_repos = gold_tier_config.required_unique_repos_count

        # Silver: enough merges but terrible credibility
        silver_merged_count = silver_required_repos
        silver_closed_count = (
            int(silver_merged_count * (1 - silver_required_credibility) / silver_required_credibility) + 2
        )

        silver_merged = pr_factory.merged_batch(silver_config, count=silver_merged_count)
        silver_closed = pr_factory.closed_batch(silver_config, count=silver_closed_count)

        # Gold: perfect stats
        gold_merged = pr_factory.merged_batch(gold_config, count=gold_required_repos + 5)

        stats = calculate_tier_stats(silver_merged + gold_merged, silver_closed)
        credibility = calculate_credibility_per_tier(silver_merged + gold_merged, silver_closed)

        # Silver credibility below requirement
        assert stats[Tier.SILVER].credibility < silver_required_credibility

        # Gold has perfect stats
        assert stats[Tier.GOLD].merged_count >= gold_required_repos
        assert stats[Tier.GOLD].credibility == 1.0

        # But Gold is locked because Silver is locked
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert credibility.get(Tier.GOLD, 0.0) == 0.0

    def test_recovering_lower_tier_credibility_unlocks_higher(
        self, pr_factory, bronze_config, silver_config, gold_config
    ):
        """
        Improving lower tier credibility can restore higher tier access (with unique repos).
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        silver_required_repos = silver_tier_config.required_unique_repos_count
        silver_required_credibility = silver_tier_config.required_credibility
        gold_required_repos = gold_tier_config.required_unique_repos_count

        # Need Bronze unlocked first (with unique repos)
        bronze_merged = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )

        # Initial state: Silver below credibility threshold (unique repos)
        silver_merged_count = silver_required_repos
        silver_closed_count = (
            int(silver_merged_count * (1 - silver_required_credibility) / silver_required_credibility) + 2
        )

        silver_merged = pr_factory.merged_batch(silver_config, count=silver_merged_count, unique_repos=True)
        silver_closed = pr_factory.closed_batch(silver_config, count=silver_closed_count, unique_repos=True)
        gold_merged = pr_factory.merged_batch(gold_config, count=gold_required_repos + 5, unique_repos=True)

        stats = calculate_tier_stats(bronze_merged + silver_merged + gold_merged, silver_closed)
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Recovery: add more Silver merges to boost credibility above threshold (unique repos)
        # New credibility = (old_merged + new) / (old_merged + old_closed + new)
        # We need enough new merges to get above silver_required_credibility
        # Let's add enough to double our merged count
        additional_silver = pr_factory.merged_batch(silver_config, count=silver_merged_count * 2, unique_repos=True)

        stats_after = calculate_tier_stats(
            bronze_merged + silver_merged + additional_silver + gold_merged, silver_closed
        )

        # Should now be above threshold
        assert stats_after[Tier.SILVER].credibility >= silver_required_credibility
        assert is_tier_unlocked(Tier.SILVER, stats_after) is True
        assert is_tier_unlocked(Tier.GOLD, stats_after) is True


# ============================================================================
# Lookback Expiry Tests
# ============================================================================


class TestLookbackExpiry:
    """
    Test scenarios where PRs expire outside the lookback window.

    Miners must continuously maintain lower tiers to keep higher tiers unlocked.
    When lower-tier PRs expire (fall outside 90-day window), the miner loses
    those counts, potentially causing cascade lock failures.
    """

    def _bronze_prs(self, pr_factory, bronze_config):
        """Helper to create Bronze PRs that unlock Bronze (with unique repos)."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        return pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )

    def test_silver_prs_expire_locks_gold(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Gold miner loses Gold access when Silver PRs expire (with unique repos).

        Scenario:
        - Miner had Bronze + Silver + Gold all unlocked
        - Time passes, Silver PRs fall outside lookback
        - Now has 0 Silver merges → Silver locks → Gold cascades to locked
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count
        gold_required = gold_tier_config.required_unique_repos_count

        # Before expiry: Gold unlocked (Bronze + Silver + Gold PRs, unique repos)
        bronze_prs = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        silver_prs = pr_factory.merged_batch(silver_config, count=silver_required, unique_repos=True)
        gold_prs = pr_factory.merged_batch(gold_config, count=gold_required + 2, unique_repos=True)

        stats_before = calculate_tier_stats(bronze_prs + silver_prs + gold_prs, [])
        assert is_tier_unlocked(Tier.SILVER, stats_before) is True
        assert is_tier_unlocked(Tier.GOLD, stats_before) is True

        # After expiry: Silver PRs gone (simulating lookback filter), Bronze stays
        pr_factory.reset()
        bronze_prs_after = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        gold_prs_after = pr_factory.merged_batch(gold_config, count=gold_required + 2, unique_repos=True)

        stats_after = calculate_tier_stats(bronze_prs_after + gold_prs_after, [])
        credibility_after = calculate_credibility_per_tier(bronze_prs_after + gold_prs_after, [])

        # Bronze still unlocked
        assert is_tier_unlocked(Tier.BRONZE, stats_after) is True
        # Silver now locked (no merges)
        assert is_tier_unlocked(Tier.SILVER, stats_after) is False
        # Gold cascades to locked despite perfect Gold stats
        assert is_tier_unlocked(Tier.GOLD, stats_after) is False
        assert credibility_after.get(Tier.GOLD, 0.0) == 0.0

    def test_partial_silver_expiry_still_unlocked(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Partial Silver expiry doesn't lock if enough PRs remain (with unique repos).

        Scenario:
        - Miner had extra Silver merges + Gold unlocked
        - Some Silver PRs expire → still meets threshold
        - Gold stays unlocked
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count
        gold_required = gold_tier_config.required_unique_repos_count
        extra_silver = 2  # Buffer above requirement

        # Before: all tiers unlocked with extra Silver merges (unique repos)
        bronze_prs = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        merged_before = (
            bronze_prs
            + pr_factory.merged_batch(silver_config, count=silver_required + extra_silver, unique_repos=True)
            + pr_factory.merged_batch(gold_config, count=gold_required + 2, unique_repos=True)
        )

        stats_before = calculate_tier_stats(merged_before, [])
        assert stats_before[Tier.SILVER].merged_count == silver_required + extra_silver
        assert is_tier_unlocked(Tier.GOLD, stats_before) is True

        # After: extra Silver merges expire, exactly at threshold remains (unique repos)
        pr_factory.reset()
        merged_after = (
            pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
            + pr_factory.merged_batch(silver_config, count=silver_required, unique_repos=True)
            + pr_factory.merged_batch(gold_config, count=gold_required + 2, unique_repos=True)
        )

        stats_after = calculate_tier_stats(merged_after, [])
        assert stats_after[Tier.SILVER].merged_count == silver_required
        # Still unlocked - exactly at threshold
        assert is_tier_unlocked(Tier.SILVER, stats_after) is True
        assert is_tier_unlocked(Tier.GOLD, stats_after) is True

    def test_one_silver_expiry_below_threshold_locks(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        When exactly at threshold, losing one PR locks the tier (with unique repos).

        Scenario:
        - Miner has exactly minimum Silver merges
        - 1 Silver PR expires → below threshold
        - Silver locks → Gold cascades
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count
        gold_required = gold_tier_config.required_unique_repos_count

        # At threshold: exactly silver_required (with Bronze unlocked, unique repos)
        merged = (
            pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
            + pr_factory.merged_batch(silver_config, count=silver_required, unique_repos=True)
            + pr_factory.merged_batch(gold_config, count=gold_required + 5, unique_repos=True)
        )

        stats = calculate_tier_stats(merged, [])
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # One Silver expires: now silver_required - 1 (Bronze still unlocked, unique repos)
        pr_factory.reset()
        merged_after = (
            pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
            + pr_factory.merged_batch(silver_config, count=silver_required - 1, unique_repos=True)
            + pr_factory.merged_batch(gold_config, count=gold_required + 5, unique_repos=True)
        )

        stats_after = calculate_tier_stats(merged_after, [])
        assert is_tier_unlocked(Tier.BRONZE, stats_after) is True
        assert is_tier_unlocked(Tier.SILVER, stats_after) is False
        assert is_tier_unlocked(Tier.GOLD, stats_after) is False

    def test_credibility_drops_as_merges_expire(self, pr_factory, bronze_config, silver_config):
        """
        Credibility changes as PRs expire from the lookback window (with unique repos).

        Scenario:
        - Miner has good credibility at Silver
        - Some merged PRs expire → credibility drops
        - Still above requirement threshold
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count
        silver_cred_required = silver_tier_config.required_credibility

        # Before: high credibility (well above threshold, unique repos)
        merged_count = silver_required + 5
        closed_count = 1  # Keep low to stay above 75% threshold
        bronze_prs = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        silver_merged_before = pr_factory.merged_batch(silver_config, count=merged_count, unique_repos=True)
        closed = pr_factory.closed_batch(silver_config, count=closed_count, unique_repos=True)

        stats_before = calculate_tier_stats(bronze_prs + silver_merged_before, closed)
        assert stats_before[Tier.SILVER].credibility == merged_count / (merged_count + closed_count)
        assert is_tier_unlocked(Tier.SILVER, stats_before) is True

        # After: some merged PRs expire but still above threshold (unique repos)
        pr_factory.reset()
        remaining_merged = silver_required  # Keep at minimum
        bronze_prs_after = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        silver_merged_after = pr_factory.merged_batch(silver_config, count=remaining_merged, unique_repos=True)

        stats_after = calculate_tier_stats(bronze_prs_after + silver_merged_after, closed)
        new_credibility = remaining_merged / (remaining_merged + closed_count)
        assert stats_after[Tier.SILVER].credibility == pytest.approx(new_credibility, abs=0.01)
        # Should still be above required credibility
        if new_credibility >= silver_cred_required:
            assert is_tier_unlocked(Tier.SILVER, stats_after) is True

    def test_credibility_drops_below_threshold_on_expiry(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Expiring merged PRs can drop credibility below threshold (with unique repos).

        Scenario:
        - Gold miner: exactly at credibility threshold
        - 1 merged PR expires → credibility drops below threshold
        - Gold locks
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count
        gold_required = gold_tier_config.required_unique_repos_count
        gold_cred_required = gold_tier_config.required_credibility

        # Calculate counts so losing 1 merged PR drops credibility below threshold
        # Use +3 instead of +2 to ensure enough margin for the math to work
        gold_merged_count = gold_required + 3
        # Calculate closed count based on (merged-1) to ensure "after" is below threshold
        gold_closed_count = int((gold_merged_count - 1) * (1 - gold_cred_required) / gold_cred_required) + 1

        # Before: at or above threshold (all tiers unlocked, unique repos)
        merged_before = (
            pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
            + pr_factory.merged_batch(silver_config, count=silver_required, unique_repos=True)
            + pr_factory.merged_batch(gold_config, count=gold_merged_count, unique_repos=True)
        )
        closed = pr_factory.closed_batch(gold_config, count=gold_closed_count, unique_repos=True)

        stats_before = calculate_tier_stats(merged_before, closed)
        assert stats_before[Tier.GOLD].credibility >= gold_cred_required
        assert is_tier_unlocked(Tier.GOLD, stats_before) is True

        # After: 1 merged Gold PR expires (unique repos)
        pr_factory.reset()
        merged_after = (
            pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
            + pr_factory.merged_batch(silver_config, count=silver_required, unique_repos=True)
            + pr_factory.merged_batch(gold_config, count=gold_merged_count - 1, unique_repos=True)
        )

        stats_after = calculate_tier_stats(merged_after, closed)
        # Credibility should drop below threshold
        assert stats_after[Tier.GOLD].credibility < gold_cred_required
        assert is_tier_unlocked(Tier.GOLD, stats_after) is False

    def test_closed_prs_expiring_improves_credibility(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Expiring closed PRs can improve credibility (with unique repos).

        Scenario:
        - Gold below credibility threshold (locked)
        - Old closed PRs expire → credibility rises above threshold
        - Gold unlocks
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count
        gold_required = gold_tier_config.required_unique_repos_count
        gold_cred_required = gold_tier_config.required_credibility

        # Before: below threshold (unique repos)
        gold_merged_count = gold_required + 8
        gold_closed_count = int(gold_merged_count * (1 - gold_cred_required) / gold_cred_required) + 3

        merged = (
            pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
            + pr_factory.merged_batch(silver_config, count=silver_required, unique_repos=True)
            + pr_factory.merged_batch(gold_config, count=gold_merged_count, unique_repos=True)
        )
        closed_before = pr_factory.closed_batch(gold_config, count=gold_closed_count, unique_repos=True)

        stats_before = calculate_tier_stats(merged, closed_before)
        assert stats_before[Tier.GOLD].credibility < gold_cred_required
        assert is_tier_unlocked(Tier.GOLD, stats_before) is False

        # After: some closed PRs expire, improving credibility (unique repos)
        pr_factory.reset()
        remaining_closed = int(gold_merged_count * (1 - gold_cred_required) / gold_cred_required) - 1
        remaining_closed = max(0, remaining_closed)
        closed_after = pr_factory.closed_batch(gold_config, count=remaining_closed, unique_repos=True)

        stats_after = calculate_tier_stats(merged, closed_after)
        assert stats_after[Tier.GOLD].credibility >= gold_cred_required
        assert is_tier_unlocked(Tier.GOLD, stats_after) is True

    def test_all_tier_activity_expires(self, pr_factory, bronze_config, silver_config):
        """
        When all PRs at a tier expire, it's like starting fresh (with unique repos).

        Scenario:
        - Miner had Bronze + Silver unlocked
        - All Silver PRs expire (Bronze still active)
        - Silver now has no activity (locked due to 0 merges)
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count

        # Before: Silver unlocked (unique repos)
        bronze_prs = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        silver_prs = pr_factory.merged_batch(silver_config, count=silver_required + 2, unique_repos=True)
        stats_before = calculate_tier_stats(bronze_prs + silver_prs, [])
        assert is_tier_unlocked(Tier.SILVER, stats_before) is True

        # After: Silver PRs expired, only Bronze remains (unique repos)
        pr_factory.reset()
        bronze_prs_after = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        stats_after = calculate_tier_stats(bronze_prs_after, [])
        assert stats_after[Tier.SILVER].merged_count == 0
        assert is_tier_unlocked(Tier.BRONZE, stats_after) is True
        assert is_tier_unlocked(Tier.SILVER, stats_after) is False

    def test_continuous_maintenance_required(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Miners must continuously contribute to lower tiers (with unique repos).

        Scenario demonstrates the "tending garden" requirement:
        - Miner gets Gold, then focuses only on Gold PRs
        - Old Silver PRs expire one by one
        - Eventually Silver locks → Gold cascades
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count
        gold_required = gold_tier_config.required_unique_repos_count

        # Phase 1: Full unlock with buffer (unique repos)
        bronze_prs = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        silver_prs = pr_factory.merged_batch(silver_config, count=silver_required + 2, unique_repos=True)
        gold_prs = pr_factory.merged_batch(gold_config, count=gold_required + 5, unique_repos=True)

        stats = calculate_tier_stats(bronze_prs + silver_prs + gold_prs, [])
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Phase 2: Some Silver expires (still above threshold)
        stats = calculate_tier_stats(bronze_prs + silver_prs[: silver_required + 1] + gold_prs, [])
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Phase 3: More Silver expires (exactly at threshold)
        stats = calculate_tier_stats(bronze_prs + silver_prs[:silver_required] + gold_prs, [])
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Phase 4: One more expires (below threshold)
        stats = calculate_tier_stats(bronze_prs + silver_prs[: silver_required - 1] + gold_prs, [])
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False  # Cascade!

    def test_refreshing_lower_tier_restores_access(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Adding new lower-tier PRs restores higher tier access (with unique repos).

        Scenario:
        - Miner lost Gold due to Silver expiry (Bronze still active)
        - Gets new Silver PRs merged
        - Gold access restored
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        bronze_required = bronze_tier_config.required_unique_repos_count
        silver_required = silver_tier_config.required_unique_repos_count
        gold_required = gold_tier_config.required_unique_repos_count

        # Lost access: Bronze unlocked, but one below Silver threshold (unique repos)
        bronze_prs = pr_factory.merged_batch(bronze_config, count=bronze_required, unique_repos=True)
        old_silver = pr_factory.merged_batch(silver_config, count=silver_required - 1, unique_repos=True)
        gold_prs = pr_factory.merged_batch(gold_config, count=gold_required + 5, unique_repos=True)

        stats = calculate_tier_stats(bronze_prs + old_silver + gold_prs, [])
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Refresh: add 1 new Silver PR to meet threshold (unique repo)
        new_silver = pr_factory.merged_batch(silver_config, count=1, unique_repos=True)

        stats = calculate_tier_stats(bronze_prs + old_silver + new_silver + gold_prs, [])
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is True


# ============================================================================
# Unique Repository Requirement Tests
# ============================================================================


class TestUniqueRepoRequirement:
    """
    Test the unique repo contribution requirement for tier unlocking.

    This new requirement prevents same-repo spam by requiring miners to contribute
    to a minimum number of unique repositories within each tier to unlock it.
    """

    def test_same_repo_spam_blocks_tier_unlock(self, pr_factory, bronze_config):
        """
        Multiple PRs to the same repo don't count as unique repo contributions.

        Scenario:
        - Miner creates multiple merged PRs to same repo
        - Meets merge count and credibility requirements
        - But only has 1 unique repo → tier locked
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_unique_repos = bronze_tier_config.required_unique_repos_count

        # Create PRs all to the same repo (default behavior without unique_repos=True)
        merged = pr_factory.merged_batch(bronze_config, count=required_repos)

        stats = calculate_tier_stats(merged, [])

        # Has enough merges
        assert stats[Tier.BRONZE].merged_count >= required_repos
        # But only 1 unique repo
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 1
        # Required unique repos is 3
        assert required_unique_repos == 3
        # Tier is locked
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_unique_repos_unlock_tier(self, pr_factory, bronze_config):
        """
        PRs to different repos count as unique repo contributions.

        Scenario:
        - Miner creates PRs to unique repos
        - Meets merge count, credibility, and unique repo requirements
        - Tier unlocks
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_unique_repos = bronze_tier_config.required_unique_repos_count

        # Create PRs to unique repos
        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)

        stats = calculate_tier_stats(merged, [])

        # Has enough merges
        assert stats[Tier.BRONZE].merged_count >= required_repos
        # Has enough unique repos
        assert stats[Tier.BRONZE].unique_repo_contribution_count >= required_unique_repos
        # Tier is unlocked
        assert is_tier_unlocked(Tier.BRONZE, stats) is True

    def test_unique_repo_count_per_tier(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Unique repo counts are tracked per tier.

        Scenario:
        - Miner has PRs in multiple tiers
        - Each tier tracks its own unique repo count
        """
        # Create PRs with unique repos for each tier
        bronze_prs = pr_factory.merged_batch(bronze_config, count=3, unique_repos=True)
        silver_prs = pr_factory.merged_batch(silver_config, count=3, unique_repos=True)

        stats = calculate_tier_stats(bronze_prs + silver_prs, [])

        # Each tier has its own unique repo count
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 3
        assert stats[Tier.SILVER].unique_repo_contribution_count == 3

    def test_same_repo_multiple_prs_counts_once(self, pr_factory, bronze_config):
        """
        Multiple PRs to the same repo count as only 1 unique repo contribution.

        Scenario:
        - Miner creates 5 PRs to repo-1
        - And 1 PR to repo-2
        - Unique repo count is 2, not 6
        """
        # Create 5 PRs to the same repo
        prs_same_repo = [pr_factory.merged(bronze_config, repo='owner/repo-1') for _ in range(5)]
        # Create 1 PR to a different repo
        pr_different_repo = pr_factory.merged(bronze_config, repo='owner/repo-2')

        merged = prs_same_repo + [pr_different_repo]
        stats = calculate_tier_stats(merged, [])

        assert stats[Tier.BRONZE].merged_count == 6
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 2

    def test_unique_repo_requirement_per_tier_config(self):
        """
        Verify each tier has the expected unique repo requirement (all are 3).
        """
        assert TIERS[Tier.BRONZE].required_unique_repos_count == 3
        assert TIERS[Tier.SILVER].required_unique_repos_count == 3
        assert TIERS[Tier.GOLD].required_unique_repos_count == 3

    def test_exactly_at_unique_repo_threshold(self, pr_factory, bronze_config):
        """
        Tier unlocks when exactly at unique repo requirement.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_unique_repos = bronze_tier_config.required_unique_repos_count

        # Create exactly required number of unique repos
        merged = pr_factory.merged_batch(bronze_config, count=required_unique_repos, unique_repos=True)

        stats = calculate_tier_stats(merged, [])

        assert stats[Tier.BRONZE].unique_repo_contribution_count == required_unique_repos
        assert is_tier_unlocked(Tier.BRONZE, stats) is True

    def test_one_below_unique_repo_threshold(self, pr_factory, bronze_config):
        """
        Tier stays locked when one below unique repo requirement.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_unique_repos = bronze_tier_config.required_unique_repos_count

        # Create one less than required unique repos
        merged = pr_factory.merged_batch(bronze_config, count=required_unique_repos - 1, unique_repos=True)

        stats = calculate_tier_stats(merged, [])

        # Has unique repos but not enough
        assert stats[Tier.BRONZE].unique_repo_contribution_count == required_unique_repos - 1
        # Tier is locked (even if we had enough merges)
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_closed_prs_dont_count_for_unique_repos(self, pr_factory, bronze_config):
        """
        Closed PRs don't count towards unique repo requirements.

        Scenario:
        - Miner has 2 merged PRs to unique repos
        - And 5 closed PRs to unique repos
        - Only 2 unique repo contributions counted
        """
        # 2 merged PRs to unique repos
        merged = pr_factory.merged_batch(bronze_config, count=2, unique_repos=True)
        # 5 closed PRs to unique repos
        closed = pr_factory.closed_batch(bronze_config, count=5, unique_repos=True)

        stats = calculate_tier_stats(merged, closed)

        # Only merged PRs count towards unique repos
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 2
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_unique_repo_with_mixed_same_repo_prs(self, pr_factory, bronze_config):
        """
        Mix of unique and same-repo PRs correctly counts unique repos.

        Scenario:
        - 3 PRs to 3 unique repos (meets requirement)
        - Plus 5 more PRs to those same repos
        - Total 8 merged PRs, 3 unique repos
        """
        # Create PRs to 3 unique repos with multiple PRs each
        repo1_prs = [pr_factory.merged(bronze_config, repo='owner/repo-1') for _ in range(3)]
        repo2_prs = [pr_factory.merged(bronze_config, repo='owner/repo-2') for _ in range(3)]
        repo3_prs = [pr_factory.merged(bronze_config, repo='owner/repo-3') for _ in range(2)]

        merged = repo1_prs + repo2_prs + repo3_prs
        stats = calculate_tier_stats(merged, [])

        assert stats[Tier.BRONZE].merged_count == 8
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 3
        assert is_tier_unlocked(Tier.BRONZE, stats) is True

    def test_tier_stats_tracks_unique_repos_correctly(self, pr_factory, bronze_config):
        """
        TierStats unique_repo_contribution_count is calculated correctly.
        """
        # 5 PRs to 2 unique repos
        prs_repo_a = [pr_factory.merged(bronze_config, repo='owner/repo-a') for _ in range(3)]
        prs_repo_b = [pr_factory.merged(bronze_config, repo='owner/repo-b') for _ in range(2)]

        merged = prs_repo_a + prs_repo_b
        stats = calculate_tier_stats(merged, [])

        assert stats[Tier.BRONZE].unique_repo_contribution_count == 2

    def test_silver_unique_repo_with_bronze_unlocked(self, pr_factory, bronze_config, silver_config):
        """
        Silver tier also requires unique repos (with Bronze unlocked first).

        Scenario:
        - Bronze unlocked with unique repos
        - Silver has enough merges but same repo spam
        - Silver stays locked
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]

        # Bronze unlocked with unique repos
        bronze_prs = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )

        # Silver has enough merges but all to same repo
        silver_prs = pr_factory.merged_batch(
            silver_config, count=silver_tier_config.required_unique_repos_count
        )  # No unique_repos=True

        stats = calculate_tier_stats(bronze_prs + silver_prs, [])

        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert stats[Tier.SILVER].merged_count >= silver_tier_config.required_unique_repos_count
        assert stats[Tier.SILVER].unique_repo_contribution_count == 1
        assert is_tier_unlocked(Tier.SILVER, stats) is False

    def test_gold_unique_repo_requirement(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Gold tier requires unique repos across Bronze, Silver, and Gold.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]
        # Calculate token scores needed per PR to meet total requirements
        silver_token_per_pr = (
            silver_tier_config.required_min_token_score or 50.0
        ) / silver_tier_config.required_unique_repos_count + 1.0
        gold_token_per_pr = (
            gold_tier_config.required_min_token_score or 150.0
        ) / gold_tier_config.required_unique_repos_count + 1.0

        # All tiers with unique repos (with sufficient token scores)
        bronze_prs = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )
        silver_prs = pr_factory.merged_batch(
            silver_config,
            count=silver_tier_config.required_unique_repos_count,
            unique_repos=True,
            token_score=silver_token_per_pr,
        )
        gold_prs = pr_factory.merged_batch(
            gold_config,
            count=gold_tier_config.required_unique_repos_count,
            unique_repos=True,
            token_score=gold_token_per_pr,
        )

        stats = calculate_tier_stats(bronze_prs + silver_prs + gold_prs, [])

        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_unique_repos_not_shared_across_tiers(self, pr_factory, bronze_config, silver_config):
        """
        Unique repos in one tier don't count towards another tier's requirement.

        Each tier tracks its own unique repo contributions independently.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]

        # Bronze with unique repos
        bronze_prs = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )

        # Silver with same repo spam (using default repo which is 'test/repo')
        # Reset to ensure we're using the default repo
        silver_prs = [
            pr_factory.merged(silver_config, repo='test/shared-repo')
            for _ in range(silver_tier_config.required_unique_repos_count)
        ]

        stats = calculate_tier_stats(bronze_prs + silver_prs, [])

        # Bronze has its unique repos
        assert stats[Tier.BRONZE].unique_repo_contribution_count == bronze_tier_config.required_unique_repos_count
        # Silver only has 1 unique repo (all to same repo)
        assert stats[Tier.SILVER].unique_repo_contribution_count == 1
        # Bronze unlocked, Silver locked
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is False


class TestUniqueRepoEdgeCases:
    """
    Edge cases for unique repo requirement.
    """

    def test_empty_repo_name_handling(self, pr_factory, bronze_config):
        """
        PRs should always have a repo name in real scenarios.
        """
        # All PRs have repository_full_name set by the factory
        merged = pr_factory.merged_batch(bronze_config, count=3, unique_repos=True)

        for pr in merged:
            assert pr.repository_full_name is not None
            assert len(pr.repository_full_name) > 0

    def test_zero_unique_repos_locks_tier(self, pr_factory, bronze_config):
        """
        Zero unique repos (no PRs) means tier is locked.
        """
        stats = calculate_tier_stats([], [])

        assert stats[Tier.BRONZE].unique_repo_contribution_count == 0
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_many_unique_repos_above_requirement(self, pr_factory, bronze_config):
        """
        Having more unique repos than required still unlocks the tier.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_unique_repos = bronze_tier_config.required_unique_repos_count

        # Create many more unique repos than required
        merged = pr_factory.merged_batch(bronze_config, count=10, unique_repos=True)

        stats = calculate_tier_stats(merged, [])

        assert stats[Tier.BRONZE].unique_repo_contribution_count == 10
        assert stats[Tier.BRONZE].unique_repo_contribution_count > required_unique_repos
        assert is_tier_unlocked(Tier.BRONZE, stats) is True


# ============================================================================
# Low-Value PR Tests
# ============================================================================


class TestLowValuePRHandling:
    """
    Test low_value_pr flag behavior in tier calculations.

    Low-value PRs (substantive_ratio < 0.1) are filtered differently:
    - Merged low-value PRs: NOT counted for merges OR unique repos
    - Closed low-value PRs: STILL count against credibility
    """

    def test_low_value_merged_pr_not_counted_for_merges(self, pr_factory, bronze_config):
        """
        Merged PRs with low_value_pr=True should not count toward merge count.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count

        # Create normal merged PRs (unique repos)
        normal_prs = pr_factory.merged_batch(bronze_config, count=required_repos - 1, unique_repos=True)

        # Create low-value merged PRs
        low_value_prs = [pr_factory.merged(bronze_config, unique_repo=True, low_value_pr=True) for _ in range(5)]

        stats = calculate_tier_stats(normal_prs + low_value_prs, [])

        # Only normal PRs counted
        assert stats[Tier.BRONZE].merged_count == required_repos - 1
        # Tier locked because we're 1 short of required merges
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_low_value_merged_pr_not_counted_for_unique_repos(self, pr_factory, bronze_config):
        """
        Merged PRs with low_value_pr=True should not count toward unique repo count.
        """
        # Create normal PRs to unique repos (2 repos)
        normal_prs = [
            pr_factory.merged(bronze_config, repo='owner/repo-1'),
            pr_factory.merged(bronze_config, repo='owner/repo-2'),
        ]

        # Create low-value PRs to different unique repos (3 more repos)
        low_value_prs = [
            pr_factory.merged(bronze_config, repo='owner/repo-3', low_value_pr=True),
            pr_factory.merged(bronze_config, repo='owner/repo-4', low_value_pr=True),
            pr_factory.merged(bronze_config, repo='owner/repo-5', low_value_pr=True),
        ]

        stats = calculate_tier_stats(normal_prs + low_value_prs, [])

        # Only 2 unique repos from normal PRs (low-value repos not counted)
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 2
        # Tier locked because unique repo count < 3
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_low_value_closed_pr_still_hurts_credibility(self, pr_factory, bronze_config):
        """
        Closed PRs with low_value_pr=True STILL count against credibility.

        This is the asymmetry: low-value filter only applies to merged PRs.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_credibility = bronze_tier_config.required_credibility

        # Create enough normal merged PRs (unique repos)
        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)

        # Create low-value closed PRs to tank credibility
        # Calculate how many closed PRs to drop below credibility threshold
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 2
        closed = [pr_factory.closed(bronze_config, unique_repo=True, low_value_pr=True) for _ in range(closed_count)]

        stats = calculate_tier_stats(merged, closed)

        # Closed PRs still counted
        assert stats[Tier.BRONZE].closed_count == closed_count
        # Credibility dropped below threshold
        assert stats[Tier.BRONZE].credibility < required_credibility
        # Tier locked due to low credibility
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_mixed_low_value_and_normal_prs(self, pr_factory, bronze_config):
        """
        Mix of low-value and normal PRs are handled correctly.
        """
        # 3 normal merged PRs to unique repos (meets requirements)
        normal_merged = [pr_factory.merged(bronze_config, repo=f'owner/repo-{i}') for i in range(3)]

        # 5 low-value merged PRs (should be ignored)
        low_value_merged = [
            pr_factory.merged(bronze_config, repo=f'owner/lowvalue-{i}', low_value_pr=True) for i in range(5)
        ]

        # 1 normal closed PR
        normal_closed = [pr_factory.closed(bronze_config, repo='owner/closed-1')]

        # 2 low-value closed PRs (still count!)
        low_value_closed = [
            pr_factory.closed(bronze_config, repo=f'owner/lv-closed-{i}', low_value_pr=True) for i in range(2)
        ]

        stats = calculate_tier_stats(normal_merged + low_value_merged, normal_closed + low_value_closed)

        # Only normal merged PRs counted
        assert stats[Tier.BRONZE].merged_count == 3
        # All closed PRs counted (including low-value)
        assert stats[Tier.BRONZE].closed_count == 3
        # Only normal merged repos counted for unique
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 3
        # Credibility: 3 / (3 + 3) = 50%
        assert stats[Tier.BRONZE].credibility == pytest.approx(0.5, abs=0.01)

    def test_all_low_value_merged_prs_means_zero_merges(self, pr_factory, bronze_config):
        """
        If all merged PRs are low-value, merge count is zero.
        """
        # Create only low-value merged PRs
        low_value_prs = [pr_factory.merged(bronze_config, unique_repo=True, low_value_pr=True) for _ in range(10)]

        stats = calculate_tier_stats(low_value_prs, [])

        assert stats[Tier.BRONZE].merged_count == 0
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 0
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_low_value_flag_per_tier(self, pr_factory, bronze_config, silver_config):
        """
        Low-value filtering is applied per-PR regardless of tier.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]

        # Normal Bronze PRs (meets Bronze requirements)
        bronze_prs = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )

        # Mix of normal and low-value Silver PRs
        silver_normal = [pr_factory.merged(silver_config, repo=f'owner/silver-{i}') for i in range(2)]
        silver_low_value = [
            pr_factory.merged(silver_config, repo=f'owner/silver-lv-{i}', low_value_pr=True) for i in range(5)
        ]

        stats = calculate_tier_stats(bronze_prs + silver_normal + silver_low_value, [])

        # Bronze meets requirements
        assert stats[Tier.BRONZE].merged_count >= bronze_tier_config.required_unique_repos_count
        assert is_tier_unlocked(Tier.BRONZE, stats) is True

        # Silver only has 2 normal merges (low-value not counted)
        assert stats[Tier.SILVER].merged_count == 2
        assert stats[Tier.SILVER].unique_repo_contribution_count == 2
        assert is_tier_unlocked(Tier.SILVER, stats) is False


# ============================================================================
# PRs Without Tier Configuration Tests
# ============================================================================


class TestPRsWithoutTierConfig:
    """
    Test behavior of PRs that have no tier configuration.

    These represent PRs from repositories not enrolled in gittensor.
    They should be completely ignored in all tier calculations.
    """

    def test_merged_pr_without_tier_not_counted(self, pr_factory, bronze_config):
        """
        Merged PRs without tier config are completely ignored.
        """
        from gittensor.classes import PRState

        bronze_tier_config = TIERS[Tier.BRONZE]

        # Normal PRs that meet requirements
        normal_prs = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )

        # PRs without tier config (should be ignored)
        untracked_prs = [
            pr_factory.create_without_tier(state=PRState.MERGED, repo=f'untracked/repo-{i}') for i in range(10)
        ]

        stats = calculate_tier_stats(normal_prs + untracked_prs, [])

        # Only normal PRs counted
        assert stats[Tier.BRONZE].merged_count == bronze_tier_config.required_unique_repos_count
        # Untracked repos don't add to unique count
        assert stats[Tier.BRONZE].unique_repo_contribution_count == bronze_tier_config.required_unique_repos_count

    def test_closed_pr_without_tier_not_counted(self, pr_factory, bronze_config):
        """
        Closed PRs without tier config don't affect credibility.
        """
        from gittensor.classes import PRState

        bronze_tier_config = TIERS[Tier.BRONZE]

        # Normal merged PRs
        merged = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )

        # Lots of closed PRs without tier config (should be ignored)
        untracked_closed = [
            pr_factory.create_without_tier(state=PRState.CLOSED, repo=f'untracked/repo-{i}') for i in range(50)
        ]

        stats = calculate_tier_stats(merged, untracked_closed)

        # No closed PRs counted
        assert stats[Tier.BRONZE].closed_count == 0
        # 100% credibility
        assert stats[Tier.BRONZE].credibility == 1.0
        # Tier unlocked
        assert is_tier_unlocked(Tier.BRONZE, stats) is True

    def test_open_pr_without_tier_not_counted(self, pr_factory, bronze_config):
        """
        Open PRs without tier config are ignored.
        """
        from gittensor.classes import PRState

        bronze_tier_config = TIERS[Tier.BRONZE]

        merged = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        )

        # Open PRs without tier config
        untracked_open = [
            pr_factory.create_without_tier(state=PRState.OPEN, repo=f'untracked/repo-{i}') for i in range(10)
        ]

        stats = calculate_tier_stats(merged, [], untracked_open)

        # No open PRs counted
        assert stats[Tier.BRONZE].open_count == 0
        assert stats[Tier.SILVER].open_count == 0
        assert stats[Tier.GOLD].open_count == 0


# ============================================================================
# Open PRs and Unique Repos Tests
# ============================================================================


class TestOpenPRsAndUniqueRepos:
    """
    Test that open PRs don't affect unique repo calculations.

    Only merged PRs contribute to unique_repo_contribution_count.
    """

    def test_open_prs_dont_count_for_unique_repos(self, pr_factory, bronze_config):
        """
        Open PRs should not count toward unique repo requirement.
        """
        # Create merged PRs to 2 unique repos
        merged = [
            pr_factory.merged(bronze_config, repo='owner/repo-1'),
            pr_factory.merged(bronze_config, repo='owner/repo-2'),
        ]

        # Create open PRs to 5 different unique repos
        open_prs = [pr_factory.open(bronze_config, repo=f'owner/open-repo-{i}') for i in range(5)]

        stats = calculate_tier_stats(merged, [], open_prs)

        # Only merged repos counted for unique
        assert stats[Tier.BRONZE].unique_repo_contribution_count == 2
        # Open PRs tracked separately
        assert stats[Tier.BRONZE].open_count == 5
        # Tier locked due to insufficient unique repos (need 3)
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

    def test_open_prs_dont_affect_credibility(self, pr_factory, bronze_config):
        """
        Open PRs don't affect credibility calculation (only merged and closed).
        """
        # 3 merged PRs (unique repos)
        merged = pr_factory.merged_batch(bronze_config, count=3, unique_repos=True)

        # 1 closed PR
        closed = [pr_factory.closed(bronze_config)]

        # 100 open PRs (should not affect credibility)
        open_prs = pr_factory.open_batch(bronze_config, count=100, unique_repos=True)

        stats = calculate_tier_stats(merged, closed, open_prs)

        # Credibility is 3 / (3 + 1) = 75% (open PRs ignored)
        assert stats[Tier.BRONZE].credibility == pytest.approx(0.75, abs=0.01)
        # Open PRs tracked
        assert stats[Tier.BRONZE].open_count == 100


# ============================================================================
# Scoring Details Tests
# ============================================================================


class TestScoringDetails:
    """
    Test include_scoring_details=True behavior.

    When enabled, earned_score and collateral_score are accumulated.
    """

    def test_earned_score_accumulated_for_merged_prs(self, pr_factory, bronze_config):
        """
        Earned scores from merged PRs are summed when include_scoring_details=True.
        """
        # Create merged PRs with different earned scores
        merged = [
            pr_factory.merged(bronze_config, repo='owner/repo-1', earned_score=100.0),
            pr_factory.merged(bronze_config, repo='owner/repo-2', earned_score=150.0),
            pr_factory.merged(bronze_config, repo='owner/repo-3', earned_score=75.0),
        ]

        stats = calculate_tier_stats(merged, [], [], include_scoring_details=True)

        # Total earned score should be 100 + 150 + 75 = 325
        assert stats[Tier.BRONZE].earned_score == pytest.approx(325.0, abs=0.01)

    def test_earned_score_not_accumulated_without_flag(self, pr_factory, bronze_config):
        """
        Earned scores are NOT accumulated when include_scoring_details=False (default).
        """
        merged = [
            pr_factory.merged(bronze_config, repo='owner/repo-1', earned_score=100.0),
            pr_factory.merged(bronze_config, repo='owner/repo-2', earned_score=150.0),
        ]

        stats = calculate_tier_stats(merged, [])  # Default: include_scoring_details=False

        # Earned score stays at default (0.0)
        assert stats[Tier.BRONZE].earned_score == 0.0

    def test_collateral_score_accumulated_for_open_prs(self, pr_factory, bronze_config):
        """
        Collateral scores from open PRs are summed when include_scoring_details=True.
        """
        merged = pr_factory.merged_batch(bronze_config, count=3, unique_repos=True)

        # Create open PRs with different collateral scores
        open_prs = [
            pr_factory.open(bronze_config, repo='owner/open-1', collateral_score=20.0),
            pr_factory.open(bronze_config, repo='owner/open-2', collateral_score=35.0),
            pr_factory.open(bronze_config, repo='owner/open-3', collateral_score=15.0),
        ]

        stats = calculate_tier_stats(merged, [], open_prs, include_scoring_details=True)

        # Total collateral score should be 20 + 35 + 15 = 70
        assert stats[Tier.BRONZE].collateral_score == pytest.approx(70.0, abs=0.01)

    def test_low_value_prs_earned_score_not_counted(self, pr_factory, bronze_config):
        """
        Low-value merged PRs don't contribute to earned_score (since they're filtered out).
        """
        # Normal merged PR
        normal_merged = [
            pr_factory.merged(bronze_config, repo='owner/repo-1', earned_score=100.0),
        ]

        # Low-value merged PR (should be ignored entirely)
        low_value_merged = [
            pr_factory.merged(bronze_config, repo='owner/repo-2', earned_score=500.0, low_value_pr=True),
        ]

        stats = calculate_tier_stats(normal_merged + low_value_merged, [], [], include_scoring_details=True)

        # Only normal PR's earned score counted
        assert stats[Tier.BRONZE].earned_score == pytest.approx(100.0, abs=0.01)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
