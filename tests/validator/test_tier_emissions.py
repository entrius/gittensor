# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for tier-based emission allocation.

Tests the allocate_emissions_by_tier() function which replaces total_score
with tier-weighted emission allocations.

Run tests:
    pytest tests/validator/test_tier_emissions.py -v
"""

import pytest

from gittensor.classes import MinerEvaluation
from gittensor.constants import TIER_EMISSION_SPLITS
from gittensor.validator.configurations.tier_config import Tier, TierStats
from gittensor.validator.evaluation.tier_emissions import allocate_emissions_by_tier


class TestTierEmissionSplitsConstant:
    """Test the TIER_EMISSION_SPLITS constant configuration."""

    def test_splits_sum_to_one(self):
        """Emission splits must sum to 1.0."""
        total = sum(TIER_EMISSION_SPLITS.values())
        assert total == pytest.approx(1.0)

    def test_bronze_allocation(self):
        """Bronze should get 15%."""
        assert TIER_EMISSION_SPLITS['Bronze'] == 0.15

    def test_silver_allocation(self):
        """Silver should get 35%."""
        assert TIER_EMISSION_SPLITS['Silver'] == 0.35

    def test_gold_allocation(self):
        """Gold should get 50%."""
        assert TIER_EMISSION_SPLITS['Gold'] == 0.50

    def test_all_tiers_have_splits(self):
        """All tier names should have emission splits defined."""
        for tier in Tier:
            assert tier.value in TIER_EMISSION_SPLITS


class TestAllocateEmissionsByTierBasic:
    """Test basic tier emission allocation scenarios."""

    def _create_miner_eval(
        self,
        uid: int,
        current_tier: Tier,
        bronze_earned: float = 0.0,
        bronze_collateral: float = 0.0,
        silver_earned: float = 0.0,
        silver_collateral: float = 0.0,
        gold_earned: float = 0.0,
        gold_collateral: float = 0.0,
    ) -> MinerEvaluation:
        """Helper to create a MinerEvaluation with tier stats."""
        eval = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        eval.current_tier = current_tier
        eval.stats_by_tier = {
            Tier.BRONZE: TierStats(earned_score=bronze_earned, collateral_score=bronze_collateral),
            Tier.SILVER: TierStats(earned_score=silver_earned, collateral_score=silver_collateral),
            Tier.GOLD: TierStats(earned_score=gold_earned, collateral_score=gold_collateral),
        }
        eval.total_score = bronze_earned + silver_earned + gold_earned  # Original before allocation
        return eval

    def test_single_miner_gold_tier(self):
        """Single Gold tier miner gets all emissions."""
        miner = self._create_miner_eval(
            uid=1,
            current_tier=Tier.GOLD,
            bronze_earned=100.0,
            silver_earned=200.0,
            gold_earned=300.0,
        )
        evaluations = {1: miner}

        allocate_emissions_by_tier(evaluations)

        # Single miner gets 100% of each tier's allocation
        # Bronze: 100% of 15% = 0.15
        # Silver: 100% of 35% = 0.35
        # Gold: 100% of 50% = 0.50
        assert miner.total_score == pytest.approx(1.0)

    def test_two_miners_same_tier_equal_scores(self):
        """Two miners with equal scores split tier allocations evenly."""
        miner_a = self._create_miner_eval(uid=1, current_tier=Tier.GOLD, bronze_earned=100.0)
        miner_b = self._create_miner_eval(uid=2, current_tier=Tier.GOLD, bronze_earned=100.0)
        evaluations = {1: miner_a, 2: miner_b}

        allocate_emissions_by_tier(evaluations)

        # Each miner gets 50% of Bronze allocation (15% / 2 = 7.5%)
        # Total for each = 0.075 (both only have Bronze scores)
        assert miner_a.total_score == pytest.approx(0.075)
        assert miner_b.total_score == pytest.approx(0.075)
        assert miner_a.total_score + miner_b.total_score == pytest.approx(0.15)

    def test_two_miners_different_scores(self):
        """Two miners with different scores get proportional allocations."""
        # Miner A: 100 Bronze, Miner B: 10 Bronze
        miner_a = self._create_miner_eval(
            uid=1,
            current_tier=Tier.GOLD,
            bronze_earned=100.0,
            silver_earned=600.0,
            gold_earned=300.0,
        )
        miner_b = self._create_miner_eval(
            uid=2,
            current_tier=Tier.GOLD,
            bronze_earned=10.0,
        )
        evaluations = {1: miner_a, 2: miner_b}

        allocate_emissions_by_tier(evaluations)

        # Bronze (15%): A gets 100/110, B gets 10/110
        # Silver (35%): A gets 100% (600/600)
        # Gold (50%): A gets 100% (300/300)
        bronze_a = (100.0 / 110.0) * 0.15
        bronze_b = (10.0 / 110.0) * 0.15
        silver_a = 0.35
        gold_a = 0.50

        assert miner_a.total_score == pytest.approx(bronze_a + silver_a + gold_a)
        assert miner_b.total_score == pytest.approx(bronze_b)

    def test_allocations_sum_to_one(self):
        """All miner allocations should sum to 1.0 when max tier is Gold."""
        miner_a = self._create_miner_eval(
            uid=1,
            current_tier=Tier.GOLD,
            bronze_earned=50.0,
            silver_earned=100.0,
            gold_earned=200.0,
        )
        miner_b = self._create_miner_eval(
            uid=2,
            current_tier=Tier.SILVER,
            bronze_earned=25.0,
            silver_earned=50.0,
        )
        miner_c = self._create_miner_eval(
            uid=3,
            current_tier=Tier.BRONZE,
            bronze_earned=25.0,
        )
        evaluations = {1: miner_a, 2: miner_b, 3: miner_c}

        allocate_emissions_by_tier(evaluations)

        total = miner_a.total_score + miner_b.total_score + miner_c.total_score
        assert total == pytest.approx(1.0)


class TestAllMinersUntiered:
    """Test edge case where no miners have a tier unlocked."""

    def test_all_untiered_get_zero(self):
        """When all miners are untiered, all get total_score = 0."""
        miner_a = MinerEvaluation(uid=1, hotkey='hotkey_1')
        miner_a.current_tier = None
        miner_a.total_score = 100.0  # Had some score before

        miner_b = MinerEvaluation(uid=2, hotkey='hotkey_2')
        miner_b.current_tier = None
        miner_b.total_score = 50.0

        evaluations = {1: miner_a, 2: miner_b}

        allocate_emissions_by_tier(evaluations)

        assert miner_a.total_score == 0.0
        assert miner_b.total_score == 0.0

    def test_empty_evaluations(self):
        """Empty evaluations dict should not cause errors."""
        evaluations = {}
        allocate_emissions_by_tier(evaluations)
        assert len(evaluations) == 0

    def test_none_evaluation_skipped(self):
        """None evaluations in the dict should be skipped."""
        evaluations = {1: None, 2: None}
        allocate_emissions_by_tier(evaluations)
        # Should not raise any errors


class TestMaxTierRedistribution:
    """Test tier redistribution when max tier is below Gold."""

    def _create_miner_eval(
        self,
        uid: int,
        current_tier: Tier,
        bronze_earned: float = 0.0,
        silver_earned: float = 0.0,
        gold_earned: float = 0.0,
    ) -> MinerEvaluation:
        """Helper to create a MinerEvaluation with tier stats."""
        eval = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        eval.current_tier = current_tier
        eval.stats_by_tier = {
            Tier.BRONZE: TierStats(earned_score=bronze_earned),
            Tier.SILVER: TierStats(earned_score=silver_earned),
            Tier.GOLD: TierStats(earned_score=gold_earned),
        }
        return eval

    def test_max_tier_bronze_redistribution(self):
        """When max tier is Bronze, Bronze gets 100% of emissions."""
        miner = self._create_miner_eval(uid=1, current_tier=Tier.BRONZE, bronze_earned=100.0)
        evaluations = {1: miner}

        allocate_emissions_by_tier(evaluations)

        # Bronze gets 100% when it's the only active tier
        assert miner.total_score == pytest.approx(1.0)

    def test_max_tier_silver_redistribution(self):
        """When max tier is Silver, Bronze and Silver split emissions proportionally."""
        # Original: Bronze 15%, Silver 35%, Gold 50%
        # After redistribution: Bronze 15/(15+35) = 30%, Silver 35/(15+35) = 70%
        miner = self._create_miner_eval(
            uid=1, current_tier=Tier.SILVER, bronze_earned=100.0, silver_earned=100.0
        )
        evaluations = {1: miner}

        allocate_emissions_by_tier(evaluations)

        # Single miner gets 100% of active tiers
        # 30% + 70% = 100%
        assert miner.total_score == pytest.approx(1.0)

    def test_two_miners_max_tier_silver(self):
        """Two miners with max tier Silver split redistributed emissions."""
        miner_a = self._create_miner_eval(
            uid=1, current_tier=Tier.SILVER, bronze_earned=75.0, silver_earned=100.0
        )
        miner_b = self._create_miner_eval(
            uid=2, current_tier=Tier.BRONZE, bronze_earned=25.0
        )
        evaluations = {1: miner_a, 2: miner_b}

        allocate_emissions_by_tier(evaluations)

        # Bronze total: 100, Silver total: 100
        # Redistributed: Bronze 30%, Silver 70%
        # A gets: 75/100 * 30% + 100/100 * 70% = 0.225 + 0.70 = 0.925
        # B gets: 25/100 * 30% = 0.075
        assert miner_a.total_score == pytest.approx(0.225 + 0.70)
        assert miner_b.total_score == pytest.approx(0.075)
        assert miner_a.total_score + miner_b.total_score == pytest.approx(1.0)


class TestNegativeNetScore:
    """Test that negative net scores in one tier don't affect others."""

    def _create_miner_eval(
        self,
        uid: int,
        current_tier: Tier,
        bronze_earned: float = 0.0,
        bronze_collateral: float = 0.0,
        silver_earned: float = 0.0,
        silver_collateral: float = 0.0,
    ) -> MinerEvaluation:
        """Helper to create a MinerEvaluation with tier stats."""
        eval = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        eval.current_tier = current_tier
        eval.stats_by_tier = {
            Tier.BRONZE: TierStats(earned_score=bronze_earned, collateral_score=bronze_collateral),
            Tier.SILVER: TierStats(earned_score=silver_earned, collateral_score=silver_collateral),
            Tier.GOLD: TierStats(),
        }
        return eval

    def test_negative_net_score_floors_to_zero(self):
        """Negative net score in one tier floors to 0, doesn't affect other tiers."""
        # Bronze: 50 earned - 100 collateral = -50 -> floors to 0
        # Silver: 100 earned - 20 collateral = 80
        miner = self._create_miner_eval(
            uid=1,
            current_tier=Tier.SILVER,
            bronze_earned=50.0,
            bronze_collateral=100.0,  # More collateral than earned
            silver_earned=100.0,
            silver_collateral=20.0,
        )
        evaluations = {1: miner}

        allocate_emissions_by_tier(evaluations)

        # Bronze net = 0 (floored from -50)
        # Silver net = 80
        # Single miner gets 100% of Silver allocation (70% after redistribution from max=Silver)
        # Bronze allocation is 0% (no positive scores)
        # Total = 0 + 70% = 70%... but wait, Bronze has 0 net so it contributes nothing
        # Actually with redistribution: Silver gets 70%, but Bronze allocation is 0 for this miner
        # The miner's Bronze allocation = 0 (0 net score / 0 total = undefined, treated as 0)
        # The miner's Silver allocation = 80/80 * 70% = 70%
        # Since Bronze total is 0, there's no Bronze allocation to distribute
        # So only Silver 70% goes to this miner
        # Wait - we need to reconsider: if network Bronze total is 0, Bronze allocation can't be distributed
        # This miner should get 70% from Silver only
        assert miner.total_score == pytest.approx(0.70)

    def test_mixed_miners_with_negative_scores(self):
        """Mixed miners where one has negative net in a tier."""
        # Miner A: Bronze 50 earned - 100 collateral = -50 -> 0
        # Miner B: Bronze 100 earned - 0 collateral = 100
        miner_a = self._create_miner_eval(
            uid=1,
            current_tier=Tier.SILVER,
            bronze_earned=50.0,
            bronze_collateral=100.0,
            silver_earned=50.0,
        )
        miner_b = self._create_miner_eval(
            uid=2,
            current_tier=Tier.SILVER,
            bronze_earned=100.0,
            silver_earned=50.0,
        )
        evaluations = {1: miner_a, 2: miner_b}

        allocate_emissions_by_tier(evaluations)

        # Bronze net: A=0 (floored), B=100 -> total=100
        # Silver net: A=50, B=50 -> total=100
        # Redistributed: Bronze 30%, Silver 70%
        # A gets: 0/100 * 30% + 50/100 * 70% = 0 + 0.35 = 0.35
        # B gets: 100/100 * 30% + 50/100 * 70% = 0.30 + 0.35 = 0.65
        assert miner_a.total_score == pytest.approx(0.35)
        assert miner_b.total_score == pytest.approx(0.65)
        assert miner_a.total_score + miner_b.total_score == pytest.approx(1.0)


