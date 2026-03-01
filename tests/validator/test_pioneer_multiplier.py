# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for binary pioneer reward mechanism."""

from datetime import datetime, timedelta, timezone

import pytest

from gittensor.classes import MinerEvaluation, PRState
from gittensor.constants import (
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    PIONEER_BASE_BONUS,
    PIONEER_MULTI_REPO_DAMPING,
)
from gittensor.validator.configurations.tier_config import TIERS, Tier
from gittensor.validator.evaluation.scoring import (
    calculate_pioneer_multiplier,
    calculate_pioneer_ranks,
    count_pioneered_repos,
    finalize_miner_scores,
)
from tests.validator.conftest import PRBuilder

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def builder():
    return PRBuilder()


@pytest.fixture
def bronze():
    return TIERS[Tier.BRONZE]


# ============================================================================
# TestCalculatePioneerMultiplier
# ============================================================================


class TestCalculatePioneerMultiplier:
    """Tests for binary calculate_pioneer_multiplier formula."""

    def test_rank_one_gets_full_bonus(self):
        assert calculate_pioneer_multiplier(1) == pytest.approx(1.0 + PIONEER_BASE_BONUS)

    def test_non_pioneer_gets_no_bonus(self):
        assert calculate_pioneer_multiplier(0) == 1.0
        assert calculate_pioneer_multiplier(2) == 1.0
        assert calculate_pioneer_multiplier(10) == 1.0

    def test_multi_repo_damping_reduces_pioneer_bonus(self):
        """Pioneering 4 repos yields less per-repo bonus than pioneering 1."""
        single = calculate_pioneer_multiplier(1, pioneered_repo_count=1)
        four = calculate_pioneer_multiplier(1, pioneered_repo_count=4)
        assert four < single
        assert four > 1.0

    def test_multi_repo_damping_formula(self):
        """Verify exact damping formula: bonus / pioneered_count^DAMPING."""
        expected = 1.0 + PIONEER_BASE_BONUS / 4**PIONEER_MULTI_REPO_DAMPING
        assert calculate_pioneer_multiplier(1, pioneered_repo_count=4) == pytest.approx(expected)


# ============================================================================
# TestCalculatePioneerRanks
# ============================================================================


class TestCalculatePioneerRanks:
    """Tests for calculate_pioneer_ranks across miner evaluations."""

    def test_single_miner_is_pioneer(self, builder, bronze):
        now = datetime.now(timezone.utc)
        pr = builder.create(state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1, merged_at=now)
        ranks, prs = calculate_pioneer_ranks({1: MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[pr])})
        assert ranks == {'org/repo-a': {1: 1}}
        assert prs == {'org/repo-a': {1: pr.number}}

    def test_earliest_merge_gets_rank_one(self, builder, bronze):
        now = datetime.now(timezone.utc)
        pr1 = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1, merged_at=now - timedelta(days=10),
        )
        pr2 = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=2, merged_at=now - timedelta(days=5),
        )
        evals = {
            1: MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[pr1]),
            2: MinerEvaluation(uid=2, hotkey='hk2', merged_pull_requests=[pr2]),
        }
        ranks, _ = calculate_pioneer_ranks(evals)
        assert ranks['org/repo-a'][1] == 1
        assert ranks['org/repo-a'][2] == 2

    def test_same_timestamp_creates_co_pioneers(self, builder, bronze):
        now = datetime.now(timezone.utc)
        pr1 = builder.create(state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1, merged_at=now)
        pr2 = builder.create(state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=2, merged_at=now)
        evals = {
            1: MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[pr1]),
            2: MinerEvaluation(uid=2, hotkey='hk2', merged_pull_requests=[pr2]),
        }
        ranks, _ = calculate_pioneer_ranks(evals)
        assert ranks['org/repo-a'][1] == 1
        assert ranks['org/repo-a'][2] == 1

    def test_repos_ranked_independently(self, builder, bronze):
        now = datetime.now(timezone.utc)
        pr1a = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1, merged_at=now - timedelta(days=10),
        )
        pr2a = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=2, merged_at=now - timedelta(days=5),
        )
        pr1b = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-b', uid=1, merged_at=now - timedelta(days=3),
        )
        pr2b = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-b', uid=2, merged_at=now - timedelta(days=8),
        )
        evals = {
            1: MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[pr1a, pr1b]),
            2: MinerEvaluation(uid=2, hotkey='hk2', merged_pull_requests=[pr2a, pr2b]),
        }
        ranks, _ = calculate_pioneer_ranks(evals)
        assert ranks['org/repo-a'][1] == 1  # UID 1 pioneered repo-a
        assert ranks['org/repo-b'][2] == 1  # UID 2 pioneered repo-b

    def test_multiple_prs_same_repo_uses_earliest(self, builder, bronze):
        now = datetime.now(timezone.utc)
        early = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1, merged_at=now - timedelta(days=20),
        )
        late = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1, merged_at=now - timedelta(days=5),
        )
        evals = {1: MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[early, late])}
        _, prs = calculate_pioneer_ranks(evals)
        assert prs['org/repo-a'][1] == early.number

    def test_empty_evaluations(self):
        ranks, prs = calculate_pioneer_ranks({})
        assert ranks == {}
        assert prs == {}


