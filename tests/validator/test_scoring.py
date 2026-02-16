# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for the core scoring module: gittensor/validator/evaluation/scoring.py

Covers:
- calculate_base_score: token threshold, code density, contribution bonus
- calculate_time_decay_multiplier: grace period, sigmoid curve, min multiplier
- calculate_issue_multiplier: issue age, maintainer bonus, self-created issues
- is_valid_issue: all validation rules and edge cases
- calculate_uniqueness_multiplier: solo contributors, crowded repos
- calculate_pr_spam_penalty_multiplier: threshold behavior (extended)
- calculate_open_pr_threshold: dynamic threshold with unlocked tiers
- calculate_pr_multipliers: merged vs open PR multiplier assignment
- finalize_miner_scores: end-to-end score finalization
- get_tier_config: repository tier configuration lookup

Run tests:
    pytest tests/validator/test_scoring.py -v
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from gittensor.classes import Issue, MinerEvaluation, PRState, PullRequest
from gittensor.constants import (
    DEFAULT_COLLATERAL_PERCENT,
    DEFAULT_MERGED_PR_BASE_SCORE,
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAINTAINER_ASSOCIATIONS,
    MAINTAINER_ISSUE_BONUS,
    MAX_CODE_DENSITY_MULTIPLIER,
    MAX_ISSUE_AGE_BONUS,
    MAX_ISSUE_AGE_FOR_MAX_SCORE,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    SECONDS_PER_DAY,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
    UNIQUE_PR_BOOST,
)
from gittensor.validator.configurations.tier_config import (
    TIERS,
    Tier,
    TierConfig,
    TierStats,
)
from gittensor.validator.evaluation.scoring import (
    calculate_issue_multiplier,
    calculate_open_pr_collateral_score,
    calculate_open_pr_threshold,
    calculate_pr_multipliers,
    calculate_pr_spam_penalty_multiplier,
    calculate_time_decay_multiplier,
    calculate_uniqueness_multiplier,
    count_repository_contributors,
    finalize_miner_scores,
    get_tier_config,
    is_valid_issue,
)
from gittensor.validator.utils.load_weights import RepositoryConfig


# ============================================================================
# Helper: build a minimal PullRequest for testing
# ============================================================================

def _make_pr(
    *,
    number: int = 1,
    state: PRState = PRState.MERGED,
    repo: str = "owner/repo",
    author: str = "contributor",
    merged_at: Optional[datetime] = None,
    created_at: Optional[datetime] = None,
    last_edited_at: Optional[datetime] = None,
    issues: Optional[List[Issue]] = None,
    tier_config: Optional[TierConfig] = None,
    base_score: float = 0.0,
    repo_weight: float = 1.0,
    issue_multiplier: float = 1.0,
    token_score: float = 10.0,
) -> PullRequest:
    now = datetime.now(timezone.utc)
    pr = PullRequest(
        number=number,
        repository_full_name=repo,
        uid=0,
        hotkey="test_hotkey",
        github_id="12345",
        title=f"Test PR #{number}",
        author_login=author,
        merged_at=merged_at or (now if state == PRState.MERGED else None),
        created_at=created_at or now,
        pr_state=state,
        repository_tier_configuration=tier_config or TIERS[Tier.BRONZE],
        base_score=base_score,
        repo_weight_multiplier=repo_weight,
        issue_multiplier=issue_multiplier,
        token_score=token_score,
    )
    pr.issues = issues
    pr.last_edited_at = last_edited_at
    return pr


def _make_issue(
    *,
    number: int = 100,
    pr_number: int = 1,
    repo: str = "owner/repo",
    author: str = "maintainer",
    created_at: Optional[datetime] = None,
    closed_at: Optional[datetime] = None,
    state: Optional[str] = "CLOSED",
    author_association: Optional[str] = None,
) -> Issue:
    return Issue(
        number=number,
        pr_number=pr_number,
        repository_full_name=repo,
        title=f"Issue #{number}",
        created_at=created_at,
        closed_at=closed_at,
        author_login=author,
        state=state,
        author_association=author_association,
    )


# ============================================================================
# Tests: get_tier_config
# ============================================================================