class TestSingleMinerEdgeCases:
    """Test edge cases with single miners."""

    def _create_miner_eval(
        self, uid: int, current_tier: Tier, **tier_scores
    ) -> MinerEvaluation:
        """Helper to create a MinerEvaluation with tier stats."""
        eval = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        eval.current_tier = current_tier
        eval.stats_by_tier = {
            Tier.BRONZE: TierStats(earned_score=tier_scores.get('bronze', 0.0)),
            Tier.SILVER: TierStats(earned_score=tier_scores.get('silver', 0.0)),
            Tier.GOLD: TierStats(earned_score=tier_scores.get('gold', 0.0)),
        }
        return eval

    def test_single_point_in_gold_gets_50_percent(self):
        """Single miner with 1 point in Gold takes full 50% Gold allocation."""
        miner = self._create_miner_eval(uid=1, current_tier=Tier.GOLD, gold=1.0)
        evaluations = {1: miner}

        allocate_emissions_by_tier(evaluations)

        # Only Gold tier has score, so miner gets 50% (just Gold allocation)
        # Bronze and Silver have 0 network totals, so those allocations don't apply
        assert miner.total_score == pytest.approx(0.50)

    def test_scores_only_in_lower_tiers(self):
        """Miner with Gold tier but scores only in Bronze and Silver."""
        miner = self._create_miner_eval(
            uid=1, current_tier=Tier.GOLD, bronze=100.0, silver=100.0, gold=0.0
        )
        evaluations = {1: miner}

        allocate_emissions_by_tier(evaluations)

        # Bronze: 15%, Silver: 35%, Gold: 0 (no score)
        # Total = 15% + 35% = 50%
        assert miner.total_score == pytest.approx(0.50)