# ============================================================================
# TestQualityGate
# ============================================================================


class TestQualityGate:
    """Quality gate: low token_score PRs can't capture pioneer rank."""

    def test_low_token_score_excluded(self, builder, bronze):
        now = datetime.now(timezone.utc)
        low = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1,
            merged_at=now - timedelta(days=10), token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
        )
        good = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=2, merged_at=now - timedelta(days=5),
        )
        evals = {
            1: MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[low]),
            2: MinerEvaluation(uid=2, hotkey='hk2', merged_pull_requests=[good]),
        }
        ranks, _ = calculate_pioneer_ranks(evals)
        assert 1 not in ranks['org/repo-a']
        assert ranks['org/repo-a'][2] == 1

    def test_no_tier_config_excluded(self, builder, bronze):
        now = datetime.now(timezone.utc)
        untracked = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1, merged_at=now - timedelta(days=10),
        )
        untracked.repository_tier_configuration = None
        good = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=2, merged_at=now - timedelta(days=5),
        )
        evals = {
            1: MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[untracked]),
            2: MinerEvaluation(uid=2, hotkey='hk2', merged_pull_requests=[good]),
        }
        ranks, _ = calculate_pioneer_ranks(evals)
        assert 1 not in ranks['org/repo-a']
        assert ranks['org/repo-a'][2] == 1


# ============================================================================
# TestFinalization
# ============================================================================


