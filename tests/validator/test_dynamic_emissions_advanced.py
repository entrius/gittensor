# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Comprehensive tests for dynamic emissions module.

Tests _exponential_unlock_scalar(), get_network_totals(), and
apply_dynamic_emissions_using_network_contributions() with focus on
boundary values, edge cases, and mathematical correctness.

Run tests:
    pytest tests/validator/test_dynamic_emissions_advanced.py -v
"""

import numpy as np
import pytest

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    RECYCLE_UID,
    TOKEN_SCORE_MAX_RECYCLE,
    TOKEN_SCORE_RECYCLE_DECAY_RATE,
    UNIQUE_REPOS_MAX_RECYCLE,
    UNIQUE_REPOS_RECYCLE_DECAY_RATE,
)
from gittensor.validator.configurations.tier_config import Tier, TierStats
from gittensor.validator.evaluation.dynamic_emissions import (
    _exponential_unlock_scalar,
    apply_dynamic_emissions_using_network_contributions,
    get_network_totals,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_eval(
    uid: int,
    current_tier: Tier | None = Tier.BRONZE,
    total_token_score: float = 0.0,
    unique_repos: set[str] | None = None,
) -> MinerEvaluation:
    """Create a MinerEvaluation with the fields relevant to dynamic emissions."""
    ev = MinerEvaluation(uid=uid, hotkey=f"hotkey_{uid}")
    ev.current_tier = current_tier
    ev.total_token_score = total_token_score
    ev.unique_repos_contributed_to = unique_repos or set()
    return ev


# ===========================================================================
# _exponential_unlock_scalar
# ===========================================================================


class TestExponentialUnlockScalar:
    """Tests for the exponential unlock curve: min(1.0, (1-max_recycle) + max_recycle*(1-exp(-decay*v)))."""

    # --- Boundary: value = 0 ---

    def test_zero_value_returns_one_minus_max_recycle(self):
        """At value=0 the exponential term is 0, so scalar = 1 - max_recycle."""
        result = _exponential_unlock_scalar(0.0, max_recycle=0.8, decay_rate=0.005)
        assert result == pytest.approx(0.2)

    def test_zero_value_with_full_recycle(self):
        """max_recycle=1.0, value=0 → scalar=0 (all emissions recycled)."""
        assert _exponential_unlock_scalar(0.0, 1.0, 0.01) == pytest.approx(0.0)

    def test_zero_value_with_no_recycle(self):
        """max_recycle=0.0, value=0 → scalar=1.0 (nothing to recycle)."""
        assert _exponential_unlock_scalar(0.0, 0.0, 0.01) == pytest.approx(1.0)

    # --- Boundary: very large value ---

    def test_very_large_value_saturates_to_one(self):
        """Extremely large value drives exp term to 0, scalar → 1.0."""
        result = _exponential_unlock_scalar(1e12, 0.8, 0.005)
        assert result == pytest.approx(1.0)

    # --- Cap at 1.0 ---

    def test_cap_at_one(self):
        """Result is capped at 1.0 even if the formula would exceed it (shouldn't normally, but verify min())."""
        # With max_recycle < 1 and positive value the formula approaches 1 from below.
        result = _exponential_unlock_scalar(1e6, 0.5, 10.0)
        assert result == 1.0

    # --- Monotonicity ---

    def test_monotonically_increasing(self):
        """Scalar should increase as value increases."""
        values = [0, 10, 50, 200, 1000, 10000]
        scalars = [_exponential_unlock_scalar(v, 0.8, 0.005) for v in values]
        for i in range(len(scalars) - 1):
            assert scalars[i] <= scalars[i + 1], f"Not monotonic at index {i}"

    # --- Specific math verification ---

    def test_known_value_manual_calculation(self):
        """Verify against hand-calculated result for a specific input."""
        # value=100, max_recycle=0.8, decay_rate=0.005
        # expected = min(1.0, 0.2 + 0.8 * (1 - exp(-0.5)))
        expected = min(1.0, 0.2 + 0.8 * (1 - np.exp(-0.5)))
        result = _exponential_unlock_scalar(100.0, 0.8, 0.005)
        assert result == pytest.approx(expected)

    def test_with_production_constants_repos(self):
        """Verify scalar using production UNIQUE_REPOS constants at value=50."""
        expected = min(
            1.0,
            (1 - UNIQUE_REPOS_MAX_RECYCLE)
            + UNIQUE_REPOS_MAX_RECYCLE * (1 - np.exp(-UNIQUE_REPOS_RECYCLE_DECAY_RATE * 50)),
        )
        assert _exponential_unlock_scalar(50, UNIQUE_REPOS_MAX_RECYCLE, UNIQUE_REPOS_RECYCLE_DECAY_RATE) == pytest.approx(expected)

    def test_with_production_constants_token_score(self):
        """Verify scalar using production TOKEN_SCORE constants at value=100000."""
        expected = min(
            1.0,
            (1 - TOKEN_SCORE_MAX_RECYCLE)
            + TOKEN_SCORE_MAX_RECYCLE * (1 - np.exp(-TOKEN_SCORE_RECYCLE_DECAY_RATE * 100000)),
        )
        assert _exponential_unlock_scalar(100000, TOKEN_SCORE_MAX_RECYCLE, TOKEN_SCORE_RECYCLE_DECAY_RATE) == pytest.approx(expected)

    # --- Edge: decay_rate = 0 ---

    def test_zero_decay_rate_always_returns_base(self):
        """With decay_rate=0 the exponential term is always 0 regardless of value."""
        assert _exponential_unlock_scalar(999999, 0.8, 0.0) == pytest.approx(0.2)


# ===========================================================================
# get_network_totals
# ===========================================================================


class TestGetNetworkTotals:
    """Tests for get_network_totals — counting only tiered miners."""

    def test_empty_evaluations(self):
        """Empty dict returns zeros."""
        repos, score = get_network_totals({})
        assert repos == 0
        assert score == 0.0

    def test_only_untiered_miners_excluded(self):
        """Miners with current_tier=None are excluded from totals."""
        ev = _make_eval(1, current_tier=None, total_token_score=500.0, unique_repos={"repo_a"})
        repos, score = get_network_totals({1: ev})
        assert repos == 0
        assert score == 0.0

    def test_tiered_miner_counted(self):
        """A tiered miner's repos and token score are counted."""
        ev = _make_eval(1, Tier.BRONZE, total_token_score=100.0, unique_repos={"repo_a", "repo_b"})
        repos, score = get_network_totals({1: ev})
        assert repos == 2
        assert score == pytest.approx(100.0)

    def test_mixed_tiered_and_untiered(self):
        """Only tiered miners contribute to totals."""
        evals = {
            1: _make_eval(1, Tier.GOLD, 300.0, {"r1", "r2"}),
            2: _make_eval(2, None, 999.0, {"r3"}),
            3: _make_eval(3, Tier.SILVER, 200.0, {"r2", "r4"}),
        }
        repos, score = get_network_totals(evals)
        # unique repos: r1, r2, r4 (r3 excluded because miner 2 is untiered)
        assert repos == 3
        assert score == pytest.approx(500.0)

    def test_duplicate_repos_across_miners_counted_once(self):
        """Same repo contributed by multiple tiered miners is counted once."""
        evals = {
            1: _make_eval(1, Tier.BRONZE, 10.0, {"shared_repo"}),
            2: _make_eval(2, Tier.BRONZE, 20.0, {"shared_repo"}),
        }
        repos, score = get_network_totals(evals)
        assert repos == 1
        assert score == pytest.approx(30.0)

    def test_miner_with_no_repos(self):
        """Tiered miner with empty repos still contributes token score."""
        ev = _make_eval(1, Tier.BRONZE, total_token_score=50.0, unique_repos=set())
        repos, score = get_network_totals({1: ev})
        assert repos == 0
        assert score == pytest.approx(50.0)

    def test_all_tiers_counted(self):
        """Bronze, Silver, and Gold miners all contribute."""
        evals = {
            1: _make_eval(1, Tier.BRONZE, 10.0, {"a"}),
            2: _make_eval(2, Tier.SILVER, 20.0, {"b"}),
            3: _make_eval(3, Tier.GOLD, 30.0, {"c"}),
        }
        repos, score = get_network_totals(evals)
        assert repos == 3
        assert score == pytest.approx(60.0)


# ===========================================================================
# apply_dynamic_emissions_using_network_contributions
# ===========================================================================


class TestApplyDynamicEmissions:
    """Tests for apply_dynamic_emissions_using_network_contributions."""

    def test_empty_rewards_returns_empty(self):
        """Empty normalized_rewards returns empty dict."""
        result = apply_dynamic_emissions_using_network_contributions({}, {})
        assert result == {}

    def test_recycle_uid_gets_recycled_amount(self):
        """Recycled emissions are allocated to RECYCLE_UID."""
        rewards = {1: 0.5, 2: 0.5}
        evals = {}  # No tiered miners → max recycle
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)
        assert RECYCLE_UID in result
        assert result[RECYCLE_UID] > 0

    def test_total_preserved_with_recycle(self):
        """Sum of scaled rewards + recycle should equal original total."""
        rewards = {1: 0.4, 2: 0.6}
        evals = {
            1: _make_eval(1, Tier.BRONZE, 100.0, {"r1"}),
        }
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)
        original_total = 1.0
        new_total = sum(result.values())
        assert new_total == pytest.approx(original_total)

    def test_zero_network_contributions_maximum_recycle(self):
        """With no tiered miners, scalar is at minimum → maximum recycling."""
        rewards = {1: 1.0}
        evals = {}  # no miners
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)

        # Both scalars at value=0: scalar = 1 - max_recycle = 0.2
        # final_scalar = (0.2 + 0.2) / 2 = 0.2
        expected_scalar = 0.2
        assert result[1] == pytest.approx(1.0 * expected_scalar)
        assert result[RECYCLE_UID] == pytest.approx(1.0 * (1 - expected_scalar))

    def test_high_contributions_low_recycle(self):
        """High network contributions drive scalar toward 1.0, minimizing recycling."""
        rewards = {1: 1.0}
        evals = {
            1: _make_eval(1, Tier.GOLD, total_token_score=1e8, unique_repos={f"r{i}" for i in range(5000)}),
        }
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)

        # Scalar should be very close to 1.0
        assert result[1] == pytest.approx(1.0, abs=0.01)
        assert result[RECYCLE_UID] == pytest.approx(0.0, abs=0.01)

    def test_scaling_preserves_relative_proportions(self):
        """Scaling should preserve relative proportions between miners."""
        rewards = {1: 0.7, 2: 0.3}
        evals = {1: _make_eval(1, Tier.BRONZE, 50.0, {"r1"})}
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)

        # Exclude RECYCLE_UID for proportion check
        miner_rewards = {k: v for k, v in result.items() if k != RECYCLE_UID}
        total_miner = sum(miner_rewards.values())
        if total_miner > 0:
            assert miner_rewards[1] / total_miner == pytest.approx(0.7)
            assert miner_rewards[2] / total_miner == pytest.approx(0.3)

    def test_single_miner_single_reward(self):
        """Single miner: reward is scaled, remainder goes to recycle."""
        rewards = {5: 1.0}
        evals = {5: _make_eval(5, Tier.SILVER, 200.0, {"a", "b"})}
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)

        assert 5 in result
        assert RECYCLE_UID in result
        assert result[5] + result[RECYCLE_UID] == pytest.approx(1.0)
        assert result[5] < 1.0  # Some must be recycled at moderate contribution levels

    def test_recycle_uid_in_original_rewards_accumulated(self):
        """If RECYCLE_UID already has a reward, recycled amount is added to it."""
        rewards = {RECYCLE_UID: 0.1, 1: 0.9}
        evals = {}
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)

        # RECYCLE_UID gets its scaled portion + all recycled emissions
        # scalar ≈ 0.2 → recycled = 1.0 * 0.8 = 0.8
        # RECYCLE_UID scaled = 0.1 * 0.2 = 0.02, then + 0.8 = 0.82
        expected_scalar = 0.2
        expected_recycle = 0.1 * expected_scalar + 1.0 * (1 - expected_scalar)
        assert result[RECYCLE_UID] == pytest.approx(expected_recycle)

    def test_all_zero_rewards(self):
        """All zero rewards: recycled amount is 0, but recycle gets 1.0 floor."""
        rewards = {1: 0.0, 2: 0.0}
        evals = {}
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)

        # total_original = 0, so total_recycled = 0
        # But code has: max(total_recycled, 1.0 if total_original <= 0 else 0.0)
        # total_original = 0 → 1.0 floor applies
        assert result[RECYCLE_UID] == pytest.approx(1.0)

    def test_all_zero_rewards_with_contributions(self):
        """Zero rewards with tiered miners: recycle still gets 1.0 floor."""
        rewards = {1: 0.0}
        evals = {1: _make_eval(1, Tier.GOLD, 1000.0, {"r"})}
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)
        assert result[RECYCLE_UID] == pytest.approx(1.0)

    def test_many_miners_rewards_consistency(self):
        """With many miners, sum of all output rewards equals original total."""
        rewards = {i: 1.0 / 50 for i in range(1, 51)}
        evals = {
            i: _make_eval(i, Tier.BRONZE, float(i * 10), {f"r{i}"})
            for i in range(1, 51)
        }
        result = apply_dynamic_emissions_using_network_contributions(rewards, evals)
        assert sum(result.values()) == pytest.approx(1.0)

    def test_untiered_miners_dont_affect_scalar(self):
        """Untiered miners' contributions don't influence the emission scalar."""
        rewards = {1: 0.5, 2: 0.5}
        # Miner 1 is tiered with modest contributions
        evals_with_untiered = {
            1: _make_eval(1, Tier.BRONZE, 50.0, {"r1"}),
            2: _make_eval(2, None, 999999.0, {f"r{i}" for i in range(10000)}),
        }
        evals_without = {
            1: _make_eval(1, Tier.BRONZE, 50.0, {"r1"}),
        }
        result_with = apply_dynamic_emissions_using_network_contributions(rewards, evals_with_untiered)
        result_without = apply_dynamic_emissions_using_network_contributions(rewards, evals_without)

        # Same scalar → same miner rewards
        assert result_with[1] == pytest.approx(result_without[1])
        assert result_with[2] == pytest.approx(result_without[2])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
