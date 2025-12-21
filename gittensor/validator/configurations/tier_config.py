from dataclasses import dataclass
from enum import Enum
from typing import Optional

from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_THRESHOLD,
    DEFAULT_COLLATERAL_PERCENT,
    CREDIBILITY_THRESHOLD
)


class Tier(str, Enum):
    LOW = "Low"
    LOWER_MID = "Lower-Mid"
    MIDDLE = "Middle"
    UPPER_MID = "Upper-Mid"
    HIGH = "High"


TIER_DEFAULTS = {
    "credibility_activation_attempts": CREDIBILITY_THRESHOLD,
    "open_pr_collateral_percentage": DEFAULT_COLLATERAL_PERCENT,
    "open_prs_allowed": EXCESSIVE_PR_PENALTY_THRESHOLD,
}


@dataclass(frozen=True)
class TierConfig:
    # Unlock requirements (None for top tier)
    required_merges: Optional[int]
    required_credibility: Optional[float]
    
    # Tier-specific scaling
    credibility_scalar: int
    
    # Defaults (can override per-tier if needed)
    credibility_activation_attempts: int = TIER_DEFAULTS["credibility_activation_attempts"]
    open_pr_collateral_percentage: int = TIER_DEFAULTS["open_pr_collateral_percentage"]
    open_prs_allowed: int = TIER_DEFAULTS["open_prs_allowed"]

    @property
    def has_next_tier(self) -> bool:
        return self.required_merges is not None


TIERS: dict[Tier, TierConfig] = {
    #                              merges  credibility  scalar
    Tier.LOW:       TierConfig(    3,      0.40,        1      ),
    Tier.LOWER_MID: TierConfig(    3,      0.50,        2      ),
    Tier.MIDDLE:    TierConfig(    3,      0.60,        3      ),
    Tier.UPPER_MID: TierConfig(    5,      0.70,        4      ),
    Tier.HIGH:      TierConfig(    None,   None,        5      ),
}

TIERS_ORDER: list[Tier] = list(TIERS.keys())


def get_next_tier(current: Tier) -> Optional[Tier]:
    """Returns the next tier, or None if already at top."""
    idx = TIERS_ORDER.index(current)
    if idx + 1 < len(TIERS_ORDER):
        return TIERS_ORDER[idx + 1]
    return None