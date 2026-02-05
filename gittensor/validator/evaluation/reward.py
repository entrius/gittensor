# The MIT License (MIT)
# Copyright © 2025 Entrius
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Tuple

import bittensor as bt
import numpy as np
from aiohttp import ClientConnectorError

from gittensor.classes import MinerEvaluation
from gittensor.synapses import GitPatSynapse
from gittensor.utils.github_api_tools import load_miners_prs
from gittensor.validator.evaluation.dynamic_emissions import apply_dynamic_emissions_using_network_contributions
from gittensor.validator.evaluation.inspections import (
    detect_and_penalize_miners_sharing_github,
    validate_response_and_initialize_miner_evaluation,
)
from gittensor.validator.evaluation.normalize import normalize_rewards_linear
from gittensor.validator.evaluation.scoring import (
    finalize_miner_scores,
    score_miner_prs,
)
from gittensor.validator.evaluation.tier_emissions import allocate_emissions_by_tier
from gittensor.validator.utils.load_weights import LanguageConfig, RepositoryConfig, TokenConfig

# NOTE: there was a circular import error, needed this if to resolve it
if TYPE_CHECKING:
    from neurons.validator import Validator


async def query_miner(self, uid: int) -> GitPatSynapse:
    """
    Returns:
        GitPatSynapse: A gittensor protocol object with a miner github pat
    """

    bt.logging.debug(f'\nQuerying UID {uid}')

    try:
        response = await self.dendrite(
            axons=[self.metagraph.axons[uid]],
            synapse=GitPatSynapse(),
            # Don't deserialize, get the GitPatSynapse objects directly
            deserialize=False,
        )

        # Extract the single response from the list
        miner_response = response[0] if response else None
        return miner_response

    except ClientConnectorError:
        bt.logging.warning(f'Cannot connect to UID {uid} - miner unreachable')
        return None
    except Exception as e:
        bt.logging.error(f'Error querying miner UID {uid}: {e}')
        return None


async def evaluate_miners_pull_requests(
    uid: int,
    response: GitPatSynapse,
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
) -> MinerEvaluation:
    """
    Entry point from taking a miners response -> Get PRs -> Score PRs by tier

    Args:
        uid: The uid of the miner being evaluated
        response: The GitPatSynapse (github access token) returned by the miner
        master_repositories: The incentivized repositories and their RepositoryConfig objects
        programming_languages: The programming languages and their weights
        token_config: Token-based scoring weights configuration

    Returns:
        MinerEvaluation: The object containing scores, valid_prs, etc.
    """

    bt.logging.info(f'******* Reward function called for UID: {uid} *******')

    miner_eval = validate_response_and_initialize_miner_evaluation(uid, response)
    if miner_eval.failed_reason is not None:
        bt.logging.info(f'UID {uid} not being evaluated: {miner_eval.failed_reason}')
        return miner_eval

    load_miners_prs(miner_eval, master_repositories)

    score_miner_prs(miner_eval, master_repositories, programming_languages, token_config)

    # Clear PAT after scoring to avoid storing sensitive data
    miner_eval.github_pat = None

    bt.logging.info('*' * 50 + '\n')
    return miner_eval


async def get_rewards(
    self: Validator,
    uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation]]:
    """
    Args:
        uids (set[int]): All valid miner uids in the subnet
        master_repositories (Dict[str, RepositoryConfig]): The dict of repositories (name -> RepositoryConfig)
        programming_languages (Dict[str, LanguageConfig]): The dict of languages (extension -> LanguageConfig)
        token_config (TokenConfig): Token-based scoring weights configuration
    Returns:
        rewards (array[int]): An array of scores for all miners in sorted fashion, miner n score = index[n]
    """

    bt.logging.info(f'UIDs: {uids}')

    responses: Dict[int, GitPatSynapse] = {}
    miner_evaluations: Dict[int, MinerEvaluation] = {}

    # Query miners and calculate score.
    for uid in uids:
        # Retrieve PAT
        miner_response = await query_miner(self, uid)
        responses[uid] = miner_response

        # Calculate score
        miner_evaluation = await evaluate_miners_pull_requests(
            uid, miner_response, master_repositories, programming_languages, token_config
        )
        miner_evaluations[uid] = miner_evaluation

    # If evaluation of miner was successful, store to cache, if api failure, fallback to previous successful evaluation if any
    self.store_or_use_cached_evaluation(miner_evaluations)

    # Adjust scores for duplicate accounts
    detect_and_penalize_miners_sharing_github(miner_evaluations)

    # Finalize scores: apply unique contribution multiplier, credibility, sum totals, deduct collateral
    finalize_miner_scores(miner_evaluations)

    # Allocate emissions by tier: replace total_score with tier-weighted allocations
    allocate_emissions_by_tier(miner_evaluations)

    # Normalize the rewards between [0,1]
    normalized_rewards = normalize_rewards_linear(miner_evaluations)

    # Scale rewards according to dynamic emission curve based off of miners total contributions.
    final_rewards = apply_dynamic_emissions_using_network_contributions(normalized_rewards, miner_evaluations)

    # Store miner evaluations after calculating all scores
    await self.bulk_store_evaluation(miner_evaluations)

    return (
        np.array([final_rewards.get(uid, 0.0) for uid in sorted(uids)]),
        miner_evaluations,
    )