class TestTierProgression:
    """Test tier progression scenarios from the plan."""

    def _create_miner_eval(
        self, uid: int, current_tier: Tier, bronze: float, silver: float = 0.0, gold: float = 0.0
    ) -> MinerEvaluation:
        """Helper to create a MinerEvaluation with tier stats."""
        eval = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}')
        eval.current_tier = current_tier
        eval.stats_by_tier = {
            Tier.BRONZE: TierStats(earned_score=bronze),
            Tier.SILVER: TierStats(earned_score=silver),
            Tier.GOLD: TierStats(earned_score=gold),
        }
        return eval

    def test_pioneer_scenario(self):
        """
        Pioneer (first Gold miner) scenario from plan.

        Before: All miners at Bronze, compete for 100% of emissions
        After: Pioneer unlocks Gold, gets 50% Gold + share of Bronze/Silver
        """
        # Before: Two Bronze miners
        miner_a = self._create_miner_eval(uid=1, current_tier=Tier.BRONZE, bronze=50.0)
        miner_b = self._create_miner_eval(uid=2, current_tier=Tier.BRONZE, bronze=50.0)
        evaluations = {1: miner_a, 2: miner_b}

        allocate_emissions_by_tier(evaluations)

        # Max tier = Bronze, so Bronze gets 100%
        assert miner_a.total_score == pytest.approx(0.50)
        assert miner_b.total_score == pytest.approx(0.50)

    def test_pioneer_unlocks_gold(self):
        """When pioneer unlocks Gold, allocations change dramatically."""
        # Miner A (pioneer): Has all tiers
        # Miner B: Still at Bronze only
        miner_a = self._create_miner_eval(
            uid=1, current_tier=Tier.GOLD, bronze=100.0, silver=200.0, gold=300.0
        )
        miner_b = self._create_miner_eval(uid=2, current_tier=Tier.BRONZE, bronze=100.0)
        evaluations = {1: miner_a, 2: miner_b}

        allocate_emissions_by_tier(evaluations)

        # Bronze: 15% -> A: 50%, B: 50%
        # Silver: 35% -> A: 100%
        # Gold: 50% -> A: 100%
        bronze_a = 0.5 * 0.15
        bronze_b = 0.5 * 0.15
        silver_a = 0.35
        gold_a = 0.50

        assert miner_a.total_score == pytest.approx(bronze_a + silver_a + gold_a)
        assert miner_b.total_score == pytest.approx(bronze_b)
        # B is protected: still gets 7.5% instead of 0%


