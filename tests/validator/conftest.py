# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Pytest fixtures for validator tests.

This module provides reusable fixtures for testing tier credibility,
scoring, and other validator functionality.

Usage:
    Fixtures are automatically available in all test files under tests/validator/

    # In your test file:
    def test_something(pr_factory, bronze_config):
        pr = pr_factory(state=PRState.MERGED, tier=bronze_config)
        ...
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

import pytest

from gittensor.classes import PRState, PullRequest
from gittensor.validator.configurations.tier_config import (
    TIERS,
    Tier,
    TierConfig,
    TierStats,
)

# ============================================================================
# Tier Config Fixtures
# ============================================================================


@pytest.fixture
def bronze_config() -> TierConfig:
    """Bronze tier configuration."""
    return TIERS[Tier.BRONZE]


@pytest.fixture
def silver_config() -> TierConfig:
    """Silver tier configuration."""
    return TIERS[Tier.SILVER]


@pytest.fixture
def gold_config() -> TierConfig:
    """Gold tier configuration."""
    return TIERS[Tier.GOLD]


# ============================================================================
# PR Factory Fixture
# ============================================================================


@dataclass
class PRBuilder:
    """
    Builder for creating mock PullRequests with sensible defaults.

    Usage:
        pr = pr_factory.merged(tier=bronze_config)
        pr = pr_factory.closed(tier=silver_config, number=5)
        pr = pr_factory.open(tier=gold_config)

        # Or use the generic create method:
        pr = pr_factory.create(state=PRState.MERGED, tier=bronze_config)
    """

    _counter: int = 0

    def _next_number(self) -> int:
        self._counter += 1
        return self._counter

    def create(
        self,
        state: PRState,
        tier: TierConfig,
        number: Optional[int] = None,
        earned_score: float = 100.0,
        collateral_score: float = 20.0,
        repo: str = "test/repo",
    ) -> PullRequest:
        """Create a mock PullRequest with the given parameters."""
        if number is None:
            number = self._next_number()

        return PullRequest(
            number=number,
            repository_full_name=repo,
            uid=0,
            hotkey="test_hotkey",
            github_id="12345",
            title=f"Test PR #{number}",
            author_login="testuser",
            merged_at=datetime.now(timezone.utc) if state == PRState.MERGED else None,
            created_at=datetime.now(timezone.utc),
            pr_state=state,
            repository_tier_configuration=tier,
            earned_score=earned_score,
            collateral_score=collateral_score,
        )

    def merged(self, tier: TierConfig, **kwargs) -> PullRequest:
        """Create a merged PR."""
        return self.create(state=PRState.MERGED, tier=tier, **kwargs)

    def closed(self, tier: TierConfig, **kwargs) -> PullRequest:
        """Create a closed PR."""
        return self.create(state=PRState.CLOSED, tier=tier, **kwargs)

    def open(self, tier: TierConfig, **kwargs) -> PullRequest:
        """Create an open PR."""
        return self.create(state=PRState.OPEN, tier=tier, **kwargs)

    def merged_batch(self, tier: TierConfig, count: int, **kwargs) -> List[PullRequest]:
        """Create multiple merged PRs."""
        return [self.merged(tier=tier, **kwargs) for _ in range(count)]

    def closed_batch(self, tier: TierConfig, count: int, **kwargs) -> List[PullRequest]:
        """Create multiple closed PRs."""
        return [self.closed(tier=tier, **kwargs) for _ in range(count)]

    def open_batch(self, tier: TierConfig, count: int, **kwargs) -> List[PullRequest]:
        """Create multiple open PRs."""
        return [self.open(tier=tier, **kwargs) for _ in range(count)]

    def reset(self):
        """Reset the counter (useful between tests)."""
        self._counter = 0


@pytest.fixture
def pr_factory() -> PRBuilder:
    """
    Factory fixture for creating mock PRs.

    Usage:
        def test_something(pr_factory, bronze_config):
            merged_pr = pr_factory.merged(tier=bronze_config)
            closed_pr = pr_factory.closed(tier=bronze_config)

            # Create batches
            merged_prs = pr_factory.merged_batch(tier=bronze_config, count=5)
    """
    return PRBuilder()


# ============================================================================
# Pre-built Miner Scenario Fixtures
# ============================================================================


@dataclass
class MinerScenario:
    """
    Represents a miner's PR history for testing.

    Attributes:
        merged: List of merged PRs
        closed: List of closed PRs
        open: List of open PRs
        description: Human-readable description of this scenario
    """

    merged: List[PullRequest]
    closed: List[PullRequest]
    open: List[PullRequest]
    description: str = ""

    @property
    def all_prs(self) -> List[PullRequest]:
        return self.merged + self.closed + self.open


