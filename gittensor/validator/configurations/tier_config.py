from dataclasses import dataclass
from enum import Enum
from typing import Optional

from gittensor.constants import (
    DEFAULT_COLLATERAL_PERCENT,
    DEFAULT_CREDIBILITY_THRESHOLD,
    DEFAULT_MERGED_PR_BASE_SCORE,
    MAX_LINE_CONTRIBUTION_BONUS,
    DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS,
)

@dataclass
class TierStats:
    """Statistics for a single tier."""
    merged_count: int = 0
    closed_count: int = 0
    open_count: int = 0

    # Included as scoring details at the tier level
    earned_score: float = 0.0
    collateral_score: float = 0.0

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
    BRONZE = "Bronze"
    SILVER = "Silver"
    GOLD = "Gold"


TIER_DEFAULTS = {
    "merged_pr_base_score": DEFAULT_MERGED_PR_BASE_SCORE,
    "contribution_score_for_full_bonus": DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS,
    "contribution_score_max_bonus": MAX_LINE_CONTRIBUTION_BONUS,
    "credibility_activation_attempts": DEFAULT_CREDIBILITY_THRESHOLD,
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
    #                                 merges  credibility  scalar  (requirements to MAINTAIN this tier)
    Tier.BRONZE:   TierConfig(        None,   None,        1      ),  # always unlocked
    Tier.SILVER:   TierConfig(        3,      0.50,        2      ),
    Tier.GOLD:     TierConfig(        5,      0.70,        3      ),
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