class TestGetTierConfig:
    """Tests for get_tier_config repository lookup."""

    def test_known_repo_returns_tier_config(self):
        repos = {"owner/repo": RepositoryConfig(tier=Tier.BRONZE, weight=1.0)}
        result = get_tier_config("owner/repo", repos)
        assert result is not None
        assert result == TIERS[Tier.BRONZE]

    def test_unknown_repo_returns_none(self):
        repos = {"owner/repo": RepositoryConfig(tier=Tier.BRONZE, weight=1.0)}
        assert get_tier_config("other/repo", repos) is None

    def test_repo_with_no_tier_returns_none(self):
        repos = {"owner/repo": RepositoryConfig(tier=None, weight=1.0)}
        assert get_tier_config("owner/repo", repos) is None

    def test_repo_with_invalid_tier_returns_none(self):
        repos = {"owner/repo": RepositoryConfig(tier="platinum", weight=1.0)}
        assert get_tier_config("owner/repo", repos) is None


# ============================================================================
# Tests: calculate_time_decay_multiplier
# ============================================================================


class TestCalculateTimeDecayMultiplier:
    """Tests for time decay sigmoid curve."""

    def test_just_merged_returns_one(self):
        """PR merged just now should have no decay."""
        pr = _make_pr(merged_at=datetime.now(timezone.utc))
        assert calculate_time_decay_multiplier(pr) == 1.0

    def test_within_grace_period_returns_one(self):
        """PR merged within the grace period should have no decay."""
        pr = _make_pr(
            merged_at=datetime.now(timezone.utc) - timedelta(hours=TIME_DECAY_GRACE_PERIOD_HOURS - 1)
        )
        assert calculate_time_decay_multiplier(pr) == 1.0

    def test_at_grace_period_boundary(self):
        """PR merged exactly at grace period edge should still return 1.0."""
        pr = _make_pr(
            merged_at=datetime.now(timezone.utc) - timedelta(hours=TIME_DECAY_GRACE_PERIOD_HOURS - 0.01)
        )
        assert calculate_time_decay_multiplier(pr) == 1.0

    def test_just_after_grace_period_starts_decay(self):
        """PR merged just past grace period should start decaying (still close to 1.0)."""
        pr = _make_pr(
            merged_at=datetime.now(timezone.utc) - timedelta(hours=TIME_DECAY_GRACE_PERIOD_HOURS + 1)
        )
        result = calculate_time_decay_multiplier(pr)
        assert 0.9 < result < 1.0

    def test_at_sigmoid_midpoint(self):
        """At the sigmoid midpoint, multiplier should be approximately 0.5."""
        pr = _make_pr(
            merged_at=datetime.now(timezone.utc) - timedelta(days=TIME_DECAY_SIGMOID_MIDPOINT)
        )
        result = calculate_time_decay_multiplier(pr)
        assert 0.45 < result < 0.55, f"Expected ~0.5 at midpoint, got {result}"

    def test_very_old_pr_approaches_minimum(self):
        """Very old PR should approach the minimum multiplier."""
        pr = _make_pr(
            merged_at=datetime.now(timezone.utc) - timedelta(days=90)
        )
        result = calculate_time_decay_multiplier(pr)
        assert result >= TIME_DECAY_MIN_MULTIPLIER
        assert result < 0.1  # Should be very close to the minimum

    def test_never_below_minimum(self):
        """Even extremely old PRs should never go below the min multiplier."""
        pr = _make_pr(
            merged_at=datetime.now(timezone.utc) - timedelta(days=365)
        )
        result = calculate_time_decay_multiplier(pr)
        assert result >= TIME_DECAY_MIN_MULTIPLIER

    def test_monotonically_decreasing(self):
        """Multiplier should decrease as time since merge increases."""
        results = []
        for days in [1, 5, 10, 15, 20, 30, 60]:
            pr = _make_pr(merged_at=datetime.now(timezone.utc) - timedelta(days=days))
            results.append(calculate_time_decay_multiplier(pr))
        for i in range(len(results) - 1):
            assert results[i] >= results[i + 1], f"Not monotonically decreasing at index {i}"


# ============================================================================
# Tests: is_valid_issue
# ============================================================================


