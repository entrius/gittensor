# The MIT License (MIT)
# Copyright (c) 2025 Entrius

"""
Unit tests for reward normalization and dynamic emission scaling.

Tests the normalize_rewards_linear() function which normalizes miner scores
to sum to 1.0, and the dynamic emissions functions which scale rewards
based on network-wide contributions (unique repos, token scores).

Run tests:
    pytest tests/validator/test_normalize_and_dynamic_emissions.py -v

Run specific test class:
    pytest tests/validator/test_normalize_and_dynamic_emissions.py::TestNormalizeRewardsLinear -v
"""

import pytest

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    RECYCLE_UID,
    TOKEN_SCORE_MAX_RECYCLE,
    TOKEN_SCORE_RECYCLE_DECAY_RATE,
    UNIQUE_REPOS_MAX_RECYCLE,
    UNIQUE_REPOS_RECYCLE_DECAY_RATE,
)
from gittensor.validator.configurations.tier_config import Tier
from gittensor.validator.evaluation.dynamic_emissions import (
    _exponential_unlock_scalar,
    apply_dynamic_emissions_using_network_contributions,
    get_network_totals,
)
from gittensor.validator.evaluation.normalize import normalize_rewards_linear


# ============================================================================
# normalize_rewards_linear Tests
# ============================================================================


class TestNormalizeRewardsLinear:
    """Tests for the normalize_rewards_linear function."""

    def _make_eval(self, uid: int, total_score: float) -> MinerEvaluation:
        """Helper to create a MinerEvaluation with a given total_score."""
        evaluation = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        evaluation.total_score = total_score
        return evaluation

    def test_empty_evaluations_returns_empty(self):
        """Empty input returns empty dict."""
        result = normalize_rewards_linear({})
        assert result == {}

    def test_single_miner_positive_score(self):
        """Single miner with positive score normalizes to 1.0."""
        evaluations = {1: self._make_eval(1, 100.0)}
        result = normalize_rewards_linear(evaluations)

        assert result[1] == pytest.approx(1.0)

    def test_two_miners_equal_scores(self):
        """Two miners with equal scores each get 0.5."""
        evaluations = {
            1: self._make_eval(1, 50.0),
            2: self._make_eval(2, 50.0),
        }
        result = normalize_rewards_linear(evaluations)

        assert result[1] == pytest.approx(0.5)
        assert result[2] == pytest.approx(0.5)

    def test_two_miners_unequal_scores(self):
        """Two miners with different scores get proportional shares."""
        evaluations = {
            1: self._make_eval(1, 75.0),
            2: self._make_eval(2, 25.0),
        }
        result = normalize_rewards_linear(evaluations)

        assert result[1] == pytest.approx(0.75)
        assert result[2] == pytest.approx(0.25)

    def test_normalized_scores_sum_to_one(self):
        """All normalized scores should sum to 1.0."""
        evaluations = {
            1: self._make_eval(1, 100.0),
            2: self._make_eval(2, 200.0),
            3: self._make_eval(3, 300.0),
            4: self._make_eval(4, 400.0),
        }
        result = normalize_rewards_linear(evaluations)

        total = sum(result.values())
        assert total == pytest.approx(1.0)

    def test_preserves_score_ratios(self):
        """Normalization preserves the ratio between scores."""
        evaluations = {
            1: self._make_eval(1, 100.0),
            2: self._make_eval(2, 300.0),
        }
        result = normalize_rewards_linear(evaluations)

        # Ratio should be 1:3
        assert result[2] / result[1] == pytest.approx(3.0)

    def test_all_zero_scores_returns_zeros(self):
        """When all scores are zero, returns original zero scores."""
        evaluations = {
            1: self._make_eval(1, 0.0),
            2: self._make_eval(2, 0.0),
            3: self._make_eval(3, 0.0),
        }
        result = normalize_rewards_linear(evaluations)

        for uid in result:
            assert result[uid] == 0.0

    def test_mix_of_zero_and_positive_scores(self):
        """Miners with zero score get 0.0, others share the total."""
        evaluations = {
            1: self._make_eval(1, 0.0),
            2: self._make_eval(2, 100.0),
            3: self._make_eval(3, 0.0),
            4: self._make_eval(4, 100.0),
        }
        result = normalize_rewards_linear(evaluations)

        assert result[1] == 0.0
        assert result[3] == 0.0
        assert result[2] == pytest.approx(0.5)
        assert result[4] == pytest.approx(0.5)
        assert sum(result.values()) == pytest.approx(1.0)

    def test_single_miner_zero_score(self):
        """Single miner with zero score returns 0.0."""
        evaluations = {1: self._make_eval(1, 0.0)}
        result = normalize_rewards_linear(evaluations)

        assert result[1] == 0.0

    def test_many_miners_normalization(self):
        """Many miners normalize correctly and sum to 1.0."""
        evaluations = {i: self._make_eval(i, float(i * 10)) for i in range(1, 21)}
        result = normalize_rewards_linear(evaluations)

        assert len(result) == 20
        assert sum(result.values()) == pytest.approx(1.0)

    def test_very_small_scores_normalize(self):
        """Very small scores still normalize correctly."""
        evaluations = {
            1: self._make_eval(1, 0.001),
            2: self._make_eval(2, 0.002),
            3: self._make_eval(3, 0.003),
        }
        result = normalize_rewards_linear(evaluations)

        assert sum(result.values()) == pytest.approx(1.0)
        assert result[3] / result[1] == pytest.approx(3.0)

    def test_very_large_scores_normalize(self):
        """Very large scores normalize correctly without overflow."""
        evaluations = {
            1: self._make_eval(1, 1_000_000.0),
            2: self._make_eval(2, 2_000_000.0),
        }
        result = normalize_rewards_linear(evaluations)

        assert result[1] == pytest.approx(1.0 / 3.0)
        assert result[2] == pytest.approx(2.0 / 3.0)

    def test_uid_keys_preserved(self):
        """Result dict has the same UIDs as input."""
        evaluations = {
            10: self._make_eval(10, 50.0),
            20: self._make_eval(20, 50.0),
            30: self._make_eval(30, 50.0),
        }
        result = normalize_rewards_linear(evaluations)

        assert set(result.keys()) == {10, 20, 30}


