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
from gittensor.validator.configurations.tier_config import (
    TIERS,
    TIERS_ORDER,
    Tier,
    TierConfig,
    TierStats,
    get_next_tier,
    get_tier_from_config,
)
from gittensor.validator.evaluation.credibility import (
    calculate_credibility_per_tier,
    calculate_tier_stats,
    is_tier_unlocked,
)


class TestGetNextTier:
    """Test get_next_tier helper function."""

    def test_bronze_next_is_silver(self):
        """Bronze → Silver."""
        assert get_next_tier(Tier.BRONZE) == Tier.SILVER

    def test_silver_next_is_gold(self):
        """Silver → Gold."""
        assert get_next_tier(Tier.SILVER) == Tier.GOLD

    def test_gold_next_is_none(self):
        """Gold is top tier, no next."""
        assert get_next_tier(Tier.GOLD) is None

    def test_progression_matches_tiers_order(self):
        """Verify get_next_tier follows TIERS_ORDER."""
        for i, tier in enumerate(TIERS_ORDER[:-1]):  # All except last
            expected_next = TIERS_ORDER[i + 1]
            assert get_next_tier(tier) == expected_next

        # Last tier has no next
        assert get_next_tier(TIERS_ORDER[-1]) is None


class TestGetTierFromConfig:
    """Test get_tier_from_config reverse lookup."""

    def test_bronze_config_returns_bronze(self, bronze_config):
        """Bronze config → Tier.BRONZE."""
        assert get_tier_from_config(bronze_config) == Tier.BRONZE

    def test_silver_config_returns_silver(self, silver_config):
        """Silver config → Tier.SILVER."""
        assert get_tier_from_config(silver_config) == Tier.SILVER

    def test_gold_config_returns_gold(self, gold_config):
        """Gold config → Tier.GOLD."""
        assert get_tier_from_config(gold_config) == Tier.GOLD

    def test_unknown_config_returns_none(self):
        """Unknown config returns None."""
        fake_config = TierConfig(
            required_credibility=0.99,
            required_min_token_score=999.0,
            required_unique_repos_count=999,
            required_min_token_score_per_repo=999.0,
            credibility_scalar=999,
        )
        assert get_tier_from_config(fake_config) is None

    def test_all_tiers_have_reversible_configs(self):
        """Every tier in TIERS can be looked up from its config."""
        for tier, config in TIERS.items():
            assert get_tier_from_config(config) == tier


