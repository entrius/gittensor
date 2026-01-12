from dataclasses import dataclass
from enum import Enum
from typing import Optional

from gittensor.constants import (
    DEFAULT_COLLATERAL_PERCENT,
    DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS,
    DEFAULT_MERGED_PR_BASE_SCORE,
    MAX_CONTRIBUTION_BONUS,
)


@dataclass
class TierStats:
    """Statistics for a single tier."""

    merged_count: int = 0
    closed_count: int = 0
    open_count: int = 0

    unique_repo_contribution_count: int = 0
    # Unique repos that meet a min token score threshold (set during calculation)
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
        required_min_token_score_per_repo=1.0,  # Each of unique repos must have at least x token score
        credibility_scalar=1.0,
    ),
    Tier.SILVER: TierConfig(
        required_credibility=0.65,
        required_min_token_score=50.0,  # Minimum total token score for Silver unlock
        required_unique_repos_count=3,
        required_min_token_score_per_repo=1.0,  # Each of unique repos must have at least x token score
        credibility_scalar=1.5,
    ),
    Tier.GOLD: TierConfig(
        required_credibility=0.60,
        required_min_token_score=150.0,  # Minimum total token score for Gold unlock
        required_unique_repos_count=3,
        required_min_token_score_per_repo=1.0,  # Each of unique repos must have at least x token score
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