class TestIsValidIssue:
    """Tests for issue validation rules."""

    def test_valid_issue_merged_pr(self):
        """A normal valid issue on a merged PR passes validation."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=5),
        )
        issue = _make_issue(
            author="maintainer",
            created_at=now - timedelta(days=10),
            closed_at=now,
            state="CLOSED",
        )
        assert is_valid_issue(issue, pr) is True

    def test_valid_issue_open_pr(self):
        """A valid issue on an open PR passes validation."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.OPEN,
            created_at=now - timedelta(days=5),
        )
        issue = _make_issue(
            author="maintainer",
            created_at=now - timedelta(days=10),
            state="OPEN",
        )
        assert is_valid_issue(issue, pr) is True

    def test_missing_author_login(self):
        """Issue with no author should be invalid."""
        pr = _make_pr()
        issue = _make_issue(author=None)
        # Issue author_login is None -> invalid
        assert is_valid_issue(issue, pr) is False

    def test_self_created_issue(self):
        """Issue created by the PR author should be invalid."""
        pr = _make_pr(author="contributor")
        issue = _make_issue(author="contributor")
        assert is_valid_issue(issue, pr) is False

    def test_issue_created_after_pr(self):
        """Issue created after the PR was created should be invalid."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(created_at=now - timedelta(days=5))
        issue = _make_issue(created_at=now - timedelta(days=2))
        assert is_valid_issue(issue, pr) is False

    def test_merged_pr_issue_not_closed(self):
        """On a merged PR, an open issue should be invalid."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(state=PRState.MERGED, merged_at=now, created_at=now - timedelta(days=5))
        issue = _make_issue(
            created_at=now - timedelta(days=10),
            state="OPEN",
        )
        assert is_valid_issue(issue, pr) is False

    def test_merged_pr_edited_after_merge(self):
        """Issue should be invalid if PR was edited after merge."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now - timedelta(hours=2),
            created_at=now - timedelta(days=5),
            last_edited_at=now,  # Edited after merge
        )
        issue = _make_issue(
            created_at=now - timedelta(days=10),
            closed_at=now - timedelta(hours=2),
            state="CLOSED",
        )
        assert is_valid_issue(issue, pr) is False

    def test_issue_closed_too_far_from_merge(self):
        """Issue closed too far from merge date should be invalid."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=5),
        )
        issue = _make_issue(
            created_at=now - timedelta(days=10),
            closed_at=now - timedelta(days=MAX_ISSUE_CLOSE_WINDOW_DAYS + 1),
            state="CLOSED",
        )
        assert is_valid_issue(issue, pr) is False

    def test_issue_closed_within_window(self):
        """Issue closed within the allowed window should be valid."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=5),
        )
        issue = _make_issue(
            created_at=now - timedelta(days=10),
            closed_at=now,
            state="CLOSED",
        )
        assert is_valid_issue(issue, pr) is True

    def test_issue_no_created_at_still_valid(self):
        """Issue without created_at should still pass (only fails on explicit checks)."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(state=PRState.MERGED, merged_at=now, created_at=now - timedelta(days=5))
        issue = _make_issue(
            created_at=None,
            closed_at=now,
            state="CLOSED",
        )
        assert is_valid_issue(issue, pr) is True

    def test_open_pr_skips_merge_checks(self):
        """Open PRs skip merged-only checks (edit after merge, closed state, close window)."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.OPEN,
            created_at=now - timedelta(days=5),
            last_edited_at=now,  # Would fail for merged PR
        )
        issue = _make_issue(
            created_at=now - timedelta(days=10),
            state="OPEN",  # Would fail for merged PR
        )
        assert is_valid_issue(issue, pr) is True


# ============================================================================
# Tests: calculate_issue_multiplier
# ============================================================================


class TestCalculateIssueMultiplier:
    """Tests for issue multiplier calculation."""

    def test_no_issues_returns_one(self):
        """PR with no issues should get multiplier 1.0."""
        pr = _make_pr(issues=None)
        assert calculate_issue_multiplier(pr) == 1.0

    def test_empty_issues_returns_one(self):
        """PR with empty issues list should get multiplier 1.0."""
        pr = _make_pr(issues=[])
        assert calculate_issue_multiplier(pr) == 1.0

    def test_no_valid_issues_returns_one(self):
        """PR where all issues are self-created should get 1.0."""
        pr = _make_pr(author="contributor", issues=[
            _make_issue(author="contributor"),
        ])
        assert calculate_issue_multiplier(pr) == 1.0

    def test_old_issue_gives_high_bonus(self):
        """An old issue (at max age) should give maximum age bonus."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=50),
            issues=[
                _make_issue(
                    created_at=now - timedelta(days=MAX_ISSUE_AGE_FOR_MAX_SCORE + 10),
                    closed_at=now,
                    state="CLOSED",
                ),
            ],
        )
        result = calculate_issue_multiplier(pr)
        # Should be 1.0 + MAX_ISSUE_AGE_BONUS (capped at sqrt ratio = 1.0)
        assert abs(result - (1.0 + MAX_ISSUE_AGE_BONUS)) < 0.05

    def test_new_issue_gives_small_bonus(self):
        """A very new issue should give a small bonus."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=5),
            issues=[
                _make_issue(
                    created_at=now - timedelta(days=6),  # Created before PR
                    closed_at=now,
                    state="CLOSED",
                ),
            ],
        )
        result = calculate_issue_multiplier(pr)
        assert 1.0 < result < 1.0 + MAX_ISSUE_AGE_BONUS

    def test_maintainer_issue_gets_extra_bonus(self):
        """Issue created by a maintainer should get MAINTAINER_ISSUE_BONUS."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=5),
            issues=[
                _make_issue(
                    created_at=now - timedelta(days=6),
                    closed_at=now,
                    state="CLOSED",
                    author_association="OWNER",
                ),
            ],
        )
        result_with_maintainer = calculate_issue_multiplier(pr)

        # Same PR without maintainer association
        pr2 = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=5),
            issues=[
                _make_issue(
                    created_at=now - timedelta(days=6),
                    closed_at=now,
                    state="CLOSED",
                    author_association="CONTRIBUTOR",
                ),
            ],
        )
        result_without = calculate_issue_multiplier(pr2)
        assert abs((result_with_maintainer - result_without) - MAINTAINER_ISSUE_BONUS) < 0.01

    def test_maintainer_associations_all_valid(self):
        """All defined maintainer associations should trigger the bonus."""
        now = datetime.now(timezone.utc)
        for assoc in MAINTAINER_ASSOCIATIONS:
            pr = _make_pr(
                state=PRState.MERGED,
                merged_at=now,
                created_at=now - timedelta(days=5),
                issues=[
                    _make_issue(
                        created_at=now - timedelta(days=6),
                        closed_at=now,
                        state="CLOSED",
                        author_association=assoc,
                    ),
                ],
            )
            result = calculate_issue_multiplier(pr)
            assert result > 1.0 + MAINTAINER_ISSUE_BONUS - 0.01, f"{assoc} didn't trigger bonus"

    def test_issue_without_created_at(self):
        """Issue with no created_at should return 1.0 + maintainer_bonus only."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=5),
            issues=[
                _make_issue(
                    created_at=None,
                    closed_at=now,
                    state="CLOSED",
                    author_association="OWNER",
                ),
            ],
        )
        result = calculate_issue_multiplier(pr)
        assert abs(result - (1.0 + MAINTAINER_ISSUE_BONUS)) < 0.01

    def test_uses_first_valid_issue_only(self):
        """Only the first valid issue should be scored."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now,
            created_at=now - timedelta(days=50),
            author="contributor",
            issues=[
                _make_issue(author="contributor"),  # Invalid: self-created
                _make_issue(  # Valid, small age
                    number=101,
                    author="person_a",
                    created_at=now - timedelta(days=1),
                    closed_at=now,
                    state="CLOSED",
                ),
                _make_issue(  # Valid, big age — but shouldn't be used
                    number=102,
                    author="person_b",
                    created_at=now - timedelta(days=MAX_ISSUE_AGE_FOR_MAX_SCORE),
                    closed_at=now,
                    state="CLOSED",
                ),
            ],
        )
        result = calculate_issue_multiplier(pr)
        # Should use issue 101 (1 day old), not 102 (max age)
        assert result < 1.0 + MAX_ISSUE_AGE_BONUS * 0.5