@pytest.fixture
def new_miner(pr_factory, bronze_config) -> MinerScenario:
    """Brand new miner with no PRs (no tiers unlocked)."""
    pr_factory.reset()
    return MinerScenario(merged=[], closed=[], open=[], description="New miner with no history")


@pytest.fixture
def bronze_miner(pr_factory, bronze_config) -> MinerScenario:
    """Miner with Bronze unlocked (meets requirements with 100% credibility)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    return MinerScenario(
        merged=pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges),
        closed=[],
        open=[],
        description=f"Bronze miner: {bronze_tier_config.required_merges} merged = 100% credibility",
    )


@pytest.fixture
def silver_unlocked_miner(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner who has unlocked Silver (Bronze and Silver requirements met)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges)
        ),
        closed=[],
        open=[],
        description="Silver miner: Bronze + Silver unlocked with 100% credibility",
    )


@pytest.fixture
def silver_threshold_miner(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner exactly at Silver credibility threshold."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    required_merges = silver_tier_config.required_merges
    required_credibility = silver_tier_config.required_credibility

    # Calculate closed to be exactly at threshold
    closed_count = int(required_merges * (1 - required_credibility) / required_credibility)

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges)
            + pr_factory.merged_batch(tier=silver_config, count=required_merges)
        ),
        closed=pr_factory.closed_batch(tier=silver_config, count=closed_count),
        open=[],
        description=f"Silver threshold: {required_merges} merged, {closed_count} closed = ~{required_credibility*100}%",
    )


@pytest.fixture
def gold_unlocked_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner who has unlocked Gold tier (all tiers unlocked)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges)
            + pr_factory.merged_batch(tier=gold_config, count=gold_tier_config.required_merges)
        ),
        closed=[],
        open=[],
        description="Gold miner: All tiers unlocked with 100% credibility",
    )


@pytest.fixture
def gold_threshold_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner exactly at Gold credibility threshold."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]
    required_merges = gold_tier_config.required_merges
    required_credibility = gold_tier_config.required_credibility

    # Calculate closed to be exactly at threshold
    closed_count = int(required_merges * (1 - required_credibility) / required_credibility)

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges)
            + pr_factory.merged_batch(tier=gold_config, count=required_merges)
        ),
        closed=pr_factory.closed_batch(tier=gold_config, count=closed_count),
        open=[],
        description=f"Gold threshold: {required_merges} merged, {closed_count} closed = ~{required_credibility*100}%",
    )


# ============================================================================
# Demotion Scenario Fixtures
# ============================================================================


@pytest.fixture
def demoted_from_gold_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner who was at Gold but got demoted (credibility dropped below requirement)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]
    gold_required = gold_tier_config.required_merges
    gold_cred_required = gold_tier_config.required_credibility

    # Calculate closed to drop below Gold credibility requirement
    closed_count = int(gold_required * (1 - gold_cred_required) / gold_cred_required) + 2

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges)
            + pr_factory.merged_batch(tier=gold_config, count=gold_required)
        ),
        closed=pr_factory.closed_batch(tier=gold_config, count=closed_count),
        open=[],
        description=f"Demoted from Gold: {gold_required}/{gold_required + closed_count} (below {gold_cred_required*100}% threshold)",
    )


@pytest.fixture
def demoted_from_silver_miner(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner who was at Silver but got demoted (credibility dropped below requirement)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    silver_required = silver_tier_config.required_merges
    silver_cred_required = silver_tier_config.required_credibility

    # Calculate closed to drop below Silver credibility requirement
    closed_count = int(silver_required * (1 - silver_cred_required) / silver_cred_required) + 2

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges)
            + pr_factory.merged_batch(tier=silver_config, count=silver_required)
        ),
        closed=pr_factory.closed_batch(tier=silver_config, count=closed_count),
        open=[],
        description=f"Demoted from Silver: {silver_required}/{silver_required + closed_count} (below {silver_cred_required*100}% threshold)",
    )


@pytest.fixture
def cascade_demoted_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner with perfect Gold stats but Silver is locked (cascade demotion)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges - 1)  # One short
            + pr_factory.merged_batch(tier=gold_config, count=gold_tier_config.required_merges + 5)  # Perfect Gold
        ),
        closed=[],
        open=[],
        description="Cascade demotion: Silver locked (1 merge short) -> Gold locked despite 100%",
    )


# ============================================================================
# Edge Case Fixtures
# ============================================================================


@pytest.fixture
def spammer_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner who spammed PRs that mostly got closed."""
    pr_factory.reset()
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=5)
            + pr_factory.merged_batch(tier=silver_config, count=5)
            + pr_factory.merged_batch(tier=gold_config, count=6)
        ),
        closed=(
            pr_factory.closed_batch(tier=bronze_config, count=20)
            + pr_factory.closed_batch(tier=silver_config, count=20)
            + pr_factory.closed_batch(tier=gold_config, count=20)
        ),
        open=[],
        description="Spammer: lots of closed PRs destroying credibility",
    )