class TestFinalization:
    """Integration tests: pioneer multiplier through full finalization pipeline."""

    def test_pioneer_gets_bonus_follower_gets_nothing(self, builder, bronze):
        """Binary: pioneer gets full bonus, follower gets exactly 1.0."""
        now = datetime.now(timezone.utc)
        pr1 = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1,
            merged_at=now - timedelta(days=5), earned_score=0.0, collateral_score=0.0,
        )
        pr2 = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=2,
            merged_at=now, earned_score=0.0, collateral_score=0.0,
        )
        pr1.base_score = 30.0
        pr2.base_score = 30.0
        eval1 = MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[pr1])
        eval1.unique_repos_contributed_to.add('org/repo-a')
        eval2 = MinerEvaluation(uid=2, hotkey='hk2', merged_pull_requests=[pr2])
        eval2.unique_repos_contributed_to.add('org/repo-a')
        finalize_miner_scores({1: eval1, 2: eval2})

        assert pr1.pioneer_multiplier == pytest.approx(1.0 + PIONEER_BASE_BONUS, abs=0.01)
        assert pr2.pioneer_multiplier == 1.0

    def test_only_pioneering_pr_gets_bonus(self, builder, bronze):
        """Single-PR scoping: follow-up PRs to same repo score at 1.0."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1,
            merged_at=now - timedelta(days=10), earned_score=0.0, collateral_score=0.0,
        )
        followup = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1,
            merged_at=now - timedelta(days=2), earned_score=0.0, collateral_score=0.0,
        )
        pioneer_pr.base_score = 30.0
        followup.base_score = 30.0
        eval1 = MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[pioneer_pr, followup])
        eval1.unique_repos_contributed_to.add('org/repo-a')
        finalize_miner_scores({1: eval1})

        assert pioneer_pr.pioneer_multiplier > 1.0
        assert followup.pioneer_multiplier == 1.0

    def test_multi_repo_damping_applied(self, builder, bronze):
        """Pioneering 4 repos yields damped bonus per repo."""
        now = datetime.now(timezone.utc)
        prs = []
        for i in range(4):
            pr = builder.create(
                state=PRState.MERGED, tier=bronze, repo=f'org/repo-{i}', uid=1,
                merged_at=now - timedelta(days=i), earned_score=0.0, collateral_score=0.0,
            )
            pr.base_score = 30.0
            prs.append(pr)
        eval1 = MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=prs)
        for pr in prs:
            eval1.unique_repos_contributed_to.add(pr.repository_full_name)
        finalize_miner_scores({1: eval1})

        expected = round(1.0 + PIONEER_BASE_BONUS / 4**PIONEER_MULTI_REPO_DAMPING, 2)
        for pr in prs:
            assert pr.pioneer_multiplier == expected

    def test_low_quality_snipe_blocked(self, builder, bronze):
        """Low-quality early merge gets 1.0, quality follower becomes pioneer."""
        now = datetime.now(timezone.utc)
        snipe = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=1,
            merged_at=now - timedelta(days=10), token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
            earned_score=0.0, collateral_score=0.0,
        )
        snipe.base_score = 10.0
        good = builder.create(
            state=PRState.MERGED, tier=bronze, repo='org/repo-a', uid=2,
            merged_at=now - timedelta(days=5), earned_score=0.0, collateral_score=0.0,
        )
        good.base_score = 30.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='hk1', merged_pull_requests=[snipe]),
            2: MinerEvaluation(uid=2, hotkey='hk2', merged_pull_requests=[good]),
        }
        evals[1].unique_repos_contributed_to.add('org/repo-a')
        evals[2].unique_repos_contributed_to.add('org/repo-a')
        finalize_miner_scores(evals)

        assert snipe.pioneer_multiplier == 1.0
        assert good.pioneer_multiplier > 1.0

    def test_count_pioneered_repos(self):
        ranks = {'repo-a': {1: 1, 2: 2}, 'repo-b': {1: 1, 3: 2}, 'repo-c': {2: 1, 1: 2}}
        assert count_pioneered_repos(ranks) == {1: 2, 2: 1}


# ============================================================================
# TestPioneerIncentiveEvidence
# ============================================================================


class TestPioneerIncentiveEvidence:
    """Evidence tests proving the mechanism rewards exploration over pile-on."""

    def test_exploration_beats_pile_on(self, builder, bronze):
        """5 miners on 5 repos yield 5x more pioneer bonus than 5 miners on 1 repo."""
        now = datetime.now(timezone.utc)

        # Pile-on: 5 miners on 1 repo — only 1 pioneer
        pile_evals = {}
        for uid in range(1, 6):
            pr = builder.create(
                state=PRState.MERGED, tier=bronze, repo='org/saturated', uid=uid,
                merged_at=now - timedelta(days=uid), earned_score=0.0, collateral_score=0.0,
            )
            pr.base_score = 30.0
            pile_evals[uid] = MinerEvaluation(uid=uid, hotkey=f'h{uid}', merged_pull_requests=[pr])
            pile_evals[uid].unique_repos_contributed_to.add('org/saturated')
        finalize_miner_scores(pile_evals)
        pile_bonus = sum(pr.pioneer_multiplier - 1.0 for ev in pile_evals.values() for pr in ev.merged_pull_requests)

        # Explore: 5 miners each on unique repo — 5 pioneers
        builder.reset()
        explore_evals = {}
        for uid in range(1, 6):
            pr = builder.create(
                state=PRState.MERGED, tier=bronze, repo=f'org/new-{uid}', uid=uid,
                merged_at=now - timedelta(days=uid), earned_score=0.0, collateral_score=0.0,
            )
            pr.base_score = 30.0
            explore_evals[uid] = MinerEvaluation(uid=uid, hotkey=f'he{uid}', merged_pull_requests=[pr])
            explore_evals[uid].unique_repos_contributed_to.add(f'org/new-{uid}')
        finalize_miner_scores(explore_evals)
        explore_bonus = sum(
            pr.pioneer_multiplier - 1.0 for ev in explore_evals.values() for pr in ev.merged_pull_requests
        )

        assert explore_bonus > pile_bonus
        assert pile_bonus == pytest.approx(PIONEER_BASE_BONUS)
        assert explore_bonus == pytest.approx(PIONEER_BASE_BONUS * 5)
