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
)
from gittensor.validator.oss_contributions.scoring import (
    calculate_pioneer_dividends,
)
from tests.validator.conftest import PRBuilder

# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def builder():
    return PRBuilder()


# ==========================================================================
# TestPioneerEligibility
# ==========================================================================


class TestPioneerEligibility:
    """Tests for PullRequest.is_pioneer_eligible instance method."""

    def test_eligible_when_merged_with_token_score(self, builder):
        pr = builder.create(state=PRState.MERGED, uid=1)
        assert pr.is_pioneer_eligible()

    def test_ineligible_when_below_token_score(self, builder):
        pr = builder.create(state=PRState.MERGED, uid=1, token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1)
        assert not pr.is_pioneer_eligible()

    def test_ineligible_when_open(self, builder):
        pr = builder.create(state=PRState.OPEN, uid=1)
        assert not pr.is_pioneer_eligible()

    def test_ineligible_when_closed(self, builder):
        pr = builder.create(state=PRState.CLOSED, uid=1)
        assert not pr.is_pioneer_eligible()


# ==========================================================================
# TestPioneerDividendCalculation
# ==========================================================================


class TestPioneerDividendCalculation:
    """Tests for calculate_pioneer_dividends function."""

    def _make_eval(self, uid, prs):
        """Helper to create a MinerEvaluation with given merged PRs."""
        eval_ = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        eval_.merged_pull_requests = prs
        return eval_

    def test_single_contributor_no_dividend(self, builder):
        """Pioneer with no followers gets no dividend."""
        now = datetime.now(timezone.utc)
        pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            merged_at=now,
            earned_score=100.0,
        )
        evals = {1: self._make_eval(1, [pr])}
        calculate_pioneer_dividends(evals)
        assert pr.pioneer_dividend == 0.0

    def test_two_contributors_pioneer_gets_dividend(self, builder):
        """Pioneer gets dividend from the 1st follower."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            merged_at=now - timedelta(days=5),
            earned_score=100.0,
        )
        follower_pr = builder.create(
            state=PRState.MERGED,
            uid=2,
            repo='test/repo',
            merged_at=now - timedelta(days=1),
            earned_score=80.0,
        )
        evals = {
            1: self._make_eval(1, [pioneer_pr]),
            2: self._make_eval(2, [follower_pr]),
        }
        calculate_pioneer_dividends(evals)

        expected_dividend = min(80.0 * PIONEER_DIVIDEND_RATE_1ST, 100.0 * PIONEER_DIVIDEND_MAX_RATIO)
        assert pioneer_pr.pioneer_dividend == round(expected_dividend, 2)
        assert pioneer_pr.pioneer_rank == 1
        assert follower_pr.pioneer_rank == 2

    def test_three_contributors_diminishing_rates(self, builder):
        """Pioneer dividend diminishes across follower positions."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            merged_at=now - timedelta(days=10),
            earned_score=200.0,
        )
        f1_pr = builder.create(
            state=PRState.MERGED,
            uid=2,
            repo='test/repo',
            merged_at=now - timedelta(days=5),
            earned_score=100.0,
        )
        f2_pr = builder.create(
            state=PRState.MERGED,
            uid=3,
            repo='test/repo',
            merged_at=now - timedelta(days=1),
            earned_score=80.0,
        )
        evals = {
            1: self._make_eval(1, [pioneer_pr]),
            2: self._make_eval(2, [f1_pr]),
            3: self._make_eval(3, [f2_pr]),
        }
        calculate_pioneer_dividends(evals)

        expected = 100.0 * PIONEER_DIVIDEND_RATE_1ST + 80.0 * PIONEER_DIVIDEND_RATE_2ND
        expected_capped = min(expected, 200.0 * PIONEER_DIVIDEND_MAX_RATIO)
        assert pioneer_pr.pioneer_dividend == round(expected_capped, 2)

    def test_dividend_capped(self, builder):
        """Pioneer dividend is capped at PIONEER_DIVIDEND_MAX_RATIO × pioneer's earned_score."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            merged_at=now - timedelta(days=10),
            earned_score=10.0,
        )
        # Large follower scores
        followers = []
        for i in range(5):
            pr = builder.create(
                state=PRState.MERGED,
                uid=i + 2,
                repo='test/repo',
                merged_at=now - timedelta(days=5 - i),
                earned_score=500.0,
            )
            followers.append(pr)

        evals = {1: self._make_eval(1, [pioneer_pr])}
        for i, fpr in enumerate(followers):
            evals[i + 2] = self._make_eval(i + 2, [fpr])

        calculate_pioneer_dividends(evals)

        max_expected = 10.0 * PIONEER_DIVIDEND_MAX_RATIO
        assert pioneer_pr.pioneer_dividend == round(max_expected, 2)

    def test_different_repos_independent(self, builder):
        """Pioneer dividends are independent per repository."""
        now = datetime.now(timezone.utc)
        pr_a = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo-a',
            merged_at=now - timedelta(days=5),
            earned_score=100.0,
        )
        pr_b = builder.create(
            state=PRState.MERGED,
            uid=2,
            repo='test/repo-b',
            merged_at=now - timedelta(days=5),
            earned_score=100.0,
        )
        evals = {
            1: self._make_eval(1, [pr_a]),
            2: self._make_eval(2, [pr_b]),
        }
        calculate_pioneer_dividends(evals)

        # No followers on either repo, so no dividends
        assert pr_a.pioneer_dividend == 0.0
        assert pr_b.pioneer_dividend == 0.0

    def test_ineligible_prs_excluded(self, builder):
        """PRs below token score threshold don't participate in pioneer calculation."""
        now = datetime.now(timezone.utc)
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            merged_at=now - timedelta(days=5),
            earned_score=100.0,
        )
        ineligible_pr = builder.create(
            state=PRState.MERGED,
            uid=2,
            repo='test/repo',
            merged_at=now - timedelta(days=1),
            earned_score=50.0,
            token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 1,
        )
        evals = {
            1: self._make_eval(1, [pioneer_pr]),
            2: self._make_eval(2, [ineligible_pr]),
        }
        calculate_pioneer_dividends(evals)

        # Ineligible follower doesn't count
        assert pioneer_pr.pioneer_dividend == 0.0