class TestExampleFromPlan:
    """Test the example calculation from the plan."""

    def test_plan_example(self):
        """
        From plan:
        Network state:
        - Bronze total: 110 (Miner A: 100, Miner B: 10)
        - Silver total: 600 (Miner A: 600)
        - Gold total: 300 (Miner A: 300)

        Expected:
        - Bronze (15%): A gets 100/110 × 0.15 = 0.1364, B gets 10/110 × 0.15 = 0.0136
        - Silver (35%): A gets 600/600 × 0.35 = 0.35
        - Gold (50%): A gets 300/300 × 0.50 = 0.50
        - Miner A: 0.1364 + 0.35 + 0.50 = 0.9864
        - Miner B: 0.0136
        """
        miner_a = MinerEvaluation(uid=1, hotkey='hotkey_1')
        miner_a.current_tier = Tier.GOLD
        miner_a.stats_by_tier = {
            Tier.BRONZE: TierStats(earned_score=100.0),
            Tier.SILVER: TierStats(earned_score=600.0),
            Tier.GOLD: TierStats(earned_score=300.0),
        }

        miner_b = MinerEvaluation(uid=2, hotkey='hotkey_2')
        miner_b.current_tier = Tier.GOLD
        miner_b.stats_by_tier = {
            Tier.BRONZE: TierStats(earned_score=10.0),
            Tier.SILVER: TierStats(earned_score=0.0),
            Tier.GOLD: TierStats(earned_score=0.0),
        }

        evaluations = {1: miner_a, 2: miner_b}

        allocate_emissions_by_tier(evaluations)

        # Expected calculations from plan
        bronze_a = (100.0 / 110.0) * 0.15
        bronze_b = (10.0 / 110.0) * 0.15
        silver_a = 0.35
        gold_a = 0.50

        assert miner_a.total_score == pytest.approx(bronze_a + silver_a + gold_a, abs=0.0001)
        assert miner_b.total_score == pytest.approx(bronze_b, abs=0.0001)

        # Verify sum equals 1.0
        total = miner_a.total_score + miner_b.total_score
        assert total == pytest.approx(1.0, abs=0.0001)


class TestMissingTierStats:
    """Test handling of missing or incomplete tier stats."""

    def test_missing_tier_stats_treated_as_zero(self):
        """Missing tier stats should be treated as zero contribution."""
        miner = MinerEvaluation(uid=1, hotkey='hotkey_1')
        miner.current_tier = Tier.BRONZE
        # Only Bronze has stats
        miner.stats_by_tier = {
            Tier.BRONZE: TierStats(earned_score=100.0),
            # Silver and Gold not present
        }

        evaluations = {1: miner}

        allocate_emissions_by_tier(evaluations)

        # Max tier = Bronze, so Bronze gets 100%
        assert miner.total_score == pytest.approx(1.0)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
