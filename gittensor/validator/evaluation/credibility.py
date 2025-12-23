# The MIT License (MIT)
# Copyright © 2025 Entrius

from dataclasses import dataclass
from typing import Dict, List, TYPE_CHECKING

import bittensor as bt

from gittensor.validator.configurations.tier_config import (
    Tier,
    TierStats,
    TIERS,
    TIERS_ORDER,
    get_tier_from_config,
)

if TYPE_CHECKING:
    from gittensor.classes import PullRequest


def calculate_tier_stats(
    merged_prs: List["PullRequest"],
    closed_prs: List["PullRequest"],
    open_prs: List["PullRequest"] = [],
    include_scoring_details: bool = False,
) -> Dict[Tier, TierStats]:
    """Calculate merged/closed counts per tier."""
    stats: Dict[Tier, TierStats] = {tier: TierStats() for tier in Tier}

    for pr in merged_prs:
        if pr.repository_tier_configuration:
            tier = get_tier_from_config(pr.repository_tier_configuration)
            if tier:
                stats[tier].merged_count += 1
                if include_scoring_details:
                    stats[tier].earned_score += pr.earned_score

    for pr in closed_prs:
        if pr.repository_tier_configuration:
            tier = get_tier_from_config(pr.repository_tier_configuration)
            if tier:
                stats[tier].closed_count += 1

    for pr in open_prs:
        if pr.repository_tier_configuration:
            tier = get_tier_from_config(pr.repository_tier_configuration)
            if tier:
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
                return False

        if config.required_credibility is not None:
            if stats.credibility < config.required_credibility:
                return False

    return True


def calculate_credibility_per_tier(
    merged_prs: List["PullRequest"],
    closed_prs: List["PullRequest"],
) -> Dict[Tier, float]:
    """
    Calculate credibility for each tier, enforcing tier progression.

    Returns dict of tier -> credibility (0.0 if tier not unlocked, else merged/total ratio).
    """
    tier_stats = calculate_tier_stats(merged_prs, closed_prs)
    tier_credibility: Dict[Tier, float] = {}

    for tier in Tier:
        stats = tier_stats[tier]
        config = TIERS[tier]

        # Skip tiers with no activity
        if stats.total_attempts == 0:
            continue

        # Check if tier is unlocked
        if not is_tier_unlocked(tier, tier_stats):
            tier_credibility[tier] = 0.0
            bt.logging.warning(
                f"Tier {tier.value}: NOT UNLOCKED - credibility = 0.0 "
                f"(has {stats.merged_count} merged, {stats.closed_count} closed but tier requirements not met)"
            )
            continue

        # Check if enough attempts to activate credibility scoring
        if stats.total_attempts < config.credibility_activation_attempts:
            tier_credibility[tier] = 1.0
            bt.logging.info(
                f"Tier {tier.value}: {stats.merged_count}/{stats.total_attempts} attempts "
                f"(below {config.credibility_activation_attempts} activation threshold) - credibility = 1.0"
            )
            continue

        # Calculate actual credibility
        credibility = stats.credibility
        tier_credibility[tier] = credibility
        bt.logging.info(f"Tier {tier.value}: {stats.merged_count}/{stats.total_attempts} = {credibility:.2f} credibility")

    return tier_credibility