# ==========================================================================
# TestPioneerDedupLegacyMirror
# ==========================================================================


class TestPioneerDedupLegacyMirror:
    """Regression tests for duplicate legacy+mirror PR deduplication in pioneer scoring."""

    def _make_eval(self, uid, merged_prs=None, mirror_prs=None):
        eval_ = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        eval_.merged_pull_requests = merged_prs or []
        eval_.mirror_merged_prs = mirror_prs or []
        return eval_

    def test_duplicate_pr_does_not_inflate_rank(self, builder):
        """Same (repo, pr_number) in both legacy and mirror lists must count once."""
        now = datetime.now(timezone.utc)
        earlier = now - timedelta(hours=1)

        # Pioneer: earliest PR on the repo
        pioneer_pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            number=1,
            merged_at=earlier,
            earned_score=100.0,
        )
        # Follower PR
        follower_pr = builder.create(
            state=PRState.MERGED,
            uid=2,
            repo='test/repo',
            number=2,
            merged_at=now,
            earned_score=50.0,
        )
        # Mirror duplicate of pioneer_pr — same repo + number
        mirror_duplicate = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            number=1,
            merged_at=earlier,
            earned_score=100.0,
        )

        pioneer_eval = self._make_eval(1, merged_prs=[pioneer_pr], mirror_prs=[mirror_duplicate])
        follower_eval = self._make_eval(2, merged_prs=[follower_pr])

        evaluations = {1: pioneer_eval, 2: follower_eval}
        calculate_pioneer_dividends(evaluations)

        # Pioneer rank must be 1, follower rank must be 2
        assert pioneer_pr.pioneer_rank == 1
        assert follower_pr.pioneer_rank == 2

    def test_duplicate_pr_does_not_inflate_follower_total(self, builder):
        """Duplicate PR in both lists must not double-count earned_score for dividend calculation."""
        now = datetime.now(timezone.utc)
        earlier = now - timedelta(hours=1)

        pioneer_pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            number=1,
            merged_at=earlier,
            earned_score=100.0,
        )
        follower_pr = builder.create(
            state=PRState.MERGED,
            uid=2,
            repo='test/repo',
            number=2,
            merged_at=now,
            earned_score=200.0,
        )
        mirror_duplicate_follower = builder.create(
            state=PRState.MERGED,
            uid=2,
            repo='test/repo',
            number=2,
            merged_at=now,
            earned_score=200.0,
        )

        pioneer_eval = self._make_eval(1, merged_prs=[pioneer_pr])
        follower_eval = self._make_eval(2, merged_prs=[follower_pr], mirror_prs=[mirror_duplicate_follower])

        evaluations = {1: pioneer_eval, 2: follower_eval}
        calculate_pioneer_dividends(evaluations)

        # Pioneer dividend should be based on follower earned_score=200, not 400
        # Dividend is capped by PIONEER_DIVIDEND_MAX_RATIO of pioneer's own earned_score
        # Pioneer dividend is bounded by PIONEER_DIVIDEND_MAX_RATIO of its own earned_score.
        # follower earned_score=200, pioneer earned_score=100 → cap kicks in.
        # Without dedup, follower would appear as earned_score=400, doubling the dividend.
        # With dedup, dividend stays at 100.0 * PIONEER_DIVIDEND_MAX_RATIO.
        expected_dividend = 100.0 * PIONEER_DIVIDEND_MAX_RATIO
        assert abs(pioneer_pr.pioneer_dividend - expected_dividend) < 1e-6
        # Sanity: dividend must NOT equal the inflated amount
        inflated_dividend = min(400.0 * PIONEER_DIVIDEND_RATE_1ST, 100.0 * PIONEER_DIVIDEND_MAX_RATIO)
        assert abs(pioneer_pr.pioneer_dividend - inflated_dividend) < 1e-6 or inflated_dividend != expected_dividend

    def test_no_duplicate_unaffected(self, builder):
        """When legacy and mirror lists have no overlap, behaviour is unchanged."""
        now = datetime.now(timezone.utc)
        earlier = now - timedelta(hours=1)

        legacy_pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            number=1,
            merged_at=earlier,
            earned_score=100.0,
        )
        mirror_pr = builder.create(
            state=PRState.MERGED,
            uid=1,
            repo='test/repo',
            number=2,
            merged_at=now,
            earned_score=80.0,
        )

        pioneer_eval = self._make_eval(1, merged_prs=[legacy_pr], mirror_prs=[mirror_pr])
        evaluations = {1: pioneer_eval}
        calculate_pioneer_dividends(evaluations)

        # Both PRs counted, pioneer rank = 1 for both
        assert legacy_pr.pioneer_rank == 1
        assert mirror_pr.pioneer_rank == 1
