from typing import Dict, Set

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    RECYCLE_UID,
    LINES_CONTRIBUTED_RECYCLE_DECAY_RATE,
    LINES_CONTRIBUTED_MAX_RECYCLE,
    UNIQUE_PRS_RECYCLE_DECAY_RATE,
    UNIQUE_PRS_MAX_RECYCLE,
)


def _exponential_unlock_scalar(value: float, max_recycle: float, decay_rate: float) -> float:
    """Calculate scalar using exponential unlock curve, capped at 1.0."""
    return min(1.0, (1 - max_recycle) + max_recycle * (1 - np.exp(-decay_rate * value)))


def get_network_totals(miner_evaluations: Dict[int, MinerEvaluation]) -> tuple[int, int]:
    """Extract total lines changed and unique repos from evaluations."""
    total_lines = 0
    unique_repos: Set[str] = set()
    
    for evaluation in miner_evaluations.values():
        if evaluation.total_merged_prs > 0:  # Exclude penalized miners
            total_lines += evaluation.total_lines_changed
        if repos := evaluation.unique_repos_contributed_to:
            unique_repos.update(repos)
    
    return total_lines, len(unique_repos)


def apply_dynamic_emissions_using_network_contributions(
    normalized_rewards: Dict[int, float], miner_evaluations: Dict[int, MinerEvaluation]
) -> Dict[int, float]:
    """Scale normalized rewards based on network-wide contributions."""
    if not normalized_rewards:
        bt.logging.warning("No normalized rewards provided for scaling")
        return {}

    # Calculate network metrics and scalars
    total_lines, total_unique_repos = get_network_totals(miner_evaluations)
    
    lines_scalar = _exponential_unlock_scalar(
        total_lines, LINES_CONTRIBUTED_MAX_RECYCLE, LINES_CONTRIBUTED_RECYCLE_DECAY_RATE
    )
    repo_scalar = _exponential_unlock_scalar(
        total_unique_repos, UNIQUE_PRS_MAX_RECYCLE, UNIQUE_PRS_RECYCLE_DECAY_RATE
    )
    final_scalar = (lines_scalar + repo_scalar) / 2.0

    # Apply scaling and calculate recycled amount
    total_original = sum(normalized_rewards.values())
    scaled_rewards = {uid: reward * final_scalar for uid, reward in normalized_rewards.items()}
    total_recycled = total_original * (1 - final_scalar)
    
    # Dynamic bound: full recycle (1.0) if no earned scores, otherwise 0
    dynamic_recycle_bound = 1 if total_original <= 0 else 0

    # Allocate recycled emissions
    scaled_rewards[RECYCLE_UID] = scaled_rewards.get(RECYCLE_UID, 0.0) + max(total_recycled, dynamic_recycle_bound)

    recycle_percentage = (total_recycled / total_original * 100) if total_original > 0 else 100.0
    bt.logging.info(
        f"Dynamic emissions: lines_scalar={lines_scalar:.3f}, repo_scalar={repo_scalar:.3f}, "
        f"final={final_scalar:.2f}, recycled={total_recycled:.2f} ({recycle_percentage:.2f}%)"
    )

    return scaled_rewards