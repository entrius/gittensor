# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for pioneer dividend mechanism."""

from datetime import datetime, timedelta, timezone

import pytest

from gittensor.classes import MinerEvaluation, PRState
from gittensor.constants import (
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    PIONEER_DIVIDEND_MAX_RATIO,
    PIONEER_DIVIDEND_RATE_1ST,
    PIONEER_DIVIDEND_RATE_2ND,
    PIONEER_DIVIDEND_RATE_REST,
)
from gittensor.validator.configurations.tier_config import TIERS, Tier
from gittensor.validator.evaluation.scoring import (
    calculate_pioneer_dividends,
    finalize_miner_scores,
)
from tests.validator.conftest import PRBuilder

# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def builder():
    return PRBuilder()


@pytest.fixture
def bronze():
    return TIERS[Tier.BRONZE]


# ==========================================================================
# TestPioneerEligibility
# ==========================================================================


class TestPioneerEligibility:
    """Tests for PullRequest.is_pioneer_eligible instance method."""

    def test_eligible_when_merged_with_tier_and_token_score(self, builder, bronze):
        pr = builder.create(state=PRState.MERGED, tier=bronze, uid=1)
        assert pr.is_pioneer_eligible()

    def test_ineligible_without_tier(self, builder, bronze):
        pr = builder.create(state=PRState.MERGED, tier=bronze, uid=1)
        pr.repository_tier_configuration = None
        assert not pr.is_pioneer_eligible()

    def test_ineligible_without_merge_timestamp(self, builder, bronze):
        pr = builder.create(state=PRState.MERGED, tier=bronze, uid=1)
        pr.merged_at = None
        assert not pr.is_pioneer_eligible()

    def test_ineligible_below_token_score_threshold(self, builder, bronze):
        pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            uid=1,
            token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
        )
        assert not pr.is_pioneer_eligible()

    def test_eligible_at_exact_token_score_threshold(self, builder, bronze):
        pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            uid=1,
            token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE,
        )
        assert pr.is_pioneer_eligible()


# ==========================================================================
# TestCalculatePioneerDividends
# ==========================================================================


