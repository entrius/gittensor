# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Pytest fixtures for validator tests.

Provides reusable fixtures for testing credibility, eligibility,
scoring, and other validator functionality.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import pytest

from gittensor.classes import PRState, PullRequest

# ============================================================================
# PR Factory Fixture
# ============================================================================


@dataclass
class PRBuilder:
    """
    Builder for creating mock PullRequests with sensible defaults.

    Usage:
        pr = pr_factory.merged()
        pr = pr_factory.closed(number=5)
        pr = pr_factory.open()
        pr = pr_factory.merged(token_score=50.0)
        prs = pr_factory.merged_batch(count=5, unique_repos=True)
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
        number: Optional[int] = None,
        earned_score: float = 100.0,
        collateral_score: float = 20.0,
        repo: Optional[str] = None,
        unique_repo: bool = False,
        token_score: float = 10.0,
        uid: int = 0,
        merged_at: Optional[datetime] = None,
    ) -> PullRequest:
        """Create a mock PullRequest with the given parameters."""
        if number is None:
            number = self._next_number()

        if repo is None:
            repo = self._next_repo() if unique_repo else 'test/repo'

        if merged_at is None:
            merged_at = datetime.now(timezone.utc) if state == PRState.MERGED else None

        return PullRequest(
            number=number,
            repository_full_name=repo,
            uid=uid,
            hotkey=f'hotkey_{uid}',
            github_id=str(uid),
            title=f'Test PR #{number}',
            author_login=f'user_{uid}',
            merged_at=merged_at,
            created_at=datetime.now(timezone.utc),
            pr_state=state,
            earned_score=earned_score,
            collateral_score=collateral_score,
            token_score=token_score,
        )

    def merged(self, **kwargs) -> PullRequest:
        """Create a merged PR."""
        return self.create(state=PRState.MERGED, **kwargs)

    def closed(self, **kwargs) -> PullRequest:
        """Create a closed PR."""
        return self.create(state=PRState.CLOSED, **kwargs)

    def open(self, **kwargs) -> PullRequest:
        """Create an open PR."""
        return self.create(state=PRState.OPEN, **kwargs)

    def merged_batch(self, count: int, unique_repos: bool = False, **kwargs) -> List[PullRequest]:
        """Create multiple merged PRs."""
        return [self.merged(unique_repo=unique_repos, **kwargs) for _ in range(count)]

    def closed_batch(self, count: int, unique_repos: bool = False, **kwargs) -> List[PullRequest]:
        """Create multiple closed PRs."""
        return [self.closed(unique_repo=unique_repos, **kwargs) for _ in range(count)]

    def open_batch(self, count: int, unique_repos: bool = False, **kwargs) -> List[PullRequest]:
        """Create multiple open PRs."""
        return [self.open(unique_repo=unique_repos, **kwargs) for _ in range(count)]

    def reset(self):
        """Reset the counters (useful between tests)."""
        self._counter = 0
        self._repo_counter = 0


@pytest.fixture
def pr_factory() -> PRBuilder:
    """Factory fixture for creating mock PRs."""
    return PRBuilder()


# ============================================================================
# Pre-built Miner Scenario Fixtures
# ============================================================================


@dataclass
class MinerScenario:
    """Represents a miner's PR history for testing."""

    merged: List[PullRequest]
    closed: List[PullRequest]
    open: List[PullRequest]
    description: str = ''

    @property
    def all_prs(self) -> List[PullRequest]:
        return self.merged + self.closed + self.open


@pytest.fixture
def new_miner(pr_factory) -> MinerScenario:
    """Brand new miner with no PRs."""
    pr_factory.reset()
    return MinerScenario(merged=[], closed=[], open=[], description='New miner with no history')


@pytest.fixture
def eligible_miner(pr_factory) -> MinerScenario:
    """Miner who passes the eligibility gate (5+ valid PRs, 100% credibility)."""
    pr_factory.reset()
    return MinerScenario(
        merged=pr_factory.merged_batch(count=6, unique_repos=True, token_score=10.0),
        closed=[],
        open=[],
        description='Eligible miner: 6 valid merged PRs, 100% credibility',
    )


@pytest.fixture
def ineligible_low_prs(pr_factory) -> MinerScenario:
    """Miner with too few valid PRs (below MIN_VALID_MERGED_PRS)."""
    pr_factory.reset()
    return MinerScenario(
        merged=pr_factory.merged_batch(count=3, unique_repos=True, token_score=10.0),
        closed=[],
        open=[],
        description='Ineligible: only 3 valid merged PRs',
    )


@pytest.fixture
def ineligible_low_credibility(pr_factory) -> MinerScenario:
    """Miner with enough PRs but credibility below 75%."""
    pr_factory.reset()
    return MinerScenario(
        merged=pr_factory.merged_batch(count=5, unique_repos=True, token_score=10.0),
        closed=pr_factory.closed_batch(count=4, unique_repos=True),
        open=[],
        description='Ineligible: 5/9 = 55.6% credibility (after mulligan: 5/8 = 62.5%)',
    )


@pytest.fixture
def miner_with_mulligan(pr_factory) -> MinerScenario:
    """Miner who benefits from the mulligan (1 closed PR forgiven)."""
    pr_factory.reset()
    return MinerScenario(
        merged=pr_factory.merged_batch(count=5, unique_repos=True, token_score=10.0),
        closed=pr_factory.closed_batch(count=1, unique_repos=True),
        open=[],
        description='Miner with mulligan: 5/5 = 100% credibility (1 closed forgiven)',
    )


@pytest.fixture
def miner_with_open_prs(pr_factory) -> MinerScenario:
    """Miner with open PRs (for collateral testing)."""
    pr_factory.reset()
    return MinerScenario(
        merged=pr_factory.merged_batch(count=5, unique_repos=True, token_score=10.0),
        closed=[],
        open=pr_factory.open_batch(count=3, unique_repos=True),
        description='Miner with 3 open PRs',
    )
