# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import TYPE_CHECKING, Dict, List

import bittensor as bt

from gittensor.validator.configurations.tier_config import (
    TIERS,
    TIERS_ORDER,
    Tier,
    TierStats,
    get_tier_from_config,
)

if TYPE_CHECKING:
    from gittensor.classes import PullRequest


def calculate_tier_stats(
    merged_prs: List['PullRequest'],
    closed_prs: List['PullRequest'],
    open_prs: List['PullRequest'] = [],
    include_scoring_details: bool = False,
) -> Dict[Tier, TierStats]:
    """Calculate merged/closed counts per tier."""
    stats: Dict[Tier, TierStats] = {tier: TierStats() for tier in Tier}

    def get_tier(pr: 'PullRequest') -> Tier | None:
        if pr.repository_tier_configuration:
            return get_tier_from_config(pr.repository_tier_configuration)
        return None

    for pr in merged_prs:
        if (tier := get_tier(pr)) and pr.earned_score > 0:
            stats[tier].merged_count += 1
            if include_scoring_details:
                stats[tier].earned_score += pr.earned_score

    for pr in closed_prs:
        if tier := get_tier(pr):
            stats[tier].closed_count += 1

    for pr in open_prs:
        if tier := get_tier(pr):
            stats[tier].open_count += 1
            if include_scoring_details:
                stats[tier].collateral_score += pr.collateral_score

    return stats


def is_tier_unlocked(tier: Tier, tier_stats: Dict[Tier, TierStats]) -> bool:
    """
    Check if a tier is unlocked by verifying this tier and all below meet their own requirements.

    Each tier's required_merges/required_credibility defines what's needed to maintain THAT tier.
    """
    tier_idx = TIERS_ORDER.index(tier)

    for i in range(tier_idx + 1):  # include current tier
        check_tier = TIERS_ORDER[i]
        config = TIERS[check_tier]
        stats = tier_stats[check_tier]

        if config.required_merges is not None:
            if stats.merged_count < config.required_merges:
                bt.logging.info(
                    f'{tier.value} locked: {check_tier.value} needs {config.required_merges} merges, has {stats.merged_count}'
                )
                return False

        if config.required_credibility is not None:
            if stats.credibility < config.required_credibility:
                bt.logging.info(
                    f'{tier.value} locked: {check_tier.value} needs {config.required_credibility:.2f} credibility, has {stats.credibility:.2f}'
                )
                return False

    return True


def calculate_credibility_per_tier(
    merged_prs: List['PullRequest'],
    closed_prs: List['PullRequest'],
) -> Dict[Tier, float]:
    """
    Calculate credibility for each tier, enforcing tier progression.

    Returns dict of tier -> credibility (0.0 if tier not unlocked, else merged/total ratio).
    """
    tier_stats = calculate_tier_stats(merged_prs, closed_prs)
    tier_credibility: Dict[Tier, float] = {}
    tier_display_parts = []

    for tier in Tier:
        stats: TierStats = tier_stats[tier]

        # Check if tier is unlocked (includes checking lower tiers)
        tier_unlocked = is_tier_unlocked(tier, tier_stats)

        # No activity in this tier
        if stats.total_attempts == 0:
            tier_display_parts.append(f'{tier.value}: LOCKED')
            continue

        # Has activity but tier not unlocked
        if not tier_unlocked:
            tier_credibility[tier] = 0.0
            tier_display_parts.append(f'{tier.value}: LOCKED')
            continue

        # Calculate actual credibility
        credibility = stats.credibility
        tier_credibility[tier] = credibility
        tier_display_parts.append(f'{tier.value}: {stats.merged_count}/{stats.total_attempts} ({credibility:.2f})')

    bt.logging.info(f'Credibility: {" | ".join(tier_display_parts)}')

    return tier_credibility