# ============================================================================
# _exponential_unlock_scalar Tests
# ============================================================================


class TestExponentialUnlockScalar:
    """Tests for the _exponential_unlock_scalar helper function."""

    def test_zero_value_returns_minimum(self):
        """Zero input returns (1 - max_recycle), the minimum scalar."""
        result = _exponential_unlock_scalar(0.0, UNIQUE_REPOS_MAX_RECYCLE, UNIQUE_REPOS_RECYCLE_DECAY_RATE)
        assert result == pytest.approx(1.0 - UNIQUE_REPOS_MAX_RECYCLE)

    def test_very_large_value_approaches_one(self):
        """Very large input approaches 1.0 (full unlock)."""
        result = _exponential_unlock_scalar(100_000.0, UNIQUE_REPOS_MAX_RECYCLE, UNIQUE_REPOS_RECYCLE_DECAY_RATE)
        assert result == pytest.approx(1.0, abs=0.01)

    def test_scalar_capped_at_one(self):
        """Scalar is capped at 1.0 and never exceeds it."""
        result = _exponential_unlock_scalar(1_000_000.0, UNIQUE_REPOS_MAX_RECYCLE, UNIQUE_REPOS_RECYCLE_DECAY_RATE)
        assert result <= 1.0

    def test_monotonically_increasing(self):
        """Scalar increases as value increases."""
        values = [0, 10, 50, 100, 200, 500, 1000]
        scalars = [
            _exponential_unlock_scalar(v, UNIQUE_REPOS_MAX_RECYCLE, UNIQUE_REPOS_RECYCLE_DECAY_RATE) for v in values
        ]

        for i in range(len(scalars) - 1):
            assert scalars[i] <= scalars[i + 1], f'Scalar should increase: {scalars[i]} <= {scalars[i + 1]}'

    def test_token_score_scalar_at_zero(self):
        """Token score scalar at zero input returns minimum."""
        result = _exponential_unlock_scalar(0.0, TOKEN_SCORE_MAX_RECYCLE, TOKEN_SCORE_RECYCLE_DECAY_RATE)
        assert result == pytest.approx(1.0 - TOKEN_SCORE_MAX_RECYCLE)

    def test_different_decay_rates_produce_different_curves(self):
        """Different decay rates produce different scalars at the same value."""
        value = 100.0
        scalar_repos = _exponential_unlock_scalar(value, UNIQUE_REPOS_MAX_RECYCLE, UNIQUE_REPOS_RECYCLE_DECAY_RATE)
        scalar_token = _exponential_unlock_scalar(value, TOKEN_SCORE_MAX_RECYCLE, TOKEN_SCORE_RECYCLE_DECAY_RATE)

        # These should be different because the decay rates differ
        assert scalar_repos != scalar_token