class TestTiersOrderIntegrity:
    """Test TIERS_ORDER and TIERS dict structural integrity."""

    def test_tiers_order_starts_with_bronze(self):
        """First tier should be Bronze (entry level)."""
        assert TIERS_ORDER[0] == Tier.BRONZE

    def test_tiers_order_ends_with_gold(self):
        """Last tier should be Gold (highest)."""
        assert TIERS_ORDER[-1] == Tier.GOLD

    def test_tiers_order_contains_all_tiers(self):
        """TIERS_ORDER should contain all Tier enum values."""
        assert set(TIERS_ORDER) == set(Tier)

    def test_tiers_dict_has_config_for_all_tiers(self):
        """Every Tier enum value should have a config in TIERS."""
        for tier in Tier:
            assert tier in TIERS
            assert isinstance(TIERS[tier], TierConfig)

    def test_all_tiers_have_requirements(self):
        """All tiers should have unlock requirements (including Bronze)."""
        for tier in TIERS_ORDER:
            config = TIERS[tier]
            assert config.required_credibility is not None
            assert config.required_unique_repos_count is not None
            assert config.required_unique_repos_count > 0
            assert 0 < config.required_credibility <= 1.0

    def test_credibility_scalars_increase_with_tier(self):
        """Higher tiers should have higher credibility scalars."""
        scalars = [TIERS[tier].credibility_scalar for tier in TIERS_ORDER]
        for i in range(len(scalars) - 1):
            assert scalars[i] < scalars[i + 1], f'Scalar should increase: {scalars}'

    def test_token_score_requirements_increase_with_tier(self):
        """Higher tiers should require more token score per repo."""
        prev_token_per_repo = 0.0

        for tier in TIERS_ORDER:
            config = TIERS[tier]
            token_per_repo = config.required_min_token_score_per_repo or 0.0
            assert token_per_repo >= prev_token_per_repo
            prev_token_per_repo = token_per_repo

    def test_credibility_requirements_decrease_with_tier(self):
        """Higher tiers have lower credibility requirements (harder repos, more lenient)."""
        prev_credibility = 1.0

        for tier in TIERS_ORDER:
            config = TIERS[tier]
            assert config.required_credibility <= prev_credibility
            prev_credibility = config.required_credibility


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
            repository_full_name='test/repo',
            uid=0,
            hotkey='test',
            github_id='123',
            title='No tier',
            author_login='test',
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

    def _unlocked_bronze_stats(self) -> TierStats:
        """Helper to create Bronze stats that meet unlock requirements (including qualified repos)."""
        bronze_config = TIERS[Tier.BRONZE]
        required_repos = bronze_config.required_unique_repos_count or 3
        token_per_repo = bronze_config.required_min_token_score_per_repo or 5.0
        return TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos,
            token_score=required_repos * token_per_repo,
        )

    def _unlocked_silver_stats(self) -> TierStats:
        """Helper to create Silver stats that meet unlock requirements (including qualified repos)."""
        silver_config = TIERS[Tier.SILVER]
        required_repos = silver_config.required_unique_repos_count or 3
        token_per_repo = silver_config.required_min_token_score_per_repo or 10.0
        min_total = silver_config.required_min_token_score or 50.0
        return TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos,
            token_score=max(required_repos * token_per_repo, min_total),
        )

    def test_bronze_locked_with_no_activity(self, empty_tier_stats):
        """Bronze is locked when miner has no PRs."""
        assert is_tier_unlocked(Tier.BRONZE, empty_tier_stats) is False

    def test_bronze_requires_merges_and_credibility(self):
        """Bronze requires meeting merge count, credibility threshold, and unique repos."""
        bronze_config = TIERS[Tier.BRONZE]
        required_repos = bronze_config.required_unique_repos_count
        required_credibility = bronze_config.required_credibility
        required_unique_repos = bronze_config.required_unique_repos_count

        closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 1

        # Not enough merges
        stats = {
            Tier.BRONZE: TierStats(
                merged_count=required_repos - 1,
                closed_count=closed_count,
                unique_repo_contribution_count=required_unique_repos,
                qualified_unique_repo_count=required_unique_repos,
                token_score=required_unique_repos * 30.0,
            ),
            Tier.SILVER: TierStats(),
            Tier.GOLD: TierStats(),
        }
        assert stats[Tier.BRONZE].credibility < required_credibility
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

        # Enough merges, meets credibility, meets unique repos
        stats[Tier.BRONZE] = TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_unique_repos,
            qualified_unique_repo_count=required_unique_repos,
            token_score=required_unique_repos * 30.0,
        )
        assert is_tier_unlocked(Tier.BRONZE, stats) is True

    def test_silver_requires_bronze_unlocked(self):
        """Silver cannot be unlocked if Bronze is locked."""
        silver_config = TIERS[Tier.SILVER]

        # Perfect Silver stats but Bronze locked
        stats = {
            Tier.BRONZE: TierStats(),  # No Bronze activity
            Tier.SILVER: TierStats(
                merged_count=silver_config.required_unique_repos_count,
                closed_count=0,
                unique_repo_contribution_count=silver_config.required_unique_repos_count,
                qualified_unique_repo_count=silver_config.required_unique_repos_count,
                token_score=silver_config.required_unique_repos_count * 30.0,
            ),
            Tier.GOLD: TierStats(),
        }
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Unlock Bronze, Silver should now unlock
        stats[Tier.BRONZE] = self._unlocked_bronze_stats()
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_silver_requires_qualified_repos(self):
        """Silver requires meeting its qualified unique repo count requirement."""
        silver_config = TIERS[Tier.SILVER]
        required_repos = silver_config.required_unique_repos_count
        required_token_score = silver_config.required_min_token_score or 50.0

        # Not enough qualified repos (one short)
        stats = {
            Tier.BRONZE: self._unlocked_bronze_stats(),
            Tier.SILVER: TierStats(
                merged_count=required_repos,
                closed_count=0,
                unique_repo_contribution_count=required_repos,
                qualified_unique_repo_count=required_repos - 1,  # One short!
                token_score=required_token_score + 10.0,  # Token score is fine
            ),
            Tier.GOLD: TierStats(),
        }
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Enough qualified repos
        stats[Tier.SILVER] = TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos,
            token_score=required_token_score + 10.0,
        )
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_silver_requires_credibility(self):
        """Silver requires meeting its credibility requirement."""
        silver_config = TIERS[Tier.SILVER]
        required_repos = silver_config.required_unique_repos_count
        required_credibility = silver_config.required_credibility
        required_unique_repos = silver_config.required_unique_repos_count

        # Calculate closed count to be just below credibility threshold
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 1

        stats = {
            Tier.BRONZE: self._unlocked_bronze_stats(),
            Tier.SILVER: TierStats(
                merged_count=required_repos,
                closed_count=closed_count,
                unique_repo_contribution_count=required_unique_repos,
                qualified_unique_repo_count=required_unique_repos,
                token_score=required_unique_repos * 30.0,
            ),
            Tier.GOLD: TierStats(),
        }
        assert stats[Tier.SILVER].credibility < required_credibility
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Fix credibility
        stats[Tier.SILVER] = TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_unique_repos,
            qualified_unique_repo_count=required_unique_repos,
            token_score=required_unique_repos * 30.0,
        )
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_silver_requires_all_conditions(self):
        """Silver requires credibility, total token score, AND qualified unique repos."""
        silver_config = TIERS[Tier.SILVER]
        required_repos = silver_config.required_unique_repos_count
        required_credibility = silver_config.required_credibility
        required_token_score = silver_config.required_min_token_score or 50.0

        # Calculate closed count for below credibility
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 1

        # Has qualified repos + token score, but low credibility
        stats = {
            Tier.BRONZE: self._unlocked_bronze_stats(),
            Tier.SILVER: TierStats(
                merged_count=required_repos,
                closed_count=closed_count,
                unique_repo_contribution_count=required_repos,
                qualified_unique_repo_count=required_repos,
                token_score=required_token_score + 10.0,
            ),
            Tier.GOLD: TierStats(),
        }
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Has credibility + token score, but not enough qualified repos
        stats[Tier.SILVER] = TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos - 1,  # One short!
            token_score=required_token_score + 10.0,
        )
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Has credibility + qualified repos, but low token score
        stats[Tier.SILVER] = TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos,
            token_score=required_token_score - 10.0,  # Below threshold!
        )
        assert is_tier_unlocked(Tier.SILVER, stats) is False

        # Has all three
        stats[Tier.SILVER] = TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos,
            token_score=required_token_score + 10.0,
        )
        assert is_tier_unlocked(Tier.SILVER, stats) is True

    def test_gold_requires_qualified_repos(self):
        """Gold requires meeting its qualified unique repo count requirement."""
        gold_config = TIERS[Tier.GOLD]
        required_repos = gold_config.required_unique_repos_count
        required_token_score = gold_config.required_min_token_score or 150.0

        # Not enough qualified repos (one short)
        stats = {
            Tier.BRONZE: self._unlocked_bronze_stats(),
            Tier.SILVER: self._unlocked_silver_stats(),
            Tier.GOLD: TierStats(
                merged_count=required_repos,
                closed_count=0,
                unique_repo_contribution_count=required_repos,
                qualified_unique_repo_count=required_repos - 1,  # One short!
                token_score=required_token_score + 10.0,
            ),
        }
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Enough qualified repos
        stats[Tier.GOLD] = TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos,
            token_score=required_token_score + 10.0,
        )
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_gold_requires_credibility(self):
        """Gold requires meeting its credibility requirement."""
        gold_config = TIERS[Tier.GOLD]
        required_repos = gold_config.required_unique_repos_count
        required_credibility = gold_config.required_credibility
        required_token_score = gold_config.required_min_token_score or 150.0

        # Calculate closed count to be just below credibility threshold
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 1

        stats = {
            Tier.BRONZE: self._unlocked_bronze_stats(),
            Tier.SILVER: self._unlocked_silver_stats(),
            Tier.GOLD: TierStats(
                merged_count=required_repos,
                closed_count=closed_count,
                unique_repo_contribution_count=required_repos,
                qualified_unique_repo_count=required_repos,
                token_score=required_token_score + 10.0,
            ),
        }
        assert stats[Tier.GOLD].credibility < required_credibility
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Fix credibility
        stats[Tier.GOLD] = TierStats(
            merged_count=required_repos,
            closed_count=0,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos,
            token_score=required_token_score + 10.0,
        )
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_gold_requires_silver_unlocked(self):
        """Gold cannot be unlocked if Silver is locked."""
        silver_config = TIERS[Tier.SILVER]
        gold_config = TIERS[Tier.GOLD]
        silver_token_score = silver_config.required_min_token_score or 50.0
        gold_token_score = gold_config.required_min_token_score or 150.0

        # Gold has perfect stats, but Silver is locked (not enough qualified repos)
        stats = {
            Tier.BRONZE: self._unlocked_bronze_stats(),
            Tier.SILVER: TierStats(
                merged_count=silver_config.required_unique_repos_count,
                closed_count=0,
                unique_repo_contribution_count=silver_config.required_unique_repos_count,
                qualified_unique_repo_count=silver_config.required_unique_repos_count - 1,  # One short!
                token_score=silver_token_score + 10.0,
            ),
            Tier.GOLD: TierStats(
                merged_count=gold_config.required_unique_repos_count + 5,
                closed_count=0,
                unique_repo_contribution_count=gold_config.required_unique_repos_count,
                qualified_unique_repo_count=gold_config.required_unique_repos_count,
                token_score=gold_token_score + 10.0,
            ),
        }
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Unlock Silver
        stats[Tier.SILVER] = self._unlocked_silver_stats()
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_gold_requires_bronze_unlocked(self):
        """Gold cannot be unlocked if Bronze is locked (cascade)."""
        gold_config = TIERS[Tier.GOLD]

        # Perfect Silver and Gold stats, but Bronze locked
        stats = {
            Tier.BRONZE: TierStats(),  # No Bronze activity
            Tier.SILVER: self._unlocked_silver_stats(),
            Tier.GOLD: TierStats(
                merged_count=gold_config.required_unique_repos_count + 5,
                closed_count=0,
                unique_repo_contribution_count=gold_config.required_unique_repos_count,
                qualified_unique_repo_count=gold_config.required_unique_repos_count,
                token_score=gold_config.required_min_token_score or 150.0,
            ),
        }
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Unlock Bronze
        stats[Tier.BRONZE] = self._unlocked_bronze_stats()
        assert is_tier_unlocked(Tier.GOLD, stats) is True

    def test_cascading_unlock(self):
        """All tiers unlock when all requirements are met (including unique repos)."""
        gold_config = TIERS[Tier.GOLD]
        stats = {
            Tier.BRONZE: self._unlocked_bronze_stats(),
            Tier.SILVER: self._unlocked_silver_stats(),
            Tier.GOLD: TierStats(
                merged_count=gold_config.required_unique_repos_count,
                closed_count=0,
                unique_repo_contribution_count=gold_config.required_unique_repos_count,
                qualified_unique_repo_count=gold_config.required_unique_repos_count,
                token_score=gold_config.required_min_token_score or 150.0,
            ),
        }
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is True


