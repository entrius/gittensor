# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Pytest fixtures for validator tests.

This module provides reusable fixtures for testing tier credibility,
scoring, and other validator functionality.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

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

        # Create PRs with unique repos (for unique repo requirement testing):
        prs = pr_factory.merged_batch(tier=bronze_config, count=3, unique_repos=True)
    """

    _counter: int = 0
    _repo_counter: int = 0

    def _next_number(self) -> int:
        self._counter += 1
        return self._counter

    def _next_repo(self) -> str:
        self._repo_counter += 1
        return f'test/repo-{self._repo_counter}'

    def create(
        self,
        state: PRState,
        tier: TierConfig,
        number: Optional[int] = None,
        earned_score: float = 100.0,
        collateral_score: float = 20.0,
        repo: Optional[str] = None,
        unique_repo: bool = False,
        low_value_pr: bool = False,
    ) -> PullRequest:
        """Create a mock PullRequest with the given parameters.

        Args:
            unique_repo: If True, generates a unique repo name for this PR.
                         If False and repo is None, uses 'test/repo'.
            low_value_pr: If True, marks the PR as low-value (won't count toward
                          merge counts or unique repos for merged PRs).
        """
        if number is None:
            number = self._next_number()

        if repo is None:
            repo = self._next_repo() if unique_repo else 'test/repo'

        return PullRequest(
            number=number,
            repository_full_name=repo,
            uid=0,
            hotkey='test_hotkey',
            github_id='12345',
            title=f'Test PR #{number}',
            author_login='testuser',
            merged_at=datetime.now(timezone.utc) if state == PRState.MERGED else None,
            created_at=datetime.now(timezone.utc),
            pr_state=state,
            repository_tier_configuration=tier,
            earned_score=earned_score,
            collateral_score=collateral_score,
            low_value_pr=low_value_pr,
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

    def merged_batch(self, tier: TierConfig, count: int, unique_repos: bool = False, **kwargs) -> List[PullRequest]:
        """Create multiple merged PRs.

        Args:
            unique_repos: If True, each PR gets a unique repo name.
        """
        return [self.merged(tier=tier, unique_repo=unique_repos, **kwargs) for _ in range(count)]

    def closed_batch(self, tier: TierConfig, count: int, unique_repos: bool = False, **kwargs) -> List[PullRequest]:
        """Create multiple closed PRs.

        Args:
            unique_repos: If True, each PR gets a unique repo name.
        """
        return [self.closed(tier=tier, unique_repo=unique_repos, **kwargs) for _ in range(count)]

    def open_batch(self, tier: TierConfig, count: int, unique_repos: bool = False, **kwargs) -> List[PullRequest]:
        """Create multiple open PRs.

        Args:
            unique_repos: If True, each PR gets a unique repo name.
        """
        return [self.open(tier=tier, unique_repo=unique_repos, **kwargs) for _ in range(count)]

    def reset(self):
        """Reset the counters (useful between tests)."""
        self._counter = 0
        self._repo_counter = 0

    def create_without_tier(
        self,
        state: PRState,
        number: Optional[int] = None,
        repo: str = 'untracked/repo',
    ) -> PullRequest:
        """Create a PR without tier configuration (simulates untracked repo).

        These PRs should be completely ignored by tier calculations.
        """
        if number is None:
            number = self._next_number()

        return PullRequest(
            number=number,
            repository_full_name=repo,
            uid=0,
            hotkey='test_hotkey',
            github_id='12345',
            title=f'Untracked PR #{number}',
            author_login='testuser',
            merged_at=datetime.now(timezone.utc) if state == PRState.MERGED else None,
            created_at=datetime.now(timezone.utc),
            pr_state=state,
            repository_tier_configuration=None,  # No tier config!
        )


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
    description: str = ''

    @property
    def all_prs(self) -> List[PullRequest]:
        return self.merged + self.closed + self.open


@pytest.fixture
def new_miner(pr_factory, bronze_config) -> MinerScenario:
    """Brand new miner with no PRs (no tiers unlocked)."""
    pr_factory.reset()
    return MinerScenario(merged=[], closed=[], open=[], description='New miner with no history')


@pytest.fixture
def bronze_miner(pr_factory, bronze_config) -> MinerScenario:
    """Miner with Bronze unlocked (meets requirements with 100% credibility and unique repos)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    return MinerScenario(
        merged=pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges, unique_repos=True),
        closed=[],
        open=[],
        description=f'Bronze miner: {bronze_tier_config.required_merges} merged to unique repos = 100% credibility',
    )


@pytest.fixture
def silver_unlocked_miner(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner who has unlocked Silver (Bronze and Silver requirements met with unique repos)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges, unique_repos=True)
        ),
        closed=[],
        open=[],
        description='Silver miner: Bronze + Silver unlocked with 100% credibility and unique repos',
    )


@pytest.fixture
def silver_threshold_miner(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner exactly at Silver credibility threshold with unique repos."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    required_merges = silver_tier_config.required_merges
    required_credibility = silver_tier_config.required_credibility

    # Calculate closed to be exactly at threshold
    closed_count = int(required_merges * (1 - required_credibility) / required_credibility)

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=silver_config, count=required_merges, unique_repos=True)
        ),
        closed=pr_factory.closed_batch(tier=silver_config, count=closed_count, unique_repos=True),
        open=[],
        description=f'Silver threshold: {required_merges} merged, {closed_count} closed = ~{required_credibility * 100}%',
    )


@pytest.fixture
def gold_unlocked_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner who has unlocked Gold tier (all tiers unlocked with unique repos)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=gold_config, count=gold_tier_config.required_merges, unique_repos=True)
        ),
        closed=[],
        open=[],
        description='Gold miner: All tiers unlocked with 100% credibility and unique repos',
    )