# ============================================================================
# get_network_totals Tests
# ============================================================================


class TestGetNetworkTotals:
    """Tests for the get_network_totals function."""

    def _make_tiered_eval(
        self, uid: int, tier: Tier, repos: set, token_score: float = 0.0
    ) -> MinerEvaluation:
        """Helper to create a tiered MinerEvaluation."""
        evaluation = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        evaluation.current_tier = tier
        evaluation.unique_repos_contributed_to = repos
        evaluation.total_token_score = token_score
        return evaluation

    def test_empty_evaluations(self):
        """Empty evaluations return zero totals."""
        unique_repos, total_token = get_network_totals({})
        assert unique_repos == 0
        assert total_token == 0.0

    def test_untiered_miners_excluded(self):
        """Miners without a tier are not counted."""
        evaluation = MinerEvaluation(uid=1, hotkey='hotkey_1')
        evaluation.current_tier = None
        evaluation.unique_repos_contributed_to = {'repo/a', 'repo/b'}
        evaluation.total_token_score = 500.0

        unique_repos, total_token = get_network_totals({1: evaluation})
        assert unique_repos == 0
        assert total_token == 0.0

    def test_tiered_miners_counted(self):
        """Miners with a tier contribute to network totals."""
        eval1 = self._make_tiered_eval(1, Tier.BRONZE, {'repo/a', 'repo/b'}, 100.0)
        eval2 = self._make_tiered_eval(2, Tier.SILVER, {'repo/b', 'repo/c'}, 200.0)

        unique_repos, total_token = get_network_totals({1: eval1, 2: eval2})

        # Unique repos: {'repo/a', 'repo/b', 'repo/c'} = 3
        assert unique_repos == 3
        # Total token: 100 + 200 = 300
        assert total_token == pytest.approx(300.0)

    def test_mix_of_tiered_and_untiered(self):
        """Only tiered miners counted in a mixed group."""
        tiered = self._make_tiered_eval(1, Tier.GOLD, {'repo/a'}, 500.0)
        untiered = MinerEvaluation(uid=2, hotkey='hotkey_2')
        untiered.current_tier = None
        untiered.unique_repos_contributed_to = {'repo/z'}
        untiered.total_token_score = 999.0

        unique_repos, total_token = get_network_totals({1: tiered, 2: untiered})

        assert unique_repos == 1
        assert total_token == pytest.approx(500.0)

    def test_duplicate_repos_deduplicated(self):
        """Same repo across multiple miners counted once."""
        eval1 = self._make_tiered_eval(1, Tier.BRONZE, {'repo/shared'}, 50.0)
        eval2 = self._make_tiered_eval(2, Tier.BRONZE, {'repo/shared'}, 75.0)

        unique_repos, total_token = get_network_totals({1: eval1, 2: eval2})

        assert unique_repos == 1  # Deduplicated
        assert total_token == pytest.approx(125.0)

    def test_empty_repos_set(self):
        """Miner with tier but no repos still contributes token score."""
        evaluation = self._make_tiered_eval(1, Tier.BRONZE, set(), 200.0)

        unique_repos, total_token = get_network_totals({1: evaluation})

        assert unique_repos == 0
        assert total_token == pytest.approx(200.0)


# ============================================================================
# apply_dynamic_emissions_using_network_contributions Tests
# ============================================================================