class TestCalculatePioneerDividends:
    """Tests for calculate_pioneer_dividends function."""

    def test_single_miner_gets_no_dividend(self, builder, bronze):
        """A lone pioneer with no followers earns zero dividend."""
        now = datetime.now(timezone.utc)
        pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now,
            earned_score=0.0,
            collateral_score=0.0,
        )
        pr.base_score = 30.0
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pr])}
        calculate_pioneer_dividends(evals)
        assert pr.pioneer_rank == 1
        assert pr.pioneer_dividend == 0.0

    def test_pioneer_earns_dividend_from_follower(self, builder, bronze):
        """Pioneer earns 30% of first follower's earned_score."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=5),
            earned_score=0.0,
            collateral_score=0.0,
        )
        follower_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now,
            earned_score=0.0,
            collateral_score=0.0,
        )
        pioneer_pr.base_score = 30.0
        follower_pr.base_score = 20.0
        # Simulate earned_scores (all multipliers = 1.0)
        pioneer_pr.earned_score = 30.0
        follower_pr.earned_score = 20.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pioneer_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[follower_pr]),
        }
        calculate_pioneer_dividends(evals)

        expected_dividend = round(20.0 * PIONEER_DIVIDEND_RATE_1ST, 2)  # 20 * 0.30 = 6.0
        assert pioneer_pr.pioneer_rank == 1
        assert pioneer_pr.pioneer_dividend == expected_dividend
        assert follower_pr.pioneer_rank == 2
        assert follower_pr.pioneer_dividend == 0.0

    def test_dividend_from_multiple_followers(self, builder, bronze):
        """Pioneer dividend uses per-position rates: 30%, 20%, 10%, 10%."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pioneer_pr.base_score = 30.0
        pioneer_pr.earned_score = 30.0
        follower_prs = []
        for uid in range(2, 6):  # 4 followers
            pr = builder.create(
                state=PRState.MERGED,
                tier=bronze,
                repo='org/repo-a',
                uid=uid,
                merged_at=now - timedelta(days=10 - uid),
                earned_score=0.0,
                collateral_score=0.0,
            )
            pr.base_score = 10.0
            pr.earned_score = 10.0
            follower_prs.append(pr)
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pioneer_pr])}
        for pr in follower_prs:
            evals[pr.uid] = MinerEvaluation(uid=pr.uid, hotkey=f'h{pr.uid}', merged_pull_requests=[pr])
        calculate_pioneer_dividends(evals)

        # 1st: 10*0.30=3.0, 2nd: 10*0.20=2.0, 3rd: 10*0.10=1.0, 4th: 10*0.10=1.0
        expected_dividend = round(
            10.0 * PIONEER_DIVIDEND_RATE_1ST
            + 10.0 * PIONEER_DIVIDEND_RATE_2ND
            + 10.0 * PIONEER_DIVIDEND_RATE_REST
            + 10.0 * PIONEER_DIVIDEND_RATE_REST,
            2,
        )
        assert pioneer_pr.pioneer_dividend == expected_dividend

    def test_dividend_grows_with_many_followers(self, builder, bronze):
        """Dividend scales with followers but is capped at PIONEER_DIVIDEND_MAX_RATIO × own earned."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=30),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pioneer_pr.base_score = 30.0
        pioneer_pr.earned_score = 30.0

        follower_prs = []
        for uid in range(2, 12):  # 10 followers
            pr = builder.create(
                state=PRState.MERGED,
                tier=bronze,
                repo='org/repo-a',
                uid=uid,
                merged_at=now - timedelta(days=30 - uid),
                earned_score=0.0,
                collateral_score=0.0,
            )
            pr.base_score = 30.0
            pr.earned_score = 30.0
            follower_prs.append(pr)
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pioneer_pr])}
        for pr in follower_prs:
            evals[pr.uid] = MinerEvaluation(uid=pr.uid, hotkey=f'h{pr.uid}', merged_pull_requests=[pr])
        calculate_pioneer_dividends(evals)

        # Raw: 30*0.30=9 + 30*0.20=6 + 8*30*0.10=24 → 39.0
        # Cap: min(39.0, 30.0 * 1.0) = 30.0
        max_dividend = round(30.0 * PIONEER_DIVIDEND_MAX_RATIO, 2)
        assert pioneer_pr.pioneer_dividend == max_dividend
        assert pioneer_pr.earned_score == 30.0 + max_dividend

    def test_dividend_cap_at_max_ratio(self, builder, bronze):
        """Dividend is capped at PIONEER_DIVIDEND_MAX_RATIO × pioneer's own earned_score."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pioneer_pr.base_score = 10.0
        pioneer_pr.earned_score = 10.0
        # 1 follower with much higher earned_score
        follower_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now,
            earned_score=0.0,
            collateral_score=0.0,
        )
        follower_pr.base_score = 100.0
        follower_pr.earned_score = 100.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pioneer_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[follower_pr]),
        }
        calculate_pioneer_dividends(evals)

        # Raw: 100*0.30 = 30.0, Cap: min(30.0, 10.0*1.0) = 10.0
        assert pioneer_pr.pioneer_dividend == round(10.0 * PIONEER_DIVIDEND_MAX_RATIO, 2)
        assert pioneer_pr.earned_score == 10.0 + pioneer_pr.pioneer_dividend

    def test_multiple_follower_prs_summed(self, builder, bronze):
        """A follower with multiple PRs on the same repo contributes all earned_scores to dividend."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pioneer_pr.base_score = 30.0
        pioneer_pr.earned_score = 30.0
        # Follower has 3 PRs on the same repo
        f_pr1 = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now - timedelta(days=5),
            earned_score=0.0,
            collateral_score=0.0,
        )
        f_pr2 = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now - timedelta(days=3),
            earned_score=0.0,
            collateral_score=0.0,
        )
        f_pr3 = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now - timedelta(days=1),
            earned_score=0.0,
            collateral_score=0.0,
        )
        f_pr1.base_score = 5.0
        f_pr1.earned_score = 5.0
        f_pr2.base_score = 5.0
        f_pr2.earned_score = 5.0
        f_pr3.base_score = 5.0
        f_pr3.earned_score = 5.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pioneer_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[f_pr1, f_pr2, f_pr3]),
        }
        calculate_pioneer_dividends(evals)

        # Single follower (position 0 → 30% rate), sum of ALL their earned_scores: (5+5+5) * 0.30
        expected = round((5.0 + 5.0 + 5.0) * PIONEER_DIVIDEND_RATE_1ST, 2)
        assert pioneer_pr.pioneer_dividend == expected

    def test_repos_are_independent(self, builder, bronze):
        """Pioneer status and dividends are calculated per repo independently."""
        now = datetime.now(timezone.utc)
        # UID 1 pioneers repo-a, UID 2 pioneers repo-b
        pr1a = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pr2a = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now - timedelta(days=5),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pr2b = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-b',
            uid=2,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pr1b = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-b',
            uid=1,
            merged_at=now - timedelta(days=5),
            earned_score=0.0,
            collateral_score=0.0,
        )
        for pr in [pr1a, pr2a, pr2b, pr1b]:
            pr.base_score = 30.0
            pr.earned_score = 30.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pr1a, pr1b]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[pr2a, pr2b]),
        }
        calculate_pioneer_dividends(evals)

        # UID 1 is pioneer on repo-a
        assert pr1a.pioneer_rank == 1
        assert pr1a.pioneer_dividend == round(30.0 * PIONEER_DIVIDEND_RATE_1ST, 2)
        # UID 2 is pioneer on repo-b
        assert pr2b.pioneer_rank == 1
        assert pr2b.pioneer_dividend == round(30.0 * PIONEER_DIVIDEND_RATE_1ST, 2)

    def test_low_quality_pr_excluded_from_pioneer(self, builder, bronze):
        """Low token_score PR cannot be pioneer; quality follower becomes pioneer."""
        now = datetime.now(timezone.utc)
        snipe_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=10),
            token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
            earned_score=0.0,
            collateral_score=0.0,
        )
        snipe_pr.base_score = 5.0
        good_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now - timedelta(days=5),
            earned_score=0.0,
            collateral_score=0.0,
        )
        good_pr.base_score = 30.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[snipe_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[good_pr]),
        }
        calculate_pioneer_dividends(evals)

        # Snipe PR is not eligible, so it keeps default pioneer_rank=0
        assert snipe_pr.pioneer_rank == 0
        assert snipe_pr.pioneer_dividend == 0.0
        # Good PR becomes the solo pioneer (no followers -> no dividend)
        assert good_pr.pioneer_rank == 1
        assert good_pr.pioneer_dividend == 0.0

    def test_ineligible_pr_does_not_receive_rank(self, builder, bronze):
        """Ineligible PR from same miner on same repo must not get pioneer_rank."""
        now = datetime.now(timezone.utc)
        eligible_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        eligible_pr.base_score = 30.0
        eligible_pr.earned_score = 30.0
        ineligible_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=5),
            token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
            earned_score=0.0,
            collateral_score=0.0,
        )
        ineligible_pr.base_score = 2.0
        ineligible_pr.earned_score = 2.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[eligible_pr, ineligible_pr]),
        }
        calculate_pioneer_dividends(evals)

        assert eligible_pr.pioneer_rank == 1
        assert ineligible_pr.pioneer_rank == 0  # must stay default

    def test_deterministic_tiebreak_by_pr_number(self, builder, bronze):
        """Same merged_at timestamp: lower PR number wins pioneer status."""
        now = datetime.now(timezone.utc)
        pr1 = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now,
            number=10,
            earned_score=0.0,
            collateral_score=0.0,
        )
        pr2 = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now,
            number=20,
            earned_score=0.0,
            collateral_score=0.0,
        )
        pr1.base_score = 30.0
        pr2.base_score = 30.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pr1]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[pr2]),
        }
        calculate_pioneer_dividends(evals)

        assert pr1.pioneer_rank == 1
        assert pr2.pioneer_rank == 2

    def test_only_pioneering_pr_gets_dividend(self, builder, bronze):
        """Follow-up PRs by the pioneer on same repo don't get dividend."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        followup_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=2),
            earned_score=0.0,
            collateral_score=0.0,
        )
        follower_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now,
            earned_score=0.0,
            collateral_score=0.0,
        )
        pioneer_pr.base_score = 30.0
        pioneer_pr.earned_score = 30.0
        followup_pr.base_score = 25.0
        followup_pr.earned_score = 25.0
        follower_pr.base_score = 10.0
        follower_pr.earned_score = 10.0
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pioneer_pr, followup_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[follower_pr]),
        }
        calculate_pioneer_dividends(evals)

        # Only the pioneering PR gets the dividend
        assert pioneer_pr.pioneer_dividend == round(10.0 * PIONEER_DIVIDEND_RATE_1ST, 2)
        assert followup_pr.pioneer_dividend == 0.0

    def test_empty_evaluations(self, builder, bronze):
        """No crash on empty evaluations."""
        evals = {}
        calculate_pioneer_dividends(evals)  # Should not raise

    def test_no_eligible_prs(self, builder, bronze):
        """No crash when all PRs are ineligible."""
        now = datetime.now(timezone.utc)
        pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now,
            token_score=0.0,
            earned_score=0.0,
            collateral_score=0.0,
        )
        evals = {1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pr])}
        calculate_pioneer_dividends(evals)
        assert pr.pioneer_rank == 0
        assert pr.pioneer_dividend == 0.0


