"""Tests for the dynamic per-repository open-PR spam threshold.

Threshold = min(base + floor(total_token_score / per_slot), max), all resolved
per repository from its eligibility config.

The spam penalty ramps linearly from 1.0 at threshold down to 0.0 at
threshold + SPAM_PENALTY_ZERO_AT_OVERAGE, then stays at 0.0 beyond that.
"""

from gittensor.constants import SPAM_PENALTY_ZERO_AT_OVERAGE
from gittensor.validator.oss_contributions.scoring import (
    calculate_open_pr_threshold,
    calculate_pr_spam_penalty_multiplier,
)
from gittensor.validator.utils.load_weights import RepoEligibilityConfig, resolve_eligibility

_CFG = resolve_eligibility(None)  # global defaults
_BASE = _CFG.excessive_pr_penalty_base_threshold
_PER_SLOT = _CFG.open_pr_threshold_token_score
_MAX = _CFG.max_open_pr_threshold
_ZERO_AT = SPAM_PENALTY_ZERO_AT_OVERAGE


class TestCalculateOpenPrThreshold:
    def test_no_token_score_returns_base_threshold(self):
        assert calculate_open_pr_threshold(_CFG) == _BASE

    def test_zero_token_score_returns_base_threshold(self):
        assert calculate_open_pr_threshold(_CFG, 0.0) == _BASE

    def test_below_one_slot_no_bonus(self):
        assert calculate_open_pr_threshold(_CFG, _PER_SLOT - 1) == _BASE

    def test_one_slot_grants_bonus(self):
        assert calculate_open_pr_threshold(_CFG, _PER_SLOT) == _BASE + 1

    def test_two_slots_grant_double_bonus(self):
        assert calculate_open_pr_threshold(_CFG, _PER_SLOT * 2) == _BASE + 2

    def test_threshold_capped_at_max(self):
        assert calculate_open_pr_threshold(_CFG, _PER_SLOT * 10_000) == _MAX

    def test_per_repo_override(self):
        cfg = resolve_eligibility(RepoEligibilityConfig(excessive_pr_penalty_base_threshold=10))
        assert calculate_open_pr_threshold(cfg) == 10


class TestCalculatePrSpamPenaltyMultiplier:
    def test_no_penalty_at_threshold(self):
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE) == 1.0

    def test_no_penalty_below_threshold(self):
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE - 1) == 1.0

    def test_partial_penalty_one_over(self):
        expected = round(1.0 - 1 / _ZERO_AT, 10)
        assert round(calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 1), 10) == expected

    def test_partial_penalty_two_over(self):
        expected = round(1.0 - 2 / _ZERO_AT, 10)
        assert round(calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 2), 10) == expected

    def test_zero_multiplier_at_zero_at_overage(self):
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE + _ZERO_AT) == 0.0

    def test_zero_multiplier_well_above_threshold(self):
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 50) == 0.0

    def test_token_score_bonus_increases_threshold(self):
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 2, _PER_SLOT * 2) == 1.0
        # one over the new threshold (base+2) ramps, not snaps to zero
        result = calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 3, _PER_SLOT * 2)
        assert 0.0 < result < 1.0