class TestApplyDynamicEmissions:
    """Tests for the apply_dynamic_emissions_using_network_contributions function."""

    def _make_tiered_eval(
        self, uid: int, tier: Tier, repos: set, token_score: float = 0.0
    ) -> MinerEvaluation:
        """Helper to create a tiered MinerEvaluation."""
        evaluation = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        evaluation.current_tier = tier
        evaluation.unique_repos_contributed_to = repos
        evaluation.total_token_score = token_score
        return evaluation

    def test_empty_rewards_returns_empty(self):
        """Empty normalized rewards returns empty dict."""
        result = apply_dynamic_emissions_using_network_contributions({}, {})
        assert result == {}

    def test_recycle_uid_receives_recycled_emissions(self):
        """RECYCLE_UID should receive recycled emissions when network is small."""
        normalized = {1: 0.5, 2: 0.5}
        evaluations = {
            1: self._make_tiered_eval(1, Tier.BRONZE, {'repo/a'}, 10.0),
            2: self._make_tiered_eval(2, Tier.BRONZE, {'repo/b'}, 10.0),
        }

        result = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # With low network contributions, significant emissions should be recycled
        assert RECYCLE_UID in result
        assert result[RECYCLE_UID] > 0.0

    def test_scaled_rewards_plus_recycle_equals_original_total(self):
        """Total of all scaled rewards (including recycle) should equal original total."""
        normalized = {1: 0.4, 2: 0.3, 3: 0.3}
        evaluations = {
            1: self._make_tiered_eval(1, Tier.BRONZE, {'repo/a'}, 50.0),
            2: self._make_tiered_eval(2, Tier.SILVER, {'repo/b'}, 100.0),
            3: self._make_tiered_eval(3, Tier.GOLD, {'repo/c'}, 200.0),
        }

        result = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        original_total = sum(normalized.values())
        result_total = sum(result.values())
        assert result_total == pytest.approx(original_total, abs=0.001)

    def test_all_miners_scaled_down(self):
        """Individual miner rewards are scaled down (not up) by dynamic emissions."""
        normalized = {1: 0.5, 2: 0.5}
        evaluations = {
            1: self._make_tiered_eval(1, Tier.BRONZE, {'repo/a'}, 10.0),
            2: self._make_tiered_eval(2, Tier.BRONZE, {'repo/b'}, 10.0),
        }

        result = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # Each miner's reward should be <= their normalized reward
        assert result.get(1, 0.0) <= normalized[1]
        assert result.get(2, 0.0) <= normalized[2]

    def test_high_network_contributions_minimal_recycling(self):
        """With many unique repos and high token scores, recycling is minimal."""
        normalized = {1: 0.5, 2: 0.5}

        # Create evaluations with high contributions
        repos = {f'repo/{i}' for i in range(500)}
        evaluations = {
            1: self._make_tiered_eval(1, Tier.GOLD, repos, 50000.0),
            2: self._make_tiered_eval(2, Tier.GOLD, repos, 50000.0),
        }

        result = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # With high contributions, scalar is high, so most emissions go to miners
        recycle_amount = result.get(RECYCLE_UID, 0.0)
        total_rewards = sum(v for k, v in result.items() if k != RECYCLE_UID)
        assert total_rewards > 0.8, f'Most emissions should go to miners, got {total_rewards}'
        assert recycle_amount < 0.2, f'Recycling should be low, got {recycle_amount}'

    def test_no_tiered_miners_maximum_recycling(self):
        """When no miners have tiers, all emissions are recycled."""
        normalized = {1: 0.5, 2: 0.5}
        evaluations = {
            1: MinerEvaluation(uid=1, hotkey='h1'),
            2: MinerEvaluation(uid=2, hotkey='h2'),
        }
        # current_tier is None by default

        result = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # With zero contributions, scalar = (1-max_recycle) = minimum
        # Most emissions go to recycle
        assert RECYCLE_UID in result

    def test_recycle_uid_existing_score_preserved(self):
        """If RECYCLE_UID already has a score in normalized, it is preserved and added to."""
        normalized = {RECYCLE_UID: 0.1, 1: 0.45, 2: 0.45}
        evaluations = {
            RECYCLE_UID: MinerEvaluation(uid=RECYCLE_UID, hotkey='recycle'),
            1: self._make_tiered_eval(1, Tier.BRONZE, {'repo/a'}, 10.0),
            2: self._make_tiered_eval(2, Tier.BRONZE, {'repo/b'}, 10.0),
        }

        result = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # RECYCLE_UID should have at least its scaled portion + recycled amount
        assert result[RECYCLE_UID] > 0.0

    def test_reward_proportions_maintained(self):
        """Relative proportions between non-recycle miners are maintained."""
        normalized = {1: 0.6, 2: 0.4}
        evaluations = {
            1: self._make_tiered_eval(1, Tier.BRONZE, {'repo/a'}, 50.0),
            2: self._make_tiered_eval(2, Tier.BRONZE, {'repo/b'}, 50.0),
        }

        result = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # Ratio between miners 1 and 2 should be preserved (both scaled by same factor)
        if result.get(1, 0.0) > 0 and result.get(2, 0.0) > 0:
            ratio = result[1] / result[2]
            assert ratio == pytest.approx(1.5, abs=0.01)

    def test_all_zero_normalized_rewards(self):
        """When all normalized rewards are zero, recycle gets 1.0."""
        normalized = {1: 0.0, 2: 0.0}
        evaluations = {
            1: MinerEvaluation(uid=1, hotkey='h1'),
            2: MinerEvaluation(uid=2, hotkey='h2'),
        }

        result = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # When total is 0, recycle should get 1.0
        assert result[RECYCLE_UID] >= 1.0


