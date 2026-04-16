# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for shared credibility and reward normalization helpers."""

import pytest

from gittensor.constants import CREDIBILITY_MULLIGAN_COUNT
from gittensor.validator.utils.credibility_math import mulligan_success_ratio
from gittensor.validator.utils.reward_normalization import normalize_reward_ratios


class TestMulliganSuccessRatio:
    def test_matches_legacy_oss_formula(self):
        merged, closed_raw = 5, 3
        expected = merged / (merged + max(0, closed_raw - CREDIBILITY_MULLIGAN_COUNT))
        assert mulligan_success_ratio(merged, closed_raw) == pytest.approx(expected)

    def test_matches_legacy_issue_formula(self):
        solved, closed_raw = 7, 4
        expected = solved / (solved + max(0, closed_raw - CREDIBILITY_MULLIGAN_COUNT))
        assert mulligan_success_ratio(solved, closed_raw) == pytest.approx(expected)

    def test_zero_attempts(self):
        assert mulligan_success_ratio(0, 0) == 0.0

    def test_custom_mulligan(self):
        assert mulligan_success_ratio(2, 5, mulligan=2) == pytest.approx(2 / (2 + 3))


class TestNormalizeRewardRatios:
    def test_empty(self):
        assert normalize_reward_ratios({}) == {}

    def test_all_zero_returns_unchanged(self):
        raw = {1: 0.0, 2: 0.0}
        assert normalize_reward_ratios(raw) is raw

    def test_normalizes_to_unit_sum(self):
        out = normalize_reward_ratios({10: 2.0, 20: 3.0})
        assert sum(out.values()) == pytest.approx(1.0)
        assert out[10] == pytest.approx(0.4)
        assert out[20] == pytest.approx(0.6)