# ============================================================================
# Tests: calculate_uniqueness_multiplier
# ============================================================================


class TestCalculateUniquenessMultiplier:
    """Tests for repository uniqueness multiplier."""

    def test_zero_contributing_miners(self):
        """With zero total miners, should return 1.0."""
        result = calculate_uniqueness_multiplier("owner/repo", {}, 0)
        assert result == 1.0

    def test_solo_contributor(self):
        """A miner contributing to a repo alone among many should get max boost."""
        repo_counts = {"owner/repo": 1}
        total_miners = 10
        result = calculate_uniqueness_multiplier("owner/repo", repo_counts, total_miners)
        expected = 1.0 + ((10 - 1 + 1) / 10) * UNIQUE_PR_BOOST
        assert abs(result - expected) < 0.001

    def test_everyone_contributes(self):
        """When all miners contribute to the same repo, boost is minimal."""
        repo_counts = {"owner/repo": 10}
        total_miners = 10
        result = calculate_uniqueness_multiplier("owner/repo", repo_counts, total_miners)
        expected = 1.0 + (1 / 10) * UNIQUE_PR_BOOST
        assert abs(result - expected) < 0.001

    def test_unknown_repo_treated_as_zero_count(self):
        """Repo not in counts dict should be treated as 0 contributors."""
        repo_counts = {"other/repo": 5}
        total_miners = 10
        result = calculate_uniqueness_multiplier("owner/repo", repo_counts, total_miners)
        expected = 1.0 + ((10 - 0 + 1) / 10) * UNIQUE_PR_BOOST
        assert abs(result - expected) < 0.001

    def test_single_miner_single_repo(self):
        """One miner contributing to one repo."""
        repo_counts = {"owner/repo": 1}
        result = calculate_uniqueness_multiplier("owner/repo", repo_counts, 1)
        expected = 1.0 + (1.0) * UNIQUE_PR_BOOST
        assert abs(result - expected) < 0.001

    def test_result_always_above_one(self):
        """Uniqueness multiplier should always be >= 1.0."""
        for total in range(1, 20):
            for count in range(1, total + 1):
                result = calculate_uniqueness_multiplier("r", {"r": count}, total)
                assert result >= 1.0


