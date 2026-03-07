from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Dict, Optional

import bittensor as bt

from gittensor.constants import (
    DEFAULT_COLLATERAL_PERCENT,
    DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS,
    DEFAULT_MERGED_PR_BASE_SCORE,
    MAX_CONTRIBUTION_BONUS,
    TIER_EMISSION_SPLITS,
)

if TYPE_CHECKING:
    from gittensor.classes import MinerEvaluation


@dataclass
class TierStats:
    """Statistics for a single tier."""

    merged_count: int = 0
    closed_count: int = 0
    open_count: int = 0

    unique_repo_contribution_count: int = 0
    # Unique repos that meet a min token score threshold
    qualified_unique_repo_count: int = 0

    # Included as scoring details at the tier level
    earned_score: float = 0.0
    collateral_score: float = 0.0

    # Token scoring breakdown for this tier
    token_score: float = 0.0
    structural_count: int = 0
    structural_score: float = 0.0
    leaf_count: int = 0
    leaf_score: float = 0.0

    @property
    def total_attempts(self) -> int:
        return self.merged_count + self.closed_count

    @property
    def total_prs(self) -> int:
        return self.merged_count + self.closed_count + self.open_count

    @property
    def credibility(self) -> float:
        return self.merged_count / self.total_attempts if self.total_attempts > 0 else 0.0


class Tier(str, Enum):
    BRONZE = 'Bronze'
    SILVER = 'Silver'
    GOLD = 'Gold'


TIER_DEFAULTS = {
    'merged_pr_base_score': DEFAULT_MERGED_PR_BASE_SCORE,
    'contribution_score_for_full_bonus': DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS,
    'contribution_score_max_bonus': MAX_CONTRIBUTION_BONUS,
    'open_pr_collateral_percentage': DEFAULT_COLLATERAL_PERCENT,
}


@dataclass(frozen=True)
class TierConfig:
    required_credibility: Optional[float]
    required_min_token_score: Optional[float]  # Minimum total token score to unlock tier
    # Unique repos with min token score requirement (both must be set or both None)
    required_unique_repos_count: Optional[int]  # Number of unique repos needed
    required_min_token_score_per_repo: Optional[float]  # Min token score each repo must have

    # Tier-specific scaling
    credibility_scalar: int

    # Defaults (can override per-tier if needed)
    merged_pr_base_score: int = TIER_DEFAULTS['merged_pr_base_score']
    contribution_score_for_full_bonus: int = TIER_DEFAULTS['contribution_score_for_full_bonus']
    contribution_score_max_bonus: int = TIER_DEFAULTS['contribution_score_max_bonus']
    open_pr_collateral_percentage: int = TIER_DEFAULTS['open_pr_collateral_percentage']


TIERS: dict[Tier, TierConfig] = {
    Tier.BRONZE: TierConfig(
        required_credibility=0.70,
        required_min_token_score=None,
        required_unique_repos_count=3,
        required_min_token_score_per_repo=5.0,  # At least n initial unique repos must have at least x token score
        credibility_scalar=1.0,
    ),
    Tier.SILVER: TierConfig(
        required_credibility=0.65,
        required_min_token_score=300.0,  # Minimum total token score for Silver unlock
        required_unique_repos_count=3,
        required_min_token_score_per_repo=89.0,  # At least n repos must have at least x token score
        credibility_scalar=1.5,
    ),
    Tier.GOLD: TierConfig(
        required_credibility=0.60,
        required_min_token_score=500.0,  # Minimum total token score for Gold unlock
        required_unique_repos_count=3,
        required_min_token_score_per_repo=144.0,  # At least n unique repos must have at least x token score
        credibility_scalar=2.0,
    ),
}
TIERS_ORDER: list[Tier] = list(TIERS.keys())


def get_next_tier(current: Tier) -> Optional[Tier]:
    """Returns the next tier, or None if already at top."""
    idx = TIERS_ORDER.index(current)
    if idx + 1 < len(TIERS_ORDER):
        return TIERS_ORDER[idx + 1]
    return None


def get_tier_from_config(tier_config: TierConfig) -> Optional[Tier]:
    """Reverse lookup tier from TierConfig."""
    for tier, config in TIERS.items():
        if config == tier_config:
            return tier
    return None


