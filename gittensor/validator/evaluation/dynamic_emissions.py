from typing import Dict, Set

import bittensor as bt
import numpy as np

from datetime import datetime, timedelta, UTC
from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    TIER_BASED_INCENTIVE_MECHANISM_START_DATE,
    RECYCLE_UID,
    DEFAULT_FIXED_RECYCLE_RATE,
    DYNAMIC_EMISSIONS_BUFFER_DAYS,
    LINES_CONTRIBUTED_RECYCLE_DECAY_RATE,
    LINES_CONTRIBUTED_MAX_RECYCLE,
    UNIQUE_PRS_RECYCLE_DECAY_RATE,
    UNIQUE_PRS_MAX_RECYCLE,
    MERGED_PRS_RECYCLE_DECAY_RATE,
    MERGED_PRS_MAX_RECYCLE,
)


def _exponential_unlock_scalar(value: float, max_recycle: float, decay_rate: float) -> float:
    """Calculate scalar using exponential unlock curve, capped at 1.0."""
    return min(1.0, (1 - max_recycle) + max_recycle * (1 - np.exp(-decay_rate * value)))


def get_network_totals(miner_evaluations: Dict[int, MinerEvaluation]) -> tuple[int, int, int]:
    """Extract total lines changed, unique repos, and total merged PR count from evaluations."""
    total_lines = 0
    unique_repos: Set[str] = set()
    total_merged_prs = 0
    
    for evaluation in miner_evaluations.values():
        total_lines += evaluation.total_lines_changed
        total_merged_prs += evaluation.total_merged_prs
        
        if repos := evaluation.unique_repos_contributed_to:
            unique_repos.update(repos)
    
    return total_lines, total_merged_prs, len(unique_repos)

def apply_dynamic_emissions_using_network_contributions(
    normalized_rewards: Dict[int, float], miner_evaluations: Dict[int, MinerEvaluation]
) -> Dict[int, float]:
    """Scale normalized rewards based on network-wide contributions."""
    if not normalized_rewards:
        bt.logging.warning("No normalized rewards provided for scaling")
        return {}

    dynamic_emissions_start = TIER_BASED_INCENTIVE_MECHANISM_START_DATE + timedelta(days=DYNAMIC_EMISSIONS_BUFFER_DAYS)
    use_dynamic_emissions = datetime.now(UTC) > dynamic_emissions_start

    if use_dynamic_emissions:
        total_lines, total_merged_prs, total_unique_repos = get_network_totals(miner_evaluations)
        
        lines_scalar = _exponential_unlock_scalar(
            total_lines, LINES_CONTRIBUTED_MAX_RECYCLE, LINES_CONTRIBUTED_RECYCLE_DECAY_RATE
        )
        merged_prs_scalar = _exponential_unlock_scalar(
            total_merged_prs, MERGED_PRS_MAX_RECYCLE, MERGED_PRS_RECYCLE_DECAY_RATE
        )
        unique_repo_scalar = _exponential_unlock_scalar(
            total_unique_repos, UNIQUE_PRS_MAX_RECYCLE, UNIQUE_PRS_RECYCLE_DECAY_RATE
        )
        final_scalar = (lines_scalar + merged_prs_scalar + unique_repo_scalar) / 3.0
    else:
        lines_scalar = merged_prs_scalar = unique_repo_scalar = None
        final_scalar = DEFAULT_FIXED_RECYCLE_RATE

    # Apply scaling and calculate recycled amount
    total_original = sum(normalized_rewards.values())
    total_recycled = total_original * (1 - final_scalar)
    
    scaled_rewards = {uid: reward * final_scalar for uid, reward in normalized_rewards.items()}
    scaled_rewards[RECYCLE_UID] = scaled_rewards.get(RECYCLE_UID, 0.0) + max(total_recycled, 1.0 if total_original <= 0 else 0.0)

    recycle_percentage = (total_recycled / total_original * 100) if total_original > 0 else 100.0
    
    if use_dynamic_emissions:
        bt.logging.info(
            f"Dynamic emissions: lines={lines_scalar:.2f}, merged_prs={merged_prs_scalar:.2f}, "
            f"unique_repos={unique_repo_scalar:.2f}, recycle_scalar={final_scalar:.2f}, "
            f"recycled={total_recycled:.2f} ({recycle_percentage:.2f}%)"
        )
    else:
        bt.logging.info(f"Fixed emissions until {dynamic_emissions_start}: recycle_scalar={final_scalar:.2f}, recycled={total_recycled:.2f} ({recycle_percentage:.2f}%)")

    return scaled_rewards