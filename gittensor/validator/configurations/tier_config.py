from dataclasses import dataclass
from enum import Enum
from typing import Optional

from gittensor.constants import (
    DEFAULT_COLLATERAL_PERCENT,
    CREDIBILITY_THRESHOLD,
    DEFAULT_MERGED_PR_BASE_SCORE,
    MAX_CONTRIBUTION_BONUS_SCORE,
    DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS,
)


class Tier(str, Enum):
    LOW = "Low"
    LOWER_MID = "Lower-Mid"
    MIDDLE = "Middle"
    UPPER_MID = "Upper-Mid"
    HIGH = "High"


TIER_DEFAULTS = {
    "merged_pr_base_score": DEFAULT_MERGED_PR_BASE_SCORE,
    "contribution_score_for_full_bonus": DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS,
    "contribution_score_max_bonus": MAX_CONTRIBUTION_BONUS_SCORE,
    "credibility_activation_attempts": CREDIBILITY_THRESHOLD,
    "open_pr_collateral_percentage": DEFAULT_COLLATERAL_PERCENT,
}


@dataclass(frozen=True)
class TierConfig:
    # Next tier unlock requirements (None for top tier)
    required_merges: Optional[int]
    required_credibility: Optional[float]
    
    # Tier-specific scaling
    credibility_scalar: int
    
    # Defaults (can override per-tier if needed)
    merged_pr_base_score: int = TIER_DEFAULTS["merged_pr_base_score"]
    contribution_score_for_full_bonus: int = TIER_DEFAULTS["contribution_score_for_full_bonus"]
    contribution_score_max_bonus: int = TIER_DEFAULTS["contribution_score_max_bonus"]
    credibility_activation_attempts: int = TIER_DEFAULTS["credibility_activation_attempts"]
    open_pr_collateral_percentage: int = TIER_DEFAULTS["open_pr_collateral_percentage"]


TIERS: dict[Tier, TierConfig] = {
    #                              merges  credibility  scalar  (requirements to MAINTAIN this tier)
    Tier.LOW:       TierConfig(    None,   None,        1      ),  # always unlocked
    Tier.LOWER_MID: TierConfig(    3,      0.40,        2      ),
    Tier.MIDDLE:    TierConfig(    3,      0.50,        3      ),
    Tier.UPPER_MID: TierConfig(    3,      0.60,        4      ),
    Tier.HIGH:      TierConfig(    5,      0.70,        5      ),
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