# ============================================================================
# Integration: Normalization -> Dynamic Emissions Pipeline
# ============================================================================


class TestNormalizeToDynamicEmissionsPipeline:
    """
    Integration tests verifying the full pipeline from normalization
    through dynamic emission scaling.
    """

    def _make_eval_with_score(
        self, uid: int, total_score: float, tier: Tier = None, repos: set = None, token_score: float = 0.0
    ) -> MinerEvaluation:
        """Helper to create a complete MinerEvaluation."""
        evaluation = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        evaluation.total_score = total_score
        evaluation.current_tier = tier
        evaluation.unique_repos_contributed_to = repos or set()
        evaluation.total_token_score = token_score
        return evaluation

    def test_full_pipeline_single_miner(self):
        """Single miner gets normalized to 1.0, then scaled by dynamic emissions."""
        evaluations = {
            1: self._make_eval_with_score(1, 500.0, Tier.GOLD, {'repo/a', 'repo/b'}, 100.0),
        }

        # Step 1: Normalize
        normalized = normalize_rewards_linear(evaluations)
        assert normalized[1] == pytest.approx(1.0)

        # Step 2: Apply dynamic emissions
        final = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # Final should sum to 1.0 (original total)
        assert sum(final.values()) == pytest.approx(1.0, abs=0.001)

    def test_full_pipeline_multiple_miners(self):
        """Multiple miners go through normalization and dynamic emissions correctly."""
        evaluations = {
            1: self._make_eval_with_score(1, 300.0, Tier.GOLD, {'repo/a', 'repo/b', 'repo/c'}, 500.0),
            2: self._make_eval_with_score(2, 200.0, Tier.SILVER, {'repo/d', 'repo/e'}, 200.0),
            3: self._make_eval_with_score(3, 100.0, Tier.BRONZE, {'repo/f'}, 50.0),
        }

        # Step 1: Normalize
        normalized = normalize_rewards_linear(evaluations)
        assert sum(normalized.values()) == pytest.approx(1.0)

        # Step 2: Apply dynamic emissions
        final = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # Total should still be 1.0
        assert sum(final.values()) == pytest.approx(1.0, abs=0.001)

        # RECYCLE_UID should exist with recycled emissions
        assert RECYCLE_UID in final

    def test_pipeline_all_zero_scores(self):
        """Pipeline handles all-zero scores gracefully."""
        evaluations = {
            1: self._make_eval_with_score(1, 0.0),
            2: self._make_eval_with_score(2, 0.0),
        }

        normalized = normalize_rewards_linear(evaluations)

        # All zeros -- should not divide by zero
        for uid in normalized:
            assert normalized[uid] == 0.0

        final = apply_dynamic_emissions_using_network_contributions(normalized, evaluations)

        # Recycle UID should get the full allocation
        assert final.get(RECYCLE_UID, 0.0) >= 1.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
