# The MIT License (MIT)
# Copyright © 2025 Entrius

from dataclasses import dataclass
from typing import Dict, List, TYPE_CHECKING

import bittensor as bt

from gittensor.validator.configurations.tier_config import (
    Tier,
    TIERS,
    TIERS_ORDER,
    get_tier_from_config,
)

if TYPE_CHECKING:
    from gittensor.classes import PullRequest


@dataclass
class TierStats:
    """Statistics for a single tier."""
    merged: int = 0
    closed: int = 0

    @property
    def total(self) -> int:
        return self.merged + self.closed

    @property
    def credibility(self) -> float:
        return self.merged / self.total if self.total > 0 else 0.0


def calculate_tier_stats(
    merged_prs: List["PullRequest"],
    closed_prs: List["PullRequest"],
) -> Dict[Tier, TierStats]:
    """Calculate merged/closed counts per tier."""
    stats: Dict[Tier, TierStats] = {tier: TierStats() for tier in Tier}

    for pr in merged_prs:
        if pr.repository_tier_configuration:
            tier = get_tier_from_config(pr.repository_tier_configuration)
            if tier:
                stats[tier].merged += 1

    for pr in closed_prs:
        if pr.repository_tier_configuration:
            tier = get_tier_from_config(pr.repository_tier_configuration)
            if tier:
                stats[tier].closed += 1

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
            if stats.merged < config.required_merges:
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
        if stats.total == 0:
            continue

        # Check if tier is unlocked
        if not is_tier_unlocked(tier, tier_stats):
            tier_credibility[tier] = 0.0
            bt.logging.warning(
                f"Tier {tier.value}: NOT UNLOCKED - credibility = 0.0 "
                f"(has {stats.merged} merged, {stats.closed} closed but tier requirements not met)"
            )
            continue

        # Check if enough attempts to activate credibility scoring
        if stats.total < config.credibility_activation_attempts:
            tier_credibility[tier] = 1.0
            bt.logging.info(
                f"Tier {tier.value}: {stats.merged}/{stats.total} attempts "
                f"(below {config.credibility_activation_attempts} activation threshold) - credibility = 1.0"
            )
            continue

        # Calculate actual credibility
        credibility = stats.credibility
        tier_credibility[tier] = credibility
        bt.logging.info(f"Tier {tier.value}: {stats.merged}/{stats.total} = {credibility:.2f} credibility")

    return tier_credibility
