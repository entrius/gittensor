from dataclasses import dataclass
from typing import Optional
from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_THRESHOLD,
    POTENTIAL_SCORE_COLLATERAL_PERCENT
)


@dataclass(frozen=True)
class TierUnlockRequirements:
    required_merges: int          # merges needed in current tier to unlock next
    required_credibility: float   # credibility % needed in current tier


@dataclass(frozen=True)
class TierConfig:
    unlock_next_tier: Optional[TierUnlockRequirements]  # None for top tier
    credibility_scalar: int                             # Tier-level exponential scaling
    open_pr_collateral_percentage: int
    open_prs_allowed: int


# -------------------------------------------------------------------------
# Tier Configurations
# -------------------------------------------------------------------------

TIERS_ORDER = ["Low", "Lower-Mid", "Middle", "Upper-Mid", "High"]

TIERS = {
    "Low": TierConfig(
        unlock_next_tier=TierUnlockRequirements(
            required_merges=3,
            required_credibility=0.40
        ),
        credibility_scalar=1,
        open_pr_collateral_percentage=POTENTIAL_SCORE_COLLATERAL_PERCENT,
        open_prs_allowed=EXCESSIVE_PR_PENALTY_THRESHOLD
    ),
    "Lower-Mid": TierConfig(
        unlock_next_tier=TierUnlockRequirements(
            required_merges=3,
            required_credibility=0.50
        ),
        credibility_scalar=2,
        open_pr_collateral_percentage=POTENTIAL_SCORE_COLLATERAL_PERCENT,
        open_prs_allowed=EXCESSIVE_PR_PENALTY_THRESHOLD
    ),
    "Middle": TierConfig(
        unlock_next_tier=TierUnlockRequirements(
            required_merges=3,
            required_credibility=0.60
        ),
        credibility_scalar=3,
        open_pr_collateral_percentage=POTENTIAL_SCORE_COLLATERAL_PERCENT,
        open_prs_allowed=EXCESSIVE_PR_PENALTY_THRESHOLD
    ),
    "Upper-Mid": TierConfig(
        unlock_next_tier=TierUnlockRequirements(
            required_merges=5,
            required_credibility=0.70
        ),
        credibility_scalar=4,
        open_pr_collateral_percentage=POTENTIAL_SCORE_COLLATERAL_PERCENT,
        open_prs_allowed=EXCESSIVE_PR_PENALTY_THRESHOLD
    ),
    "High": TierConfig(
        unlock_next_tier=None,  # top tier: no next tier to unlock
        credibility_scalar=5,
        open_pr_collateral_percentage=POTENTIAL_SCORE_COLLATERAL_PERCENT,
        open_prs_allowed=EXCESSIVE_PR_PENALTY_THRESHOLD
    ),
}