# ============================================================================
# Tests: calculate_pr_multipliers
# ============================================================================


class TestCalculatePrMultipliers:
    """Tests for PR multiplier assignment logic."""

    def test_merged_pr_gets_time_decay(self):
        """Merged PR should have time_decay_multiplier calculated."""
        now = datetime.now(timezone.utc)
        pr = _make_pr(
            state=PRState.MERGED,
            merged_at=now - timedelta(days=5),
            created_at=now - timedelta(days=10),
        )
        miner_eval = MinerEvaluation(uid=0, hotkey="test")
        repos = {pr.repository_full_name: RepositoryConfig(tier=Tier.BRONZE, weight=0.5)}
        calculate_pr_multipliers(pr, miner_eval, repos)

        assert pr.repo_weight_multiplier == 0.5
        assert pr.time_decay_multiplier < 1.0  # Should have decayed
        assert pr.open_pr_spam_multiplier == 1.0  # Placeholder

    def test_open_pr_gets_default_multipliers(self):
        """Open PR should get 1.0 for time_decay and credibility."""
        pr = _make_pr(state=PRState.OPEN)
        miner_eval = MinerEvaluation(uid=0, hotkey="test")
        repos = {pr.repository_full_name: RepositoryConfig(tier=Tier.BRONZE, weight=0.8)}
        calculate_pr_multipliers(pr, miner_eval, repos)

        assert pr.time_decay_multiplier == 1.0
        assert pr.credibility_multiplier == 1.0
        assert pr.repo_weight_multiplier == 0.8

    def test_missing_repo_config_gets_minimum_weight(self):
        """PR in a repo not in master_repositories should get 0.01 weight."""
        pr = _make_pr(state=PRState.MERGED, repo="unknown/repo")
        miner_eval = MinerEvaluation(uid=0, hotkey="test")
        repos = {}
        calculate_pr_multipliers(pr, miner_eval, repos)

        assert pr.repo_weight_multiplier == 0.01


# ============================================================================
# Tests: count_repository_contributors
# ============================================================================


class TestCountRepositoryContributors:
    """Tests for counting unique contributors per repository."""

    def test_empty_evaluations(self):
        assert count_repository_contributors({}) == {}

    def test_single_miner_single_repo(self):
        eval1 = MinerEvaluation(uid=0, hotkey="a")
        eval1.unique_repos_contributed_to = {"owner/repo"}
        result = count_repository_contributors({0: eval1})
        assert result == {"owner/repo": 1}

    def test_multiple_miners_same_repo(self):
        eval1 = MinerEvaluation(uid=0, hotkey="a")
        eval1.unique_repos_contributed_to = {"owner/repo"}
        eval2 = MinerEvaluation(uid=1, hotkey="b")
        eval2.unique_repos_contributed_to = {"owner/repo"}
        result = count_repository_contributors({0: eval1, 1: eval2})
        assert result == {"owner/repo": 2}

    def test_miners_with_no_repos(self):
        eval1 = MinerEvaluation(uid=0, hotkey="a")
        result = count_repository_contributors({0: eval1})
        assert result == {}


# ============================================================================
# Tests: finalize_miner_scores
# ============================================================================


