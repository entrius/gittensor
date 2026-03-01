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
        token_score: Optional[float] = None,  # Auto-calculated from tier if None
        merged_at: Optional[datetime] = None,
        uid: int = 0,
    ) -> PullRequest:
        """Create a mock PullRequest with the given parameters.

        Args:
            unique_repo: If True, generates a unique repo name for this PR.
                         If False and repo is None, uses 'test/repo'.
            token_score: Token score for this PR. If None, auto-calculates based on tier
                         requirements to ensure the PR qualifies.
            merged_at: Explicit merge timestamp. If None and state is MERGED, uses now().
            uid: Miner UID for this PR (default 0).
        """
        # Auto-calculate token score if not specified - ensure it meets tier requirements
        if token_score is None:
            required_repos = tier.required_unique_repos_count or 3
            min_per_repo = tier.required_min_token_score_per_repo or 5.0
            min_total = tier.required_min_token_score or 0.0
            # Each PR should contribute enough to meet both per-repo and total requirements
            token_score = max(min_per_repo, min_total / required_repos) + 1.0
        if number is None:
            number = self._next_number()

        if repo is None:
            repo = self._next_repo() if unique_repo else 'test/repo'

        if merged_at is None and state == PRState.MERGED:
            merged_at = datetime.now(timezone.utc)

        return PullRequest(
            number=number,
            repository_full_name=repo,
            uid=uid,
            hotkey='test_hotkey',
            github_id='12345',
            title=f'Test PR #{number}',
            author_login='testuser',
            merged_at=merged_at,
            created_at=datetime.now(timezone.utc),
            pr_state=state,
            repository_tier_configuration=tier,
            earned_score=earned_score,
            collateral_score=collateral_score,
            token_score=token_score,
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
    """Miner with Bronze unlocked (meets requirements with 100% credibility and qualified unique repos)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    required_repos = bronze_tier_config.required_unique_repos_count or 3
    return MinerScenario(
        merged=pr_factory.merged_batch(tier=bronze_config, count=required_repos, unique_repos=True),
        closed=[],
        open=[],
        description=f'Bronze miner: {required_repos} merged to unique repos = 100% credibility',
    )


@pytest.fixture
def silver_unlocked_miner(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner who has unlocked Silver (Bronze and Silver requirements met with qualified unique repos)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    bronze_repos = bronze_tier_config.required_unique_repos_count or 3
    silver_repos = silver_tier_config.required_unique_repos_count or 3
    # Ensure enough token score per PR to meet Silver's total token score requirement
    silver_token_per_pr = (silver_tier_config.required_min_token_score or 50.0) / silver_repos + 1.0
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_repos, unique_repos=True)
            + pr_factory.merged_batch(
                tier=silver_config, count=silver_repos, unique_repos=True, token_score=silver_token_per_pr
            )
        ),
        closed=[],
        open=[],
        description='Silver miner: Bronze + Silver unlocked with 100% credibility and qualified repos',
    )


@pytest.fixture
def silver_threshold_miner(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner exactly at Silver credibility threshold with qualified repos."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    bronze_repos = bronze_tier_config.required_unique_repos_count or 3
    silver_repos = silver_tier_config.required_unique_repos_count or 3
    required_credibility = silver_tier_config.required_credibility

    # Calculate closed to be exactly at threshold
    closed_count = int(silver_repos * (1 - required_credibility) / required_credibility)
    # Ensure enough token score per PR to meet Silver's total token score requirement
    silver_token_per_pr = (silver_tier_config.required_min_token_score or 50.0) / silver_repos + 1.0

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_repos, unique_repos=True)
            + pr_factory.merged_batch(
                tier=silver_config, count=silver_repos, unique_repos=True, token_score=silver_token_per_pr
            )
        ),
        closed=pr_factory.closed_batch(tier=silver_config, count=closed_count, unique_repos=True),
        open=[],
        description=f'Silver threshold: {silver_repos} merged, {closed_count} closed = ~{required_credibility * 100}%',
    )


@pytest.fixture
def gold_unlocked_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner who has unlocked Gold tier (all tiers unlocked with qualified repos)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]
    bronze_repos = bronze_tier_config.required_unique_repos_count or 3
    silver_repos = silver_tier_config.required_unique_repos_count or 3
    gold_repos = gold_tier_config.required_unique_repos_count or 3
    # Ensure enough token score per PR to meet each tier's requirements
    silver_token_per_pr = (silver_tier_config.required_min_token_score or 50.0) / silver_repos + 1.0
    gold_token_per_pr = (gold_tier_config.required_min_token_score or 150.0) / gold_repos + 1.0
    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_repos, unique_repos=True)
            + pr_factory.merged_batch(
                tier=silver_config, count=silver_repos, unique_repos=True, token_score=silver_token_per_pr
            )
            + pr_factory.merged_batch(
                tier=gold_config, count=gold_repos, unique_repos=True, token_score=gold_token_per_pr
            )
        ),
        closed=[],
        open=[],
        description='Gold miner: All tiers unlocked with 100% credibility and qualified repos',
    )


@pytest.fixture
def gold_threshold_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner exactly at Gold credibility threshold with qualified repos."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]
    bronze_repos = bronze_tier_config.required_unique_repos_count or 3
    silver_repos = silver_tier_config.required_unique_repos_count or 3
    gold_repos = gold_tier_config.required_unique_repos_count or 3
    required_credibility = gold_tier_config.required_credibility

    # Calculate closed to be exactly at threshold
    closed_count = int(gold_repos * (1 - required_credibility) / required_credibility)
    # Ensure enough token score per PR to meet each tier's requirements
    silver_token_per_pr = (silver_tier_config.required_min_token_score or 50.0) / silver_repos + 1.0
    gold_token_per_pr = (gold_tier_config.required_min_token_score or 150.0) / gold_repos + 1.0

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_repos, unique_repos=True)
            + pr_factory.merged_batch(
                tier=silver_config, count=silver_repos, unique_repos=True, token_score=silver_token_per_pr
            )
            + pr_factory.merged_batch(
                tier=gold_config, count=gold_repos, unique_repos=True, token_score=gold_token_per_pr
            )
        ),
        closed=pr_factory.closed_batch(tier=gold_config, count=closed_count, unique_repos=True),
        open=[],
        description=f'Gold threshold: {gold_repos} merged, {closed_count} closed = ~{required_credibility * 100}%',
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
    bronze_repos = bronze_tier_config.required_unique_repos_count or 3
    silver_repos = silver_tier_config.required_unique_repos_count or 3
    gold_repos = gold_tier_config.required_unique_repos_count or 3
    gold_cred_required = gold_tier_config.required_credibility

    # Calculate closed to drop below Gold credibility requirement
    closed_count = int(gold_repos * (1 - gold_cred_required) / gold_cred_required) + 2
    # Ensure enough token score per PR to meet each tier's requirements
    silver_token_per_pr = (silver_tier_config.required_min_token_score or 50.0) / silver_repos + 1.0
    gold_token_per_pr = (gold_tier_config.required_min_token_score or 150.0) / gold_repos + 1.0

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_repos, unique_repos=True)
            + pr_factory.merged_batch(
                tier=silver_config, count=silver_repos, unique_repos=True, token_score=silver_token_per_pr
            )
            + pr_factory.merged_batch(
                tier=gold_config, count=gold_repos, unique_repos=True, token_score=gold_token_per_pr
            )
        ),
        closed=pr_factory.closed_batch(tier=gold_config, count=closed_count, unique_repos=True),
        open=[],
        description=f'Demoted from Gold: {gold_repos}/{gold_repos + closed_count} (below {gold_cred_required * 100}% threshold)',
    )


@pytest.fixture
def demoted_from_silver_miner(pr_factory, bronze_config, silver_config) -> MinerScenario:
    """Miner who was at Silver but got demoted (credibility dropped below requirement)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    bronze_repos = bronze_tier_config.required_unique_repos_count or 3
    silver_repos = silver_tier_config.required_unique_repos_count or 3
    silver_cred_required = silver_tier_config.required_credibility

    # Calculate closed to drop below Silver credibility requirement
    closed_count = int(silver_repos * (1 - silver_cred_required) / silver_cred_required) + 2
    # Ensure enough token score per PR to meet Silver's requirements
    silver_token_per_pr = (silver_tier_config.required_min_token_score or 50.0) / silver_repos + 1.0

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_repos, unique_repos=True)
            + pr_factory.merged_batch(
                tier=silver_config, count=silver_repos, unique_repos=True, token_score=silver_token_per_pr
            )
        ),
        closed=pr_factory.closed_batch(tier=silver_config, count=closed_count, unique_repos=True),
        open=[],
        description=f'Demoted from Silver: {silver_repos}/{silver_repos + closed_count} (below {silver_cred_required * 100}% threshold)',
    )