@pytest.fixture
def gold_threshold_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner exactly at Gold credibility threshold with unique repos."""
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
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=gold_config, count=required_merges, unique_repos=True)
        ),
        closed=pr_factory.closed_batch(tier=gold_config, count=closed_count, unique_repos=True),
        open=[],
        description=f'Gold threshold: {required_merges} merged, {closed_count} closed = ~{required_credibility * 100}%',
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
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=silver_config, count=silver_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=gold_config, count=gold_required, unique_repos=True)
        ),
        closed=pr_factory.closed_batch(tier=gold_config, count=closed_count, unique_repos=True),
        open=[],
        description=f'Demoted from Gold: {gold_required}/{gold_required + closed_count} (below {gold_cred_required * 100}% threshold)',
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
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(tier=silver_config, count=silver_required, unique_repos=True)
        ),
        closed=pr_factory.closed_batch(tier=silver_config, count=closed_count, unique_repos=True),
        open=[],
        description=f'Demoted from Silver: {silver_required}/{silver_required + closed_count} (below {silver_cred_required * 100}% threshold)',
    )


@pytest.fixture
def cascade_demoted_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner with perfect Gold stats but Silver is locked (cascade demotion due to not enough merges)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_tier_config.required_merges, unique_repos=True)
            + pr_factory.merged_batch(
                tier=silver_config, count=silver_tier_config.required_merges - 1, unique_repos=True
            )  # One short
            + pr_factory.merged_batch(
                tier=gold_config, count=gold_tier_config.required_merges + 5, unique_repos=True
            )  # Perfect Gold
        ),
        closed=[],
        open=[],
        description='Cascade demotion: Silver locked (1 merge short) -> Gold locked despite 100%',
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
            pr_factory.merged_batch(tier=bronze_config, count=5, unique_repos=True)
            + pr_factory.merged_batch(tier=silver_config, count=5, unique_repos=True)
            + pr_factory.merged_batch(tier=gold_config, count=6, unique_repos=True)
        ),
        closed=(
            pr_factory.closed_batch(tier=bronze_config, count=20, unique_repos=True)
            + pr_factory.closed_batch(tier=silver_config, count=20, unique_repos=True)
            + pr_factory.closed_batch(tier=gold_config, count=20, unique_repos=True)
        ),
        open=[],
        description='Spammer: lots of closed PRs destroying credibility',
    )


@pytest.fixture
def perfect_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner with 100% credibility across all tiers and unique repos."""
    pr_factory.reset()
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=5, unique_repos=True)
            + pr_factory.merged_batch(tier=silver_config, count=5, unique_repos=True)
            + pr_factory.merged_batch(tier=gold_config, count=10, unique_repos=True)
        ),
        closed=[],
        open=[],
        description='Perfect miner: 100% credibility everywhere with unique repos',
    )


@pytest.fixture
def mixed_performance_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner with varying performance across tiers."""
    pr_factory.reset()
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=9, unique_repos=True)  # 90%
            + pr_factory.merged_batch(tier=silver_config, count=11, unique_repos=True)  # 55%
            + pr_factory.merged_batch(tier=gold_config, count=6, unique_repos=True)  # 60%
        ),
        closed=(
            pr_factory.closed_batch(tier=bronze_config, count=1, unique_repos=True)
            + pr_factory.closed_batch(tier=silver_config, count=9, unique_repos=True)
            + pr_factory.closed_batch(tier=gold_config, count=4, unique_repos=True)
        ),
        open=[],
        description='Mixed: Bronze 90%, Silver 55%, Gold 60% (locked)',
    )


@pytest.fixture
def miner_with_open_prs(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner with some open PRs (for collateral testing) with unique repos."""
    pr_factory.reset()
    return MinerScenario(
        merged=pr_factory.merged_batch(tier=bronze_config, count=3, unique_repos=True),
        closed=pr_factory.closed_batch(tier=bronze_config, count=1, unique_repos=True),
        open=(
            pr_factory.open_batch(tier=bronze_config, count=2, unique_repos=True)
            + pr_factory.open_batch(tier=silver_config, count=3, unique_repos=True)
        ),
        description='Miner with 5 open PRs (for collateral testing) with unique repos',
    )


# ============================================================================
# TierStats Fixtures
# ============================================================================


@pytest.fixture
def empty_tier_stats() -> dict:
    """Empty TierStats for all tiers."""
    return {tier: TierStats() for tier in Tier}


def _unlocked_bronze_stats() -> TierStats:
    """Helper to create Bronze stats that meet unlock requirements (including unique repos)."""
    bronze_config = TIERS[Tier.BRONZE]
    return TierStats(
        merged_count=bronze_config.required_merges,
        closed_count=0,
        unique_repo_contribution_count=bronze_config.required_unique_repos_merged_to,
    )


def _unlocked_silver_stats() -> TierStats:
    """Helper to create Silver stats that meet unlock requirements (including unique repos)."""
    silver_config = TIERS[Tier.SILVER]
    return TierStats(
        merged_count=silver_config.required_merges,
        closed_count=0,
        unique_repo_contribution_count=silver_config.required_unique_repos_merged_to,
    )


def _unlocked_gold_stats() -> TierStats:
    """Helper to create Gold stats that meet unlock requirements (including unique repos)."""
    gold_config = TIERS[Tier.GOLD]
    return TierStats(
        merged_count=gold_config.required_merges,
        closed_count=0,
        unique_repo_contribution_count=gold_config.required_unique_repos_merged_to,
    )


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