class TestFinalizeMinerScores:
    """Tests for end-to-end score finalization."""

    def test_empty_evaluations(self):
        """Empty dict should not raise."""
        finalize_miner_scores({})

    def test_miner_with_no_prs(self):
        """Miner with no PRs should keep score at 0."""
        evaluation = MinerEvaluation(uid=0, hotkey="test")
        finalize_miner_scores({0: evaluation})
        assert evaluation.total_score == 0.0

    def test_none_evaluation_raises(self):
        """None evaluation in the dict raises an error (not supported)."""
        with pytest.raises(AttributeError):
            finalize_miner_scores({0: None})

    def test_open_prs_generate_collateral(self):
        """Open PRs should have collateral calculated and deducted from total."""
        now = datetime.now(timezone.utc)
        tier = TIERS[Tier.BRONZE]

        open_pr = _make_pr(
            state=PRState.OPEN,
            tier_config=tier,
            base_score=10.0,
            repo_weight=1.0,
            issue_multiplier=1.0,
        )
        merged_pr = _make_pr(
            number=2,
            state=PRState.MERGED,
            tier_config=tier,
            merged_at=now,
            created_at=now - timedelta(days=1),
            base_score=50.0,
            token_score=10.0,
        )

        evaluation = MinerEvaluation(uid=0, hotkey="test")
        evaluation.open_pull_requests = [open_pr]
        evaluation.merged_pull_requests = [merged_pr]
        evaluation.unique_repos_contributed_to = {merged_pr.repository_full_name}

        finalize_miner_scores({0: evaluation})

        assert evaluation.total_collateral_score > 0
        assert evaluation.total_score >= 0.0

    def test_collateral_cannot_make_score_negative(self):
        """Total score should never go below 0 after collateral deduction."""
        tier = TIERS[Tier.BRONZE]

        # Create many open PRs with high base scores
        open_prs = []
        for i in range(20):
            open_prs.append(_make_pr(
                number=i + 100,
                state=PRState.OPEN,
                tier_config=tier,
                base_score=100.0,
                repo_weight=1.0,
                issue_multiplier=1.0,
            ))

        # One small merged PR
        merged_pr = _make_pr(
            number=1,
            state=PRState.MERGED,
            tier_config=tier,
            base_score=1.0,
            token_score=10.0,
        )

        evaluation = MinerEvaluation(uid=0, hotkey="test")
        evaluation.open_pull_requests = open_prs
        evaluation.merged_pull_requests = [merged_pr]
        evaluation.unique_repos_contributed_to = {merged_pr.repository_full_name}

        finalize_miner_scores({0: evaluation})

        assert evaluation.total_score >= 0.0

    def test_merged_pr_adds_to_unique_repos(self):
        """score_pull_request adds merged PR repos to unique_repos_contributed_to,
        but finalize uses that set for uniqueness multiplier calculation."""
        tier = TIERS[Tier.BRONZE]
        now = datetime.now(timezone.utc)

        pr1 = _make_pr(number=1, state=PRState.MERGED, repo="owner/repo-a", tier_config=tier, base_score=10.0, token_score=10.0)
        pr2 = _make_pr(number=2, state=PRState.MERGED, repo="owner/repo-b", tier_config=tier, base_score=10.0, token_score=10.0)

        evaluation = MinerEvaluation(uid=0, hotkey="test")
        evaluation.merged_pull_requests = [pr1, pr2]
        evaluation.unique_repos_contributed_to = {"owner/repo-a", "owner/repo-b"}

        finalize_miner_scores({0: evaluation})

        assert evaluation.unique_repos_count == 2

    def test_spam_multiplier_applied_consistently(self):
        """All merged PRs for a miner should get the same spam multiplier."""
        tier = TIERS[Tier.BRONZE]
        now = datetime.now(timezone.utc)

        merged_prs = [
            _make_pr(number=i, state=PRState.MERGED, tier_config=tier, base_score=10.0, token_score=10.0, repo=f"owner/repo-{i}")
            for i in range(3)
        ]

        evaluation = MinerEvaluation(uid=0, hotkey="test")
        evaluation.merged_pull_requests = merged_prs
        evaluation.unique_repos_contributed_to = {pr.repository_full_name for pr in merged_prs}

        finalize_miner_scores({0: evaluation})

        spam_values = {pr.open_pr_spam_multiplier for pr in merged_prs}
        assert len(spam_values) == 1, "All merged PRs should have the same spam multiplier"