@pytest.fixture
def cascade_demoted_miner(pr_factory, bronze_config, silver_config, gold_config) -> MinerScenario:
    """Miner with perfect Gold stats but Silver is locked (cascade demotion due to not enough qualified repos)."""
    pr_factory.reset()
    bronze_tier_config = TIERS[Tier.BRONZE]
    silver_tier_config = TIERS[Tier.SILVER]
    gold_tier_config = TIERS[Tier.GOLD]
    bronze_repos = bronze_tier_config.required_unique_repos_count or 3
    silver_repos = silver_tier_config.required_unique_repos_count or 3
    gold_repos = gold_tier_config.required_unique_repos_count or 3
    gold_token_per_pr = (gold_tier_config.required_min_token_score or 150.0) / gold_repos + 1.0
    # Silver has low token scores - below the per-repo requirement
    silver_low_token = (silver_tier_config.required_min_token_score_per_repo or 10.0) - 5.0

    return MinerScenario(
        merged=(
            pr_factory.merged_batch(tier=bronze_config, count=bronze_repos, unique_repos=True)
            + pr_factory.merged_batch(
                tier=silver_config, count=silver_repos, unique_repos=True, token_score=silver_low_token
            )  # Low token score - doesn't qualify
            + pr_factory.merged_batch(
                tier=gold_config, count=gold_repos + 5, unique_repos=True, token_score=gold_token_per_pr
            )  # Perfect Gold
        ),
        closed=[],
        open=[],
        description='Cascade demotion: Silver locked (low token score repos) -> Gold locked despite 100%',
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
    """Helper to create Bronze stats that meet unlock requirements (including qualified repos)."""
    bronze_config = TIERS[Tier.BRONZE]
    required_repos = bronze_config.required_unique_repos_count or 3
    token_per_repo = bronze_config.required_min_token_score_per_repo or 5.0
    total_token_score = required_repos * token_per_repo  # Enough token score to meet requirements
    return TierStats(
        merged_count=required_repos,
        closed_count=0,
        unique_repo_contribution_count=required_repos,
        qualified_unique_repo_count=required_repos,
        token_score=total_token_score,
    )


def _unlocked_silver_stats() -> TierStats:
    """Helper to create Silver stats that meet unlock requirements (including qualified repos)."""
    silver_config = TIERS[Tier.SILVER]
    required_repos = silver_config.required_unique_repos_count or 3
    token_per_repo = silver_config.required_min_token_score_per_repo or 10.0
    # Silver requires 50.0 total token score, so we ensure that's met
    min_total = silver_config.required_min_token_score or 50.0
    total_token_score = max(required_repos * token_per_repo, min_total)
    return TierStats(
        merged_count=required_repos,
        closed_count=0,
        unique_repo_contribution_count=required_repos,
        qualified_unique_repo_count=required_repos,
        token_score=total_token_score,
    )


def _unlocked_gold_stats() -> TierStats:
    """Helper to create Gold stats that meet unlock requirements (including qualified repos)."""
    gold_config = TIERS[Tier.GOLD]
    required_repos = gold_config.required_unique_repos_count or 3
    token_per_repo = gold_config.required_min_token_score_per_repo or 25.0
    # Gold requires 150.0 total token score, so we ensure that's met
    min_total = gold_config.required_min_token_score or 150.0
    total_token_score = max(required_repos * token_per_repo, min_total)
    return TierStats(
        merged_count=required_repos,
        closed_count=0,
        unique_repo_contribution_count=required_repos,
        qualified_unique_repo_count=required_repos,
        token_score=total_token_score,
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
    required_repos = gold_config.required_unique_repos_count or 3
    required_credibility = gold_config.required_credibility

    # Calculate closed count to be just below credibility threshold
    closed_count = int(required_repos * (1 - required_credibility) / required_credibility) + 1

    return {
        Tier.BRONZE: _unlocked_bronze_stats(),
        Tier.SILVER: _unlocked_silver_stats(),
        Tier.GOLD: TierStats(
            merged_count=required_repos,
            closed_count=closed_count,
            unique_repo_contribution_count=required_repos,
            qualified_unique_repo_count=required_repos,
            token_score=gold_config.required_min_token_score or 150.0,
        ),
    }
