from typing import Dict

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    BURN_UID,
    LINES_CONTRIBUTED_BURN_DECAY_RATE,
    LINES_CONTRIBUTED_MAX_BURN,
    UNIQUE_PRS_BURN_DECAY_RATE,
    UNIQUE_PRS_MAX_BURN,
)


def scale_rewards_with_network_burn(
    normalized_rewards: Dict[int, float], miner_evaluations: Dict[int, MinerEvaluation]
) -> Dict[int, float]:
    """
    Scale normalized rewards based on network-wide burn mechanism.

    Args:
        normalized_rewards (Dict[int, float]): Normalized rewards that sum to 1.0
        miner_evaluations (Dict[int, MinerEvaluation]): Dict mapping miner UIDs to their evaluations

    Returns:
        Dict[int, float]: Scaled rewards after applying network burn mechanism
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
    total_burned = 0.0
    burn_uid = BURN_UID

    for uid, reward in normalized_rewards.items():
        scaled_reward = reward * final_network_scalar
        scaled_rewards[uid] = scaled_reward
        total_burned += reward - scaled_reward

    # Allocate all burned emissions to the burn UID
    if burn_uid not in scaled_rewards:
        scaled_rewards[burn_uid] = 0.0
    scaled_rewards[burn_uid] += total_burned

    bt.logging.info(f"Network burn applied:")
    bt.logging.info(f"  - Lines changed scalar: {lines_scalar:.6f}")
    bt.logging.info(f"  - Unique repos scalar: {unique_repo_scalar:.6f}")
    bt.logging.info(f"  - Final network scalar: {final_network_scalar:.6f}")
    bt.logging.info(f"  - Total emissions burned: {total_burned:.6f} ({total_burned*100:.2f}%)")
    bt.logging.info(f"  - Burned emissions allocated to UID {burn_uid}: {total_burned:.6f}")
    bt.logging.info(f"  - Final reward sum: {sum(scaled_rewards.values()):.6f}")
    bt.logging.info(f"  - Network unlock percentage: {final_network_scalar*100:.2f}%")

    return scaled_rewards


def calculate_network_lines_changed_emissions_scalar(miner_evaluations: Dict[int, MinerEvaluation]) -> float:
    """
    Calculate the emissions scalar based on total network lines changed and burn parameters.

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Dict mapping miner UIDs to their evaluations

    Returns:
        float: Network emission scalar (0-1) based on collective contribution
    """

    # Calculate total lines changed across all miners (excluding penalized miners)
    # TODO: We'll want to get this function to evaluate all time lines changed, not just those within lookback window.
    total_network_lines = sum(
        evaluation.total_lines_changed
        for evaluation in miner_evaluations.values()
        if evaluation.total_score > 0  # Exclude penalized miners
    )

    bt.logging.info(f"Total lines changed across all miners (disregarding penalized miners): {total_network_lines}")

    # Calculate scalar using exponential unlock curve
    scalar = (1 - LINES_CONTRIBUTED_MAX_BURN) + LINES_CONTRIBUTED_MAX_BURN * (
        1 - np.exp(-LINES_CONTRIBUTED_BURN_DECAY_RATE * total_network_lines)
    )
    scalar = min(scalar, 1.0)  # Cap at 1.0

    bt.logging.info(f"Network emission scalar: {scalar:.6f} (unlocked: {scalar*100:.2f}%)")

    # We can implement time-based campaigns
    # - Weekly/monthly targets with bonus multipliers
    # - "Sprint weeks" with higher burn decay for rapid unlock
    # - Seasonal campaigns targeting specific ecosystem needs

    return scalar


def calculate_network_unique_repos_emissions_scalar(miner_evaluations: Dict[int, MinerEvaluation]) -> float:
    """
    Calculate the emissions scalar based on total network unique repositories and burn parameters.

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
        f"Total unique repositories across all miners (disregarding penalized miners): {total_unique_repos}"
    )

    # Calculate scalar using exponential unlock curve
    scalar = (1 - UNIQUE_PRS_MAX_BURN) + UNIQUE_PRS_MAX_BURN * (
        1 - np.exp(-UNIQUE_PRS_BURN_DECAY_RATE * total_unique_repos)
    )
    scalar = min(scalar, 1.0)  # Cap at 1.0

    bt.logging.info(f"Unique repositories emission scalar: {scalar:.6f} (unlocked: {scalar*100:.2f}%)")

    return scalar

