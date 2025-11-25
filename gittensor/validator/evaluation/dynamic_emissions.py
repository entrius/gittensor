from typing import Dict

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


def apply_dynamic_emissions_using_network_contributions(
    normalized_rewards: Dict[int, float], miner_evaluations: Dict[int, MinerEvaluation]
) -> Dict[int, float]:
    """
    Scale normalized rewards based on network-wide contributions.

    Args:
        normalized_rewards (Dict[int, float]): Normalized rewards that sum to 1.0
        miner_evaluations (Dict[int, MinerEvaluation]): Dict mapping miner UIDs to their evaluations

    Returns:
        Dict[int, float]: Scaled rewards after applying network dynamic emissions mechanism
    """
    if not normalized_rewards:
        bt.logging.warning("No normalized rewards provided for scaling")
        return {}

    # Calculate network emission scalar for lines changed total
    lines_scalar = calculate_network_lines_changed_emissions_scalar(miner_evaluations)

    # Calculate network emission scalar for unique repositories
    unique_repo_scalar = calculate_network_unique_repos_emissions_scalar(miner_evaluations)

    # Take the average of the 2 scalars to have the final network_scalar
    final_network_scalar = (lines_scalar + unique_repo_scalar) / 2.0

    # Apply network scalar to all rewards
    scaled_rewards = {}
    total_original_rewards = 0.0
    total_recycled = 0.0

    for uid, original_reward in normalized_rewards.items():
        scaled_reward = original_reward * final_network_scalar
        scaled_rewards[uid] = scaled_reward

        total_original_rewards += original_reward
        total_recycled += original_reward - scaled_reward

    # Allocate all recycled emissions to the recycled UID
    if RECYCLE_UID not in scaled_rewards:
        scaled_rewards[RECYCLE_UID] = 0.0

    scaled_rewards[RECYCLE_UID] += total_recycled if total_recycled > 0 else 1
    percent_rewards_recycled = total_recycled / total_original_rewards if total_original_rewards > 0 else 1.0

    bt.logging.info("Dynamic emissions based on network wide contributions applied:")
    bt.logging.info(f"  - Lines changed scalar: {lines_scalar:.6f}")
    bt.logging.info(f"  - Unique repos scalar: {unique_repo_scalar:.6f}")
    bt.logging.info(f"  - Final network scalar: {final_network_scalar:.6f}")
    bt.logging.info(f"  - Total emissions recycled: {total_recycled:.6f} ({percent_rewards_recycled*100:.2f}%)")
    bt.logging.info(f"  - Recycled emissions allocated to UID {RECYCLE_UID}: {total_recycled:.6f}")
    bt.logging.info(f"  - Final reward sum: {sum(scaled_rewards.values()):.6f}")
    bt.logging.info(f"  - Network unlock percentage: {final_network_scalar*100:.2f}%")

    return scaled_rewards


def calculate_network_lines_changed_emissions_scalar(miner_evaluations: Dict[int, MinerEvaluation]) -> float:
    """
    Calculate the emissions scalar based on total network lines changed and dynamic emission parameters.

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Dict mapping miner UIDs to their evaluations

    Returns:
        float: Network emission scalar (0-1) based on collective contribution
    """

    # Calculate total lines changed across all miners (excluding penalized miners)
    total_network_lines = sum(
        evaluation.total_lines_changed
        for evaluation in miner_evaluations.values()
        if evaluation.total_score > 0  # Exclude penalized miners
    )

    bt.logging.info(f"Total lines scored across all miners: {total_network_lines}")

    # Calculate scalar using exponential unlock curve
    scalar = (1 - LINES_CONTRIBUTED_MAX_RECYCLE) + LINES_CONTRIBUTED_MAX_RECYCLE * (
        1 - np.exp(-LINES_CONTRIBUTED_RECYCLE_DECAY_RATE * total_network_lines)
    )
    scalar = min(scalar, 1.0)  # Cap at 1.0

    bt.logging.info(f"Lines changed emission scalar: {scalar:.6f} (unlocked: {scalar*100:.2f}%)")

    return scalar


def calculate_network_unique_repos_emissions_scalar(miner_evaluations: Dict[int, MinerEvaluation]) -> float:
    """
    Calculate the emissions scalar based on total network unique repositories and dynamic emission parameters.

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Dict mapping miner UIDs to their evaluations

    Returns:
        float: Network emission scalar (0-1) based on collective repository diversity
    """

    all_unique_repositories = set()
    for evaluation in miner_evaluations.values():
        if evaluation.get_unique_repositories():
            all_unique_repositories.update(evaluation.get_unique_repositories())

    total_unique_repos = len(all_unique_repositories)

    bt.logging.info(
        f"Total unique repositories across all miners: {total_unique_repos}"
    )

    # Calculate scalar using exponential unlock curve
    scalar = (1 - UNIQUE_PRS_MAX_RECYCLE) + UNIQUE_PRS_MAX_RECYCLE * (
        1 - np.exp(-UNIQUE_PRS_RECYCLE_DECAY_RATE * total_unique_repos)
    )
    scalar = min(scalar, 1.0)  # Cap at 1.0

    bt.logging.info(f"Unique repositories emission scalar: {scalar:.6f} (unlocked: {scalar*100:.2f}%)")

    return scalar