@pytest.fixture
def perfect_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner with 100% credibility across all tiers."""
    pr_factory.reset()
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=5)
            + pr_factory.merged_batch(tier=silver_config, count=5)
            + pr_factory.merged_batch(tier=gold_config, count=10)
        ),
        closed=[],
        open=[],
        description="Perfect miner: 100% credibility everywhere",
    )


@pytest.fixture
def mixed_performance_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner with varying performance across tiers."""
    pr_factory.reset()
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=9)  # 90%
            + pr_factory.merged_batch(tier=silver_config, count=11)  # 55%
            + pr_factory.merged_batch(tier=gold_config, count=6)  # 60%
        ),
        closed=(
            pr_factory.closed_batch(tier=bronze_config, count=1)
            + pr_factory.closed_batch(tier=silver_config, count=9)
            + pr_factory.closed_batch(tier=gold_config, count=4)
        ),
        open=[],
        description="Mixed: Bronze 90%, Silver 55%, Gold 60% (locked)",
    )


@pytest.fixture
def miner_with_open_prs(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner with some open PRs (for collateral testing)."""
    pr_factory.reset()
    return MinerScenario(
        merged=pr_factory.merged_batch(tier=bronze_config, count=3),
        closed=pr_factory.closed_batch(tier=bronze_config, count=1),
        open=(pr_factory.open_batch(tier=bronze_config, count=2) + pr_factory.open_batch(tier=silver_config, count=3)),
        description="Miner with 5 open PRs (for collateral testing)",
    )


# ============================================================================
# TierStats Fixtures
# ============================================================================


@pytest.fixture
def empty_tier_stats() -> dict:
    """Empty TierStats for all tiers."""
    return {tier: TierStats() for tier in Tier}


def _unlocked_bronze_stats() -> TierStats:
    """Helper to create Bronze stats that meet unlock requirements."""
    bronze_config = TIERS[Tier.BRONZE]
    return TierStats(merged_count=bronze_config.required_merges, closed_count=0)


def _unlocked_silver_stats() -> TierStats:
    """Helper to create Silver stats that meet unlock requirements."""
    silver_config = TIERS[Tier.SILVER]
    return TierStats(merged_count=silver_config.required_merges, closed_count=0)


def _unlocked_gold_stats() -> TierStats:
    """Helper to create Gold stats that meet unlock requirements."""
    gold_config = TIERS[Tier.GOLD]
    return TierStats(merged_count=gold_config.required_merges, closed_count=0)


@pytest.fixture
def silver_unlocked_stats() -> dict:
    """TierStats where Silver is unlocked (Bronze must also be unlocked)."""
    return {
        Tier.BRONZE: _unlocked_bronze_stats(),
        Tier.SILVER: _unlocked_silver_stats(),
        Tier.GOLD: TierStats(),
    }


@pytest.fixture
def gold_unlocked_stats() -> dict:
    """TierStats where Gold is unlocked (Bronze and Silver must also be unlocked)."""
    return {
        Tier.BRONZE: _unlocked_bronze_stats(),
        Tier.SILVER: _unlocked_silver_stats(),
        Tier.GOLD: _unlocked_gold_stats(),
    }


@pytest.fixture
def gold_locked_stats() -> dict:
    """TierStats where Gold is locked (below credibility requirement)."""
    gold_config = TIERS[Tier.GOLD]
    required_merges = gold_config.required_merges
    required_credibility = gold_config.required_credibility

    # Calculate closed count to be just below credibility threshold
    closed_count = int(required_merges * (1 - required_credibility) / required_credibility) + 1

    return {
        Tier.BRONZE: _unlocked_bronze_stats(),
        Tier.SILVER: _unlocked_silver_stats(),
        Tier.GOLD: TierStats(merged_count=required_merges, closed_count=closed_count),
    }
