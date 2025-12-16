# The MIT License (MIT)
# Copyright © 2025 Entrius
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict

import bittensor as bt
import numpy as np

from gittensor.classes import GitPatSynapse, MinerEvaluation, PullRequest
from gittensor.utils.github_api_tools import get_user_merged_prs_graphql
from gittensor.validator.evaluation.dynamic_emissions import apply_dynamic_emissions_using_network_contributions
from gittensor.validator.evaluation.inspections import (
    detect_and_penalize_duplicates,
    validate_response_and_initialize_miner_evaluation,
)
from gittensor.validator.evaluation.normalize import normalize_rewards_linear
from gittensor.validator.evaluation.scoring import (
    apply_cross_miner_multipliers_and_finalize,
    score_pull_requests,
)

# NOTE: there was a circular import error, needed this if to resolve it
if TYPE_CHECKING:
    from neurons.validator import Validator


async def query_miner(self, uid: int) -> GitPatSynapse:
    """
    Returns:
        GitPatSynapse: A gittensor protocol object with a miner github pat
    """

    bt.logging.debug(f"\nQuerying UID {uid}")

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

    except Exception as e:
        bt.logging.error(f"Error querying miner UID {uid}: {e}")
        return None


async def reward(
    uid: int,
    response: GitPatSynapse,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> MinerEvaluation:
    """
    Entry point from taking a miners response -> Get PRs -> Score PR Diff

    Args:
        uid (int): The uid of the miner being evaluated
        response (GitPatSynapse): The GitPatSynapse (github access token) returned by the miner
        master_repositories (Dict[str, Dict]): The incentivized repositories and their metadata (weight, inactiveAt)
        programming_languages (Dict[str, float]): The programming languages and their weights

    Returns:
        MinerEvaluation: The object containing scores, valid_prs, etc.
    """

    bt.logging.info(f"******* Reward function called for UID: {uid} *******")

    miner_eval = await asyncio.to_thread(validate_response_and_initialize_miner_evaluation, uid, response)
    if miner_eval.failed_reason is not None:
        bt.logging.info(f"UID {uid} not being evaluated: {miner_eval.failed_reason}")
        return miner_eval


    pr_result = await asyncio.to_thread(
        get_user_merged_prs_graphql,
        miner_eval.github_id,
        miner_eval.github_pat,
        master_repositories,
    )

    miner_eval.total_merged_prs = pr_result.merged_pr_count
    miner_eval.total_open_prs = pr_result.open_pr_count
    miner_eval.total_closed_prs = pr_result.closed_pr_count

    for raw_pr in pr_result.valid_prs:
        miner_eval.add_pull_request(
            PullRequest.from_graphql_response(raw_pr, uid, miner_eval.hotkey, miner_eval.github_id)
        )


    await score_pull_requests(miner_eval, master_repositories, programming_languages)
    # Clear PAT after scoring to avoid storing sensitive data
    miner_eval.github_pat = None

    bt.logging.info("*" * 50 + "\n")
    return miner_eval


async def get_rewards(
    self: Validator, uids: set[int], master_repositories: dict[str, dict], programming_languages: dict[str, float]
) -> np.ndarray:
    """
    Args:
        uids (set[int]): All valid miner uids in the subnet
        master_repositories (dict[str, dict]): The dict of repositories (name -> {weight, inactiveAt})
        programming_languages (dict[str, float]): The dict of languages (extension, weight)
    Returns:
        rewards (array[int]): An array of scores for all miners in sorted fashion, miner n score = index[n]
    """

    bt.logging.info(f"UIDs: {uids}")

    responses: Dict[int, GitPatSynapse] = {}
    miner_evaluations: Dict[int, MinerEvaluation] = {}

    # Query miners and calculate score.
    for uid in uids:

        # retrieve PAT
        miner_response = await query_miner(self, uid)
        responses[uid] = miner_response

        # Calculate score
        miner_evaluation = await reward(uid, miner_response, master_repositories, programming_languages)
        miner_evaluations[uid] = miner_evaluation

    # Adjust scores for duplicate accounts
    detect_and_penalize_duplicates(responses, miner_evaluations)

    # Apply all multipliers and calculate final scores
    apply_cross_miner_multipliers_and_finalize(miner_evaluations)

    # store all miner evaluations after adjusting score
    await self.bulk_store_evaluation(miner_evaluations)

    # Normalize the rewards between [0,1]
    normalized_rewards = normalize_rewards_linear(miner_evaluations)

    # Scale rewards according to dynamic emission curve based off of miners total contributions.
    final_rewards = apply_dynamic_emissions_using_network_contributions(normalized_rewards, miner_evaluations)

    # Store miner evaluations after calculating all scores
    await self.bulk_store_evaluation(miner_evaluations)

    return np.array([final_rewards.get(uid, 0.0) for uid in sorted(uids)])
