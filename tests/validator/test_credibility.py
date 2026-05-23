"""Tests for PR credibility — volume-aware gate (issue #1340).

Credibility uses a Beta prior so a couple of closed PRs don't sink an
active miner. It only gates eligibility; it's no longer a per-PR multiplier.
"""

from unittest.mock import Mock

import pytest

from gittensor.validator.oss_contributions.credibility import PRIOR_K, calculate_credibility, check_eligibility
from gittensor.validator.utils.load_weights import RepoEligibilityConfig, resolve_eligibility

_CFG = resolve_eligibility(None)


def _scored_pr(merged: bool = True):
    pr = Mock()
    pr.token_score = 10.0 if merged else 0.0
    return pr


class TestCalculateCredibility:
    def test_no_attempts_returns_zero(self):
        assert calculate_credibility([], []) == 0.0

    def test_all_merged(self):
        merged = [_scored_pr() for _ in range(5)]
        assert calculate_credibility(merged, []) == pytest.approx(
            (5 + PRIOR_K) / (5 + 2 * PRIOR_K), abs=1e-3
        )

    def test_all_closed(self):
        closed = [_scored_pr(merged=False) for _ in range(3)]
        assert calculate_credibility([], closed) == pytest.approx(
            PRIOR_K / (3 + 2 * PRIOR_K), abs=1e-3
        )

    @pytest.mark.parametrize(
        'merged_count,closed_count,expected',
        [
            # Before (flat): 3/0 = 1.0 → free pass for low activity
            # After  (prior): (3+2)/(3+4) ≈ 0.71 → no free 1.0
            (3, 0, pytest.approx((3 + PRIOR_K) / (3 + 2 * PRIOR_K), abs=1e-3)),
            # Before: 30/8 = 0.79 → death sentence for active miner
            # After:  (30+2)/(38+4) ≈ 0.76 → recoverable
            (30, 8, pytest.approx((30 + PRIOR_K) / (38 + 2 * PRIOR_K), abs=1e-3)),
            # Before: 50/10 = 0.83 → slight penalty
            # After:  (50+2)/(60+4) ≈ 0.81 → still passes typical gate
            (50, 10, pytest.approx((50 + PRIOR_K) / (60 + 2 * PRIOR_K), abs=1e-3)),
            # Single merged, no closed
            (1, 0, pytest.approx((1 + PRIOR_K) / (1 + 2 * PRIOR_K), abs=1e-3)),
            # Zero merged, some closed
            (0, 5, pytest.approx(PRIOR_K / (5 + 2 * PRIOR_K), abs=1e-3)),
            # Balance
            (10, 10, pytest.approx((10 + PRIOR_K) / (20 + 2 * PRIOR_K), abs=1e-3)),
        ],
    )
    def test_volume_aware_formula(self, merged_count: int, closed_count: int, expected: float):
        merged = [_scored_pr() for _ in range(merged_count)]
        closed = [_scored_pr(merged=False) for _ in range(closed_count)]
        assert calculate_credibility(merged, closed) == expected


class TestCheckEligibility:
    def test_credibility_gates_but_does_not_multiply(self):
        """Credibility only gates — once eligible, PRs score at full base."""
        # Use relaxed config to pass both valid-merged (0) and credibility (0.0) gates
        relaxed = resolve_eligibility(RepoEligibilityConfig(min_valid_merged_prs=0, min_credibility=0.0))
        merged = [_scored_pr() for _ in range(4)]
        closed = [_scored_pr(merged=False)]

        is_eligible, credibility, reason = check_eligibility(merged, closed, relaxed)
        assert is_eligible is True
        assert reason == ''

        # credibility itself is informational here — no longer a multiplier

    def test_below_threshold_is_ineligible(self):
        # 3 merged with valid token_score + enough closed to drop credibility below 0.80
        merged = [_scored_pr() for _ in range(3)]
        closed = [_scored_pr(merged=False) for _ in range(3)]

        is_eligible, _, reason = check_eligibility(merged, closed, _CFG)
        assert is_eligible is False
        assert 'credibility' in reason

    def test_not_enough_valid_merged_is_ineligible(self):
        merged = [_scored_pr()]
        merged[0].token_score = 0.0  # below min token threshold

        is_eligible, _, reason = check_eligibility(merged, [], _CFG)
        assert is_eligible is False
        assert 'valid merged PRs' in reason

    def test_per_repo_override_relaxes_gate(self):
        relaxed = resolve_eligibility(RepoEligibilityConfig(min_valid_merged_prs=0, min_credibility=0.0))
        merged = [_scored_pr()]
        is_eligible, _, _ = check_eligibility(merged, [], relaxed)
        assert is_eligible is True
