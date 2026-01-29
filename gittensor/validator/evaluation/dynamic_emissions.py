from typing import Dict, Set

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    RECYCLE_UID,
    TOKEN_SCORE_MAX_RECYCLE,
    TOKEN_SCORE_RECYCLE_DECAY_RATE,
    UNIQUE_REPOS_MAX_RECYCLE,
    UNIQUE_REPOS_RECYCLE_DECAY_RATE,
)


def _exponential_unlock_scalar(value: float, max_recycle: float, decay_rate: float) -> float:
    """Calculate scalar using exponential unlock curve, capped at 1.0."""
    return min(1.0, (1 - max_recycle) + max_recycle * (1 - np.exp(-decay_rate * value)))


def get_network_totals(miner_evaluations: Dict[int, MinerEvaluation]) -> tuple[int, float]:
    """Extract unique repos count and total token score from tiered miners only.

    Only miners with a tier (bronze, silver, gold) are counted.
    This excludes miners who haven't reached any tier yet.
    """
    unique_repos: Set[str] = set()
    total_token_score = 0.0

    for evaluation in miner_evaluations.values():
        # Only count contributions from miners who have achieved a tier
        if evaluation.current_tier is not None:
            total_token_score += evaluation.total_token_score

            if repos := evaluation.unique_repos_contributed_to:
                unique_repos.update(repos)

    return len(unique_repos), total_token_score


def apply_dynamic_emissions_using_network_contributions(
    normalized_rewards: Dict[int, float], miner_evaluations: Dict[int, MinerEvaluation]
) -> Dict[int, float]:
    """Scale normalized rewards based on network-wide contributions."""
    if not normalized_rewards:
        bt.logging.warning('No normalized rewards provided for scaling')
        return {}

    total_unique_repos, total_token_score = get_network_totals(miner_evaluations)

    unique_repo_scalar = _exponential_unlock_scalar(
        total_unique_repos, UNIQUE_REPOS_MAX_RECYCLE, UNIQUE_REPOS_RECYCLE_DECAY_RATE
    )
    token_score_scalar = _exponential_unlock_scalar(
        total_token_score, TOKEN_SCORE_MAX_RECYCLE, TOKEN_SCORE_RECYCLE_DECAY_RATE
    )
    final_scalar = (unique_repo_scalar + token_score_scalar) / 2.0

    # Apply scaling and calculate recycled amount
    total_original = sum(normalized_rewards.values())
    total_recycled = total_original * (1 - final_scalar)

    scaled_rewards = {uid: reward * final_scalar for uid, reward in normalized_rewards.items()}
    scaled_rewards[RECYCLE_UID] = scaled_rewards.get(RECYCLE_UID, 0.0) + max(
        total_recycled, 1.0 if total_original <= 0 else 0.0
    )

    recycle_percentage = (total_recycled / total_original * 100) if total_original > 0 else 100.0

    bt.logging.info(
        f'Dynamic emissions: unique_repos={unique_repo_scalar:.2f}, token_score={token_score_scalar:.2f}, '
        f'recycle_scalar={final_scalar:.2f}, recycled={total_recycled:.2f} ({recycle_percentage:.2f}%)'
    )

    return scaled_rewards
