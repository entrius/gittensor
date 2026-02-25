# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tier-based emission allocation module.

Implements fixed tier-based emission splits (Bronze 15%, Silver 35%, Gold 50%)
so miners compete for rewards within their tier rather than globally.
"""

from typing import Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.constants import TIER_EMISSION_SPLITS
from gittensor.validator.configurations.tier_config import TIERS_ORDER, Tier


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