# ============================================================================
# Bronze-Specific Edge Cases
# ============================================================================


class TestBronzeEdgeCases:
    """
    Test Bronze-specific edge cases now that Bronze has unlock requirements.

    Bronze requirements:
    - required_repos: 3
    - required_credibility: 80%
    """

    def test_bronze_locked_below_merge_threshold(self, pr_factory, bronze_config):
        """Bronze stays locked when merges are below requirement."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count

        # One below threshold
        merged = pr_factory.merged_batch(bronze_config, count=required_repos - 1)
        stats = calculate_tier_stats(merged, [])
        credibility = calculate_credibility_per_tier(merged, [])

        assert is_tier_unlocked(Tier.BRONZE, stats) is False
        assert credibility.get(Tier.BRONZE, 0.0) == 0.0

    def test_bronze_locked_below_credibility_threshold(self, pr_factory, bronze_config):
        """Bronze stays locked when credibility is below requirement."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_credibility = bronze_tier_config.required_credibility

        # Enough merges but terrible credibility
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 2
        merged = pr_factory.merged_batch(bronze_config, count=required_repos)
        closed = pr_factory.closed_batch(bronze_config, count=closed_count)

        stats = calculate_tier_stats(merged, closed)
        credibility = calculate_credibility_per_tier(merged, closed)

        assert stats[Tier.BRONZE].merged_count >= required_repos
        assert stats[Tier.BRONZE].credibility < required_credibility
        assert is_tier_unlocked(Tier.BRONZE, stats) is False
        assert credibility.get(Tier.BRONZE, 0.0) == 0.0

    def test_bronze_unlocks_at_exact_threshold(self, pr_factory, bronze_config):
        """Bronze unlocks when exactly at merge, credibility, and unique repo thresholds."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_credibility = bronze_tier_config.required_credibility

        # Calculate closed count for exactly at credibility threshold
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility)
        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)
        closed = pr_factory.closed_batch(bronze_config, count=closed_count, unique_repos=True)

        stats = calculate_tier_stats(merged, closed)
        credibility = calculate_credibility_per_tier(merged, closed)

        assert stats[Tier.BRONZE].credibility >= required_credibility
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert credibility[Tier.BRONZE] >= required_credibility

    def test_bronze_demotion_cascades_to_all_tiers(self, pr_factory, bronze_config, silver_config, gold_config):
        """When Bronze locks, Silver and Gold cascade to locked."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]
        bronze_required_credibility = bronze_tier_config.required_credibility

        # Perfect Silver and Gold stats, but Bronze has terrible credibility
        bronze_merged = bronze_tier_config.required_unique_repos_count
        bronze_closed = int(bronze_merged * (1 - bronze_required_credibility) / bronze_required_credibility) + 2

        merged = (
            pr_factory.merged_batch(bronze_config, count=bronze_merged)
            + pr_factory.merged_batch(silver_config, count=silver_tier_config.required_unique_repos_count + 5)
            + pr_factory.merged_batch(gold_config, count=gold_tier_config.required_unique_repos_count + 5)
        )
        closed = pr_factory.closed_batch(bronze_config, count=bronze_closed)

        stats = calculate_tier_stats(merged, closed)
        credibility = calculate_credibility_per_tier(merged, closed)

        # Bronze locked due to low credibility
        assert is_tier_unlocked(Tier.BRONZE, stats) is False
        # Silver and Gold cascade to locked
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        # All credibilities are 0
        assert credibility.get(Tier.BRONZE, 0.0) == 0.0
        assert credibility.get(Tier.SILVER, 0.0) == 0.0
        assert credibility.get(Tier.GOLD, 0.0) == 0.0

    def test_bronze_recovery_from_low_credibility(self, pr_factory, bronze_config):
        """Bronze can recover by adding more merged PRs (to unique repos)."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_credibility = bronze_tier_config.required_credibility

        # Start locked: enough merges but low credibility (all unique repos)
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 2
        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)
        closed = pr_factory.closed_batch(bronze_config, count=closed_count, unique_repos=True)

        stats = calculate_tier_stats(merged, closed)
        assert is_tier_unlocked(Tier.BRONZE, stats) is False

        # Recovery: add more merged PRs to boost credibility (to unique repos)
        extra_needed = (
            int((required_credibility * (required_repos + closed_count) - required_repos) / (1 - required_credibility))
            + 1
        )
        merged.extend(pr_factory.merged_batch(bronze_config, count=extra_needed, unique_repos=True))

        stats = calculate_tier_stats(merged, closed)
        credibility = calculate_credibility_per_tier(merged, closed)

        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert credibility[Tier.BRONZE] >= required_credibility


class TestBronzeLookbackExpiry:
    """
    Test Bronze PRs expiring from lookback window.

    Miners must continuously maintain Bronze to keep higher tiers unlocked.
    """

    def test_bronze_prs_expire_locks_all_tiers(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        All tiers lock when Bronze PRs expire.

        Scenario:
        - Miner had all tiers unlocked
        - Bronze PRs expire outside lookback window
        - All tiers cascade to locked
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

        # Before: All tiers unlocked (with unique repos)
        merged_before = (
            pr_factory.merged_batch(
                bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
            )
            + pr_factory.merged_batch(
                silver_config,
                count=silver_tier_config.required_unique_repos_count,
                unique_repos=True,
                token_score=silver_token_per_pr,
            )
            + pr_factory.merged_batch(
                gold_config,
                count=gold_tier_config.required_unique_repos_count,
                unique_repos=True,
                token_score=gold_token_per_pr,
            )
        )

        stats_before = calculate_tier_stats(merged_before, [])
        assert is_tier_unlocked(Tier.BRONZE, stats_before) is True
        assert is_tier_unlocked(Tier.GOLD, stats_before) is True

        # After: Bronze PRs expired, only Silver and Gold remain
        pr_factory.reset()
        merged_after = pr_factory.merged_batch(
            silver_config,
            count=silver_tier_config.required_unique_repos_count,
            unique_repos=True,
            token_score=silver_token_per_pr,
        ) + pr_factory.merged_batch(
            gold_config,
            count=gold_tier_config.required_unique_repos_count,
            unique_repos=True,
            token_score=gold_token_per_pr,
        )

        stats_after = calculate_tier_stats(merged_after, [])
        credibility_after = calculate_credibility_per_tier(merged_after, [])

        # All tiers locked due to Bronze cascade
        assert is_tier_unlocked(Tier.BRONZE, stats_after) is False
        assert is_tier_unlocked(Tier.SILVER, stats_after) is False
        assert is_tier_unlocked(Tier.GOLD, stats_after) is False
        assert credibility_after.get(Tier.GOLD, 0.0) == 0.0

    def test_partial_bronze_expiry_still_unlocked(self, pr_factory, bronze_config, silver_config):
        """
        Partial Bronze expiry doesn't lock if enough PRs remain.

        Scenario:
        - Miner had extra Bronze merges
        - Some expire → still meets threshold
        - Silver stays unlocked
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        extra_bronze = 2

        # Before: Bronze with buffer (unique repos)
        merged_before = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count + extra_bronze, unique_repos=True
        ) + pr_factory.merged_batch(
            silver_config, count=silver_tier_config.required_unique_repos_count, unique_repos=True
        )

        stats_before = calculate_tier_stats(merged_before, [])
        assert is_tier_unlocked(Tier.SILVER, stats_before) is True

        # After: Extra Bronze expires, exactly at threshold
        pr_factory.reset()
        merged_after = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
        ) + pr_factory.merged_batch(
            silver_config, count=silver_tier_config.required_unique_repos_count, unique_repos=True
        )

        stats_after = calculate_tier_stats(merged_after, [])
        assert is_tier_unlocked(Tier.BRONZE, stats_after) is True
        assert is_tier_unlocked(Tier.SILVER, stats_after) is True

    def test_one_bronze_expiry_below_threshold_locks_all(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Losing one Bronze PR when exactly at threshold locks all tiers.
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

        # At threshold: exactly bronze_required merges (unique repos)
        merged = (
            pr_factory.merged_batch(
                bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
            )
            + pr_factory.merged_batch(
                silver_config,
                count=silver_tier_config.required_unique_repos_count,
                unique_repos=True,
                token_score=silver_token_per_pr,
            )
            + pr_factory.merged_batch(
                gold_config,
                count=gold_tier_config.required_unique_repos_count,
                unique_repos=True,
                token_score=gold_token_per_pr,
            )
        )

        stats = calculate_tier_stats(merged, [])
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # One Bronze expires
        pr_factory.reset()
        merged_after = (
            pr_factory.merged_batch(
                bronze_config, count=bronze_tier_config.required_unique_repos_count - 1, unique_repos=True
            )
            + pr_factory.merged_batch(
                silver_config,
                count=silver_tier_config.required_unique_repos_count,
                unique_repos=True,
                token_score=silver_token_per_pr,
            )
            + pr_factory.merged_batch(
                gold_config,
                count=gold_tier_config.required_unique_repos_count,
                unique_repos=True,
                token_score=gold_token_per_pr,
            )
        )

        stats_after = calculate_tier_stats(merged_after, [])
        assert is_tier_unlocked(Tier.BRONZE, stats_after) is False
        assert is_tier_unlocked(Tier.SILVER, stats_after) is False
        assert is_tier_unlocked(Tier.GOLD, stats_after) is False

    def test_bronze_credibility_drops_on_expiry(self, pr_factory, bronze_config):
        """
        Expiring merged Bronze PRs can drop credibility below threshold.
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_credibility = bronze_tier_config.required_credibility

        # Before: At credibility threshold with extra merged PRs (unique repos)
        merged_count = required_repos + 2
        closed_count = int(merged_count * (1 - required_credibility) / required_credibility)

        merged = pr_factory.merged_batch(bronze_config, count=merged_count, unique_repos=True)
        closed = pr_factory.closed_batch(bronze_config, count=closed_count, unique_repos=True)

        stats_before = calculate_tier_stats(merged, closed)
        assert stats_before[Tier.BRONZE].credibility >= required_credibility
        assert is_tier_unlocked(Tier.BRONZE, stats_before) is True

        # After: Some merged PRs expire (still need unique repo threshold met)
        pr_factory.reset()
        merged_after = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)

        stats_after = calculate_tier_stats(merged_after, closed)
        # May drop below threshold now
        if stats_after[Tier.BRONZE].credibility < required_credibility:
            assert is_tier_unlocked(Tier.BRONZE, stats_after) is False

    def test_bronze_maintenance_required_for_gold(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Demonstrates continuous Bronze maintenance is required for Gold.

        Scenario:
        - Miner gets Gold, then focuses only on Gold PRs
        - Bronze PRs slowly expire
        - Eventually Bronze locks → Gold cascades to locked
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        # Phase 1: Full unlock with buffer (unique repos)
        bronze_prs = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count + 2, unique_repos=True
        )
        silver_prs = pr_factory.merged_batch(
            silver_config, count=silver_tier_config.required_unique_repos_count, unique_repos=True
        )
        gold_prs = pr_factory.merged_batch(
            gold_config, count=gold_tier_config.required_unique_repos_count + 5, unique_repos=True
        )

        stats = calculate_tier_stats(bronze_prs + silver_prs + gold_prs, [])
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Phase 2: Some Bronze expires (still above threshold)
        stats = calculate_tier_stats(
            bronze_prs[: bronze_tier_config.required_unique_repos_count + 1] + silver_prs + gold_prs, []
        )
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Phase 3: More Bronze expires (exactly at threshold)
        stats = calculate_tier_stats(
            bronze_prs[: bronze_tier_config.required_unique_repos_count] + silver_prs + gold_prs, []
        )
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Phase 4: One more Bronze expires (below threshold)
        stats = calculate_tier_stats(
            bronze_prs[: bronze_tier_config.required_unique_repos_count - 1] + silver_prs + gold_prs, []
        )
        assert is_tier_unlocked(Tier.BRONZE, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False  # Cascade!

    def test_refreshing_bronze_restores_all_tiers(self, pr_factory, bronze_config, silver_config, gold_config):
        """
        Adding new Bronze PRs restores all tier access.
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

        # Lost access: Bronze one below threshold (unique repos)
        old_bronze = pr_factory.merged_batch(
            bronze_config, count=bronze_tier_config.required_unique_repos_count - 1, unique_repos=True
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

        stats = calculate_tier_stats(old_bronze + silver_prs + gold_prs, [])
        assert is_tier_unlocked(Tier.BRONZE, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Refresh: add 1 new Bronze PR (to unique repo)
        new_bronze = pr_factory.merged_batch(bronze_config, count=1, unique_repos=True)

        stats = calculate_tier_stats(old_bronze + new_bronze + silver_prs + gold_prs, [])
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is True


# ============================================================================
# calculate_credibility_per_tier Tests
# ============================================================================


class TestCredibilityCalculation:
    """Test calculate_credibility_per_tier function."""

    def test_no_activity_returns_empty(self):
        result = calculate_credibility_per_tier([], [])
        assert result == {}

    def test_single_tier_credibility(self, pr_factory, bronze_config):
        """Test credibility calculation for an unlocked tier (with unique repos)."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count

        # Create enough merges to unlock Bronze with 100% credibility (unique repos)
        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)

        result = calculate_credibility_per_tier(merged, [])

        assert result[Tier.BRONZE] == 1.0

    def test_credibility_with_some_closed(self, pr_factory, bronze_config):
        """Test credibility when there are closed PRs but tier is still unlocked (unique repos)."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_credibility = bronze_tier_config.required_credibility

        # Calculate max closed to still meet credibility requirement
        # credibility = merged / (merged + closed) >= required_credibility
        # closed <= merged * (1 - required_credibility) / required_credibility
        max_closed = int(required_repos * (1 - required_credibility) / required_credibility)

        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)
        closed = pr_factory.closed_batch(bronze_config, count=max_closed, unique_repos=True)

        result = calculate_credibility_per_tier(merged, closed)

        expected = required_repos / (required_repos + max_closed)
        assert result[Tier.BRONZE] == pytest.approx(expected, abs=0.01)
        assert result[Tier.BRONZE] >= required_credibility

    def test_locked_tier_returns_zero(self, pr_factory, bronze_config, silver_config):
        """Silver returns 0.0 when locked (Bronze not unlocked)."""
        silver_tier_config = TIERS[Tier.SILVER]

        # Silver has enough merges but Bronze is not unlocked (unique repos)
        merged = pr_factory.merged_batch(
            silver_config, count=silver_tier_config.required_unique_repos_count, unique_repos=True
        )

        result = calculate_credibility_per_tier(merged, [])

        assert result.get(Tier.SILVER, 0.0) == 0.0

    def test_tier_locked_due_to_low_credibility(self, pr_factory, bronze_config):
        """Tier returns 0.0 when credibility is below requirement."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count
        required_credibility = bronze_tier_config.required_credibility

        # Create PRs that have enough merges but credibility below requirement (unique repos)
        closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 2

        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)
        closed = pr_factory.closed_batch(bronze_config, count=closed_count, unique_repos=True)

        result = calculate_credibility_per_tier(merged, closed)

        # Bronze is locked due to low credibility
        assert result.get(Tier.BRONZE, 0.0) == 0.0

    def test_100_percent_credibility(self, pr_factory, bronze_config):
        """Test 100% credibility with no closed PRs (unique repos)."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count

        merged = pr_factory.merged_batch(bronze_config, count=required_repos + 2, unique_repos=True)
        result = calculate_credibility_per_tier(merged, [])
        assert result[Tier.BRONZE] == 1.0

    def test_0_percent_credibility(self, pr_factory, bronze_config):
        """No merged PRs means tier is locked (0.0 credibility)."""
        closed = pr_factory.closed_batch(bronze_config, count=5, unique_repos=True)
        result = calculate_credibility_per_tier([], closed)
        # Bronze is locked because no merged PRs (doesn't meet required_repos)
        assert result.get(Tier.BRONZE, 0.0) == 0.0


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

    def test_gold_demoted_not_enough_merges(self, pr_factory, bronze_config, silver_config, gold_config):
        """Gold locks when merge count drops below requirement (with unique repos)."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]

        merged = (
            pr_factory.merged_batch(
                bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
            )
            + pr_factory.merged_batch(
                silver_config, count=silver_tier_config.required_unique_repos_count, unique_repos=True
            )
            + pr_factory.merged_batch(
                gold_config, count=gold_tier_config.required_unique_repos_count - 1, unique_repos=True
            )  # One short
        )

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

    def test_recovery_from_demotion(self, pr_factory, bronze_config, silver_config, gold_config):
        """Miner can recover from demotion by getting more merges (to unique repos)."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]
        gold_required_credibility = gold_tier_config.required_credibility

        # Initially demoted: below gold credibility threshold (unique repos)
        gold_merged_count = gold_tier_config.required_unique_repos_count
        gold_closed_count = int(gold_merged_count * (1 - gold_required_credibility) / gold_required_credibility) + 2

        merged = (
            pr_factory.merged_batch(
                bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
            )
            + pr_factory.merged_batch(
                silver_config, count=silver_tier_config.required_unique_repos_count, unique_repos=True
            )
            + pr_factory.merged_batch(gold_config, count=gold_merged_count, unique_repos=True)
        )
        closed = pr_factory.closed_batch(gold_config, count=gold_closed_count, unique_repos=True)

        stats = calculate_tier_stats(merged, closed)
        assert is_tier_unlocked(Tier.GOLD, stats) is False

        # Recovery: add more merges to get above credibility threshold (unique repos)
        # new_cred = (gold_merged + extra) / (gold_merged + gold_closed + extra) >= gold_required_credibility
        # Solve for extra: extra >= (gold_required_credibility * (gold_merged + gold_closed) - gold_merged) / (1 - gold_required_credibility)
        extra_needed = (
            int(
                (gold_required_credibility * (gold_merged_count + gold_closed_count) - gold_merged_count)
                / (1 - gold_required_credibility)
            )
            + 1
        )
        merged.extend(pr_factory.merged_batch(gold_config, count=extra_needed, unique_repos=True))

        stats = calculate_tier_stats(merged, closed)
        credibility = calculate_credibility_per_tier(merged, closed)

        assert is_tier_unlocked(Tier.GOLD, stats) is True
        assert credibility[Tier.GOLD] >= gold_required_credibility

    def test_spam_destroys_all_tiers(self, spammer_miner):
        """Massive closed PRs tanks credibility everywhere."""
        stats = calculate_tier_stats(spammer_miner.merged, spammer_miner.closed)
        credibility = calculate_credibility_per_tier(spammer_miner.merged, spammer_miner.closed)

        # All tiers locked due to terrible credibility
        # Bronze: 5 merged, 20 closed = 20% (needs 80% for unlock)
        assert is_tier_unlocked(Tier.BRONZE, stats) is False
        assert credibility.get(Tier.BRONZE, 0.0) == 0.0

        # Silver & Gold: LOCKED (cascade from Bronze)
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False

    def test_gradual_decline(self, pr_factory, bronze_config, silver_config, gold_config):
        """Miner starts strong then declines (with unique repos)."""
        bronze_tier_config = TIERS[Tier.BRONZE]
        silver_tier_config = TIERS[Tier.SILVER]
        gold_tier_config = TIERS[Tier.GOLD]
        gold_required_credibility = gold_tier_config.required_credibility

        # Phase 1: Strong start - all tiers unlocked (unique repos)
        merged = (
            pr_factory.merged_batch(
                bronze_config, count=bronze_tier_config.required_unique_repos_count, unique_repos=True
            )
            + pr_factory.merged_batch(
                silver_config, count=silver_tier_config.required_unique_repos_count, unique_repos=True
            )
            + pr_factory.merged_batch(
                gold_config, count=gold_tier_config.required_unique_repos_count + 3, unique_repos=True
            )
        )

        stats = calculate_tier_stats(merged, [])
        assert is_tier_unlocked(Tier.GOLD, stats) is True

        # Phase 2: Decline - add closed PRs to drop Gold below credibility threshold (unique repos)
        gold_merged_count = gold_tier_config.required_unique_repos_count + 3
        closed_for_drop = int(gold_merged_count * (1 - gold_required_credibility) / gold_required_credibility) + 2
        closed = pr_factory.closed_batch(gold_config, count=closed_for_drop, unique_repos=True)

        stats = calculate_tier_stats(merged, closed)
        credibility = calculate_credibility_per_tier(merged, closed)

        # Gold now LOCKED (below credibility threshold)
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert credibility[Tier.GOLD] == 0.0

        # Silver still OK (no closed at Silver tier)
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

        # Bronze: 9 merged, 1 closed = 90% (above 80% threshold)
        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert credibility[Tier.BRONZE] == pytest.approx(0.9, abs=0.01)

        # Silver: 11 merged, 9 closed = 55% (below 75% threshold) - LOCKED
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert credibility[Tier.SILVER] == 0.0

        # Gold: 60% (below 70% threshold) - LOCKED (cascade from Silver)
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert credibility[Tier.GOLD] == 0.0


# ============================================================================
# Edge Cases & Boundary Tests
# ============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_exactly_at_silver_threshold(self, silver_threshold_miner):
        """Test exactly at Silver credibility requirement."""
        silver_tier_config = TIERS[Tier.SILVER]
        required_credibility = silver_tier_config.required_credibility

        stats = calculate_tier_stats(silver_threshold_miner.merged, silver_threshold_miner.closed)
        credibility = calculate_credibility_per_tier(silver_threshold_miner.merged, silver_threshold_miner.closed)

        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert credibility[Tier.SILVER] >= required_credibility

    def test_exactly_at_gold_threshold(self, gold_threshold_miner):
        """Test exactly at Gold credibility requirement."""
        gold_tier_config = TIERS[Tier.GOLD]
        required_credibility = gold_tier_config.required_credibility

        stats = calculate_tier_stats(gold_threshold_miner.merged, gold_threshold_miner.closed)
        credibility = calculate_credibility_per_tier(gold_threshold_miner.merged, gold_threshold_miner.closed)

        assert is_tier_unlocked(Tier.GOLD, stats) is True
        assert credibility[Tier.GOLD] >= required_credibility

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
        """Single PR is not enough to unlock any tier."""
        bronze_required = TIERS[Tier.BRONZE].required_unique_repos_count

        # Bronze: NOT unlocked with just 1 PR (needs required_repos)
        merged = [pr_factory.merged(bronze_config)]
        credibility = calculate_credibility_per_tier(merged, [])
        # With 1 PR and Bronze requiring more merges, Bronze is locked
        if bronze_required > 1:
            assert credibility.get(Tier.BRONZE, 0.0) == 0.0
        else:
            # If Bronze only needs 1 merge, it would be unlocked
            assert credibility[Tier.BRONZE] == 1.0

        # Silver: NOT unlocked (need Bronze unlocked + Silver requirements)
        merged = [pr_factory.merged(silver_config)]
        credibility = calculate_credibility_per_tier(merged, [])
        assert credibility.get(Tier.SILVER, 0.0) == 0.0

        # Gold: NOT unlocked (need Bronze + Silver unlocked + Gold requirements)
        merged = [pr_factory.merged(gold_config)]
        credibility = calculate_credibility_per_tier(merged, [])
        assert credibility.get(Tier.GOLD, 0.0) == 0.0

    def test_activation_threshold_boundary(self, pr_factory, bronze_config):
        """Test activation threshold behavior.

        When required_repos >= activation_threshold (which is true for Bronze: 3 >= 2),
        the "below activation threshold with tier unlocked" scenario can't happen.

        This test verifies that:
        1. Below required_repos = tier locked (credibility = 0)
        2. At required_repos with perfect record and unique repos = tier unlocked (credibility = 1.0)
        """
        bronze_tier_config = TIERS[Tier.BRONZE]
        required_repos = bronze_tier_config.required_unique_repos_count

        # Below required_repos: tier locked (even with unique repos)
        merged = pr_factory.merged_batch(bronze_config, count=required_repos - 1, unique_repos=True)
        cred = calculate_credibility_per_tier(merged, [])
        assert cred.get(Tier.BRONZE, 0.0) == 0.0  # Locked due to not enough merges

        # At required_repos with perfect record and unique repos: tier unlocked
        pr_factory.reset()
        merged = pr_factory.merged_batch(bronze_config, count=required_repos, unique_repos=True)
        cred = calculate_credibility_per_tier(merged, [])
        assert cred[Tier.BRONZE] == 1.0  # 100% credibility

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

    def test_new_miner_no_tiers(self, new_miner):
        """New miner has no tiers unlocked."""
        stats = calculate_tier_stats(new_miner.merged, new_miner.closed)

        assert is_tier_unlocked(Tier.BRONZE, stats) is False
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert is_tier_unlocked(Tier.GOLD, stats) is False

    def test_bronze_miner_scenario(self, bronze_miner):
        """Bronze-only miner with Bronze unlocked."""
        stats = calculate_tier_stats(bronze_miner.merged, bronze_miner.closed)
        cred = calculate_credibility_per_tier(bronze_miner.merged, bronze_miner.closed)

        assert is_tier_unlocked(Tier.BRONZE, stats) is True
        assert is_tier_unlocked(Tier.SILVER, stats) is False
        assert cred[Tier.BRONZE] == 1.0  # 100% with no closed PRs

    def test_silver_miner_scenario(self, silver_unlocked_miner):
        """Silver miner with 100% credibility (no closed PRs)."""
        stats = calculate_tier_stats(silver_unlocked_miner.merged, silver_unlocked_miner.closed)
        cred = calculate_credibility_per_tier(silver_unlocked_miner.merged, silver_unlocked_miner.closed)

        assert is_tier_unlocked(Tier.SILVER, stats) is True
        assert is_tier_unlocked(Tier.GOLD, stats) is False
        assert cred[Tier.SILVER] == 1.0  # 100% credibility with no closed PRs

    def test_gold_miner_scenario(self, gold_unlocked_miner):
        """Gold miner with 100% credibility (no closed PRs)."""
        stats = calculate_tier_stats(gold_unlocked_miner.merged, gold_unlocked_miner.closed)
        cred = calculate_credibility_per_tier(gold_unlocked_miner.merged, gold_unlocked_miner.closed)

        assert is_tier_unlocked(Tier.GOLD, stats) is True
        assert cred[Tier.GOLD] == 1.0  # 100% credibility with no closed PRs

    def test_open_prs_tracked_separately(self, miner_with_open_prs):
        """Open PRs are counted but don't affect credibility."""
        stats = calculate_tier_stats(miner_with_open_prs.merged, miner_with_open_prs.closed, miner_with_open_prs.open)

        # Open PRs are counted
        assert stats[Tier.BRONZE].open_count == 2
        assert stats[Tier.SILVER].open_count == 3

        # But don't affect credibility calculation
        # miner_with_open_prs fixture: 3 merged, 1 closed at Bronze
        cred = calculate_credibility_per_tier(miner_with_open_prs.merged, miner_with_open_prs.closed)
        # Bronze: 3 merged, 1 closed = 75% credibility
        # Bronze requires 70% credibility, so Bronze is unlocked
        assert cred.get(Tier.BRONZE, 0.0) == 0.75  # Unlocked with 75% credibility


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
