"""Tests for the dynamic per-repository open-PR spam threshold.

Threshold = min(base + floor(total_token_score / per_slot), max), all resolved
per repository from its eligibility config.
"""

from gittensor.validator.oss_contributions.scoring import (
    calculate_open_pr_threshold,
    calculate_pr_spam_penalty_multiplier,
)
from gittensor.validator.utils.load_weights import RepoEligibilityConfig, resolve_eligibility

_CFG = resolve_eligibility(None)  # global defaults
_BASE = _CFG.excessive_pr_penalty_base_threshold
_PER_SLOT = _CFG.open_pr_threshold_token_score
_MAX = _CFG.max_open_pr_threshold


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

    def test_zero_multiplier_above_threshold(self):
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 1) == 0.0

    def test_zero_multiplier_well_above_threshold(self):
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 50) == 0.0

    def test_token_score_bonus_increases_threshold(self):
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 2, _PER_SLOT * 2) == 1.0
        assert calculate_pr_spam_penalty_multiplier(_CFG, _BASE + 3, _PER_SLOT * 2) == 0.0