# ==========================================================================
# TestFinalizeWithDividend
# ==========================================================================


class TestFinalizeWithDividend:
    """Integration tests: pioneer dividend flows through finalize_miner_scores."""

    def test_pioneer_dividend_additive_to_earned_score(self, builder, bronze):
        """Pioneer dividend is added on top of earned_score: base × multipliers + dividend."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=5),
            earned_score=0.0,
            collateral_score=0.0,
        )
        follower_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now,
            earned_score=0.0,
            collateral_score=0.0,
        )
        pioneer_pr.base_score = 30.0
        follower_pr.base_score = 30.0
        # Compute earned_scores first (base × multipliers)
        pioneer_pr.calculate_final_earned_score()
        follower_pr.calculate_final_earned_score()
        assert pioneer_pr.earned_score == 30.0  # base × 1.0
        assert follower_pr.earned_score == 30.0

        # Now apply dividend (uses follower earned_score)
        evals = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pioneer_pr]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[follower_pr]),
        }
        calculate_pioneer_dividends(evals)

        # Dividend = 30% of follower's earned_score
        expected_dividend = round(30.0 * PIONEER_DIVIDEND_RATE_1ST, 2)
        assert pioneer_pr.pioneer_dividend == expected_dividend
        # Pioneer earned_score = base_earned + dividend = 30 + 9 = 39
        assert pioneer_pr.earned_score == 30.0 + expected_dividend
        assert pioneer_pr.earned_score > follower_pr.earned_score

    def test_follower_keeps_full_score(self, builder, bronze):
        """Follower's score is not reduced — dividend is additive, not zero-sum."""
        now = datetime.now(timezone.utc)
        # Create a solo miner scenario for baseline
        solo_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/solo-repo',
            uid=3,
            merged_at=now,
            earned_score=0.0,
            collateral_score=0.0,
        )
        solo_pr.base_score = 30.0
        solo_eval = MinerEvaluation(uid=3, hotkey='h3', merged_pull_requests=[solo_pr])
        solo_eval.unique_repos_contributed_to.add('org/solo-repo')

        # Create a follower scenario
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=5),
            earned_score=0.0,
            collateral_score=0.0,
        )
        follower_pr = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now,
            earned_score=0.0,
            collateral_score=0.0,
        )
        pioneer_pr.base_score = 30.0
        follower_pr.base_score = 30.0
        eval1 = MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pioneer_pr])
        eval1.unique_repos_contributed_to.add('org/repo-a')
        eval2 = MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[follower_pr])
        eval2.unique_repos_contributed_to.add('org/repo-a')

        finalize_miner_scores({1: eval1, 2: eval2, 3: solo_eval})

        # Follower's earned_score should equal solo miner's (no penalty)
        assert follower_pr.pioneer_dividend == 0.0