def allocate_emissions_by_tier(miner_evaluations: Dict[int, MinerEvaluation]) -> None:
    """
    Replace each miner's total_score with tier-weighted emission allocations.
    15% of emissions will go to Bronze tier contributions, 35% to silver, and 50% to gold.

    Algorithm:
    1. Calculate net score per miner per tier: max(0, earned - collateral)
    2. Sum network totals per tier
    3. Determine max tier reached across all miners
    4. Redistribute allocations from inactive tiers to active tiers
    5. Calculate each miner's share within each tier
    6. Replace total_score with sum of tier allocations

    Args:
        miner_evaluations: Dict mapping uid to MinerEvaluation (modified in place)

    Note: MinerEvaluation is imported via TYPE_CHECKING for type hints only
          (avoids circular import with gittensor.classes).
    """
    # Step 1 & 2: Calculate net scores and network totals per tier
    network_tier_totals: Dict[Tier, float] = {tier: 0.0 for tier in TIERS_ORDER}
    miner_net_scores: Dict[int, Dict[Tier, float]] = {}

    max_tier: Tier = None

    for uid, evaluation in miner_evaluations.items():
        if not evaluation or evaluation.current_tier is None:
            continue

        # Track the highest tier reached across all miners
        if max_tier is None:
            max_tier = evaluation.current_tier
        elif TIERS_ORDER.index(evaluation.current_tier) > TIERS_ORDER.index(max_tier):
            max_tier = evaluation.current_tier

        miner_net_scores[uid] = {}

        for tier in TIERS_ORDER:
            stats = evaluation.stats_by_tier.get(tier)
            if stats is None:
                miner_net_scores[uid][tier] = 0.0
                continue

            # Net score floors at 0 - negative in one tier doesn't affect others
            net_score = max(0.0, stats.earned_score - stats.collateral_score)
            miner_net_scores[uid][tier] = net_score
            network_tier_totals[tier] += net_score

    # If no miners have a tier, all scores remain 0
    if max_tier is None:
        bt.logging.info('Tier emissions: No tiered miners found, all scores set to 0')
        for evaluation in miner_evaluations.values():
            if evaluation:
                evaluation.total_score = 0.0
        return

    # Step 3 & 4: Determine active tiers and calculate final percentages
    max_tier_idx = TIERS_ORDER.index(max_tier)
    active_tiers = TIERS_ORDER[: max_tier_idx + 1]

    # Calculate sum of active tier percentages for redistribution
    active_pct_sum = sum(TIER_EMISSION_SPLITS[tier.value] for tier in active_tiers)

    # Final percentages after redistribution. I.e, if gold is not yet unlocked, its 50% allocation will be
    # proportionally distributed to the bronze/silver tiers.
    final_tier_pcts: Dict[Tier, float] = {}
    for tier in TIERS_ORDER:
        if tier in active_tiers:
            original_pct = TIER_EMISSION_SPLITS[tier.value]
            final_tier_pcts[tier] = original_pct / active_pct_sum
        else:
            final_tier_pcts[tier] = 0.0

    # Log tier allocation summary
    bt.logging.info('')
    bt.logging.info('=' * 50)
    bt.logging.info('Tier-Based Emission Allocation')
    bt.logging.info('=' * 50)
    bt.logging.info(f'Max tier reached: {max_tier.value}')
    bt.logging.info(f'Active tiers: {[t.value for t in active_tiers]}')
    bt.logging.info('Network totals per tier:')
    for tier in TIERS_ORDER:
        status = 'active' if tier in active_tiers else 'redistributed'
        bt.logging.info(
            f'  {tier.value}: {network_tier_totals[tier]:.2f} total | '
            f'{final_tier_pcts[tier] * 100:.1f}% allocation ({status})'
        )

    # Step 5 & 6: Calculate miner allocations and replace total_score
    bt.logging.info('')
    bt.logging.info('Per-miner allocations:')

    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        if uid not in miner_net_scores:
            evaluation.total_score = 0.0
            continue

        total_allocation = 0.0
        tier_allocations: Dict[Tier, float] = {}

        for tier in TIERS_ORDER:
            net_score = miner_net_scores[uid].get(tier, 0.0)
            network_total = network_tier_totals[tier]
            tier_pct = final_tier_pcts[tier]

            if network_total > 0 and net_score > 0:
                miner_share = net_score / network_total
                tier_allocation = miner_share * tier_pct
            else:
                tier_allocation = 0.0

            tier_allocations[tier] = tier_allocation
            total_allocation += tier_allocation

        evaluation.total_score = total_allocation

        # Log non-zero allocations
        if total_allocation > 0:
            alloc_parts = [
                f'{tier.value}={tier_allocations[tier]:.4f}' for tier in TIERS_ORDER if tier_allocations[tier] > 0
            ]
            bt.logging.info(f'  UID {uid}: {" + ".join(alloc_parts)} = {total_allocation:.4f}')

    bt.logging.info('=' * 50)