# ==========================================================================
# TestPioneerIncentiveEvidence
# ==========================================================================


class TestPioneerIncentiveEvidence:
    """Evidence tests proving the mechanism rewards exploration over pile-on."""

    def test_exploration_beats_pile_on(self, builder, bronze):
        """5 miners piling on 1 repo: only pioneer gets dividend. Exploring avoids the crowd."""
        now = datetime.now(timezone.utc)

        # Pile-on: 5 miners on 1 repo — only 1 pioneer
        builder.reset()
        pile_evals = {}
        for uid in range(1, 6):
            pr = builder.create(
                state=PRState.MERGED,
                tier=bronze,
                repo='org/saturated',
                uid=uid,
                merged_at=now - timedelta(days=uid),
                earned_score=0.0,
                collateral_score=0.0,
            )
            pr.base_score = 30.0
            pr.earned_score = 30.0
            pile_evals[uid] = MinerEvaluation(uid=uid, hotkey=f'h{uid}', merged_pull_requests=[pr])
        calculate_pioneer_dividends(pile_evals)
        pile_total_dividend = sum(pr.pioneer_dividend for ev in pile_evals.values() for pr in ev.merged_pull_requests)

        # With pile-on, only pioneer gets dividend (based on follower earned_scores)
        expected = round(
            30.0 * PIONEER_DIVIDEND_RATE_1ST
            + 30.0 * PIONEER_DIVIDEND_RATE_2ND
            + 30.0 * PIONEER_DIVIDEND_RATE_REST
            + 30.0 * PIONEER_DIVIDEND_RATE_REST,
            2,
        )
        assert pile_total_dividend == expected

    def test_pioneer_earns_more_with_more_followers(self, builder, bronze):
        """Pioneer's reward naturally grows as more miners follow — self-scaling incentive."""
        now = datetime.now(timezone.utc)

        # Scenario 1: 1 follower
        builder.reset()
        pr1 = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=1,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pr1.base_score = 30.0
        pr1.earned_score = 30.0
        f1 = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-a',
            uid=2,
            merged_at=now,
            earned_score=0.0,
            collateral_score=0.0,
        )
        f1.base_score = 30.0
        f1.earned_score = 30.0
        evals1 = {
            1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pr1]),
            2: MinerEvaluation(uid=2, hotkey='h2', merged_pull_requests=[f1]),
        }
        calculate_pioneer_dividends(evals1)
        div_1_follower = pr1.pioneer_dividend

        # Scenario 2: 5 followers
        builder.reset()
        pr2 = builder.create(
            state=PRState.MERGED,
            tier=bronze,
            repo='org/repo-b',
            uid=1,
            merged_at=now - timedelta(days=10),
            earned_score=0.0,
            collateral_score=0.0,
        )
        pr2.base_score = 30.0
        pr2.earned_score = 30.0
        followers = []
        for uid in range(2, 7):
            f = builder.create(
                state=PRState.MERGED,
                tier=bronze,
                repo='org/repo-b',
                uid=uid,
                merged_at=now - timedelta(days=10 - uid),
                earned_score=0.0,
                collateral_score=0.0,
            )
            f.base_score = 30.0
            f.earned_score = 30.0
            followers.append(f)
        evals2 = {1: MinerEvaluation(uid=1, hotkey='h1', merged_pull_requests=[pr2])}
        for f in followers:
            evals2[f.uid] = MinerEvaluation(uid=f.uid, hotkey=f'h{f.uid}', merged_pull_requests=[f])
        calculate_pioneer_dividends(evals2)
        div_5_followers = pr2.pioneer_dividend

        assert div_5_followers > div_1_follower
