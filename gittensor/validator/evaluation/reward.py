# The MIT License (MIT)
# Copyright © 2025 Entrius
from __future__ import annotations

from typing import TYPE_CHECKING, Dict

import bittensor as bt
import numpy as np

from gittensor.classes import GitPatSynapse, MinerEvaluation, PullRequest
from gittensor.utils.github_api_tools import get_pull_request_file_changes, get_user_merged_prs_graphql
from gittensor.validator.evaluation.dynamic_emissions import apply_dynamic_emissions_using_network_contributions
from gittensor.validator.evaluation.inspections import (
    detect_and_penalize_duplicates,
    validate_response_and_initialize_miner_evaluation,
)
from gittensor.validator.evaluation.scoring import (
    apply_boost_for_gittensor_tag_in_pr_description,
    apply_issue_resolvement_bonus,
    apply_repository_uniqueness_boost,
    apply_time_decay_for_repository_contributions,
    normalize_rewards_with_pareto,
)
from gittensor.validator.evaluation.spam_detection import apply_typo_detection_penalties

# NOTE: there was a circular import error, needed this if to resolve it
if TYPE_CHECKING:
    from neurons.validator import Validator


def score_pull_requests(
    uid: int,
    miner_eval: MinerEvaluation,
    valid_raw_prs: list,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> MinerEvaluation:
    """
    Helper function to score pull requests and populate MinerEvaluation object.

    This function takes raw PR data and:
    1. Converts to PullRequest objects
    2. Fetches file changes for each PR
    3. Calculates scores based on language weights and changes
    4. Applies repository weight
    5. Applies issue bonuses
    6. Calculates totals and penalties

    Args:
        uid (int): Miner UID for logging
        miner_eval (MinerEvaluation): MinerEvaluation object to populate
        valid_raw_prs (list): List of raw PR data from GraphQL API
        master_repositories (Dict[str, Dict]): The incentivized repositories and their metadata (weight, inactiveAt)
        programming_languages (Dict[str, float]): The programming languages and their weights

    Returns:
        MinerEvaluation: The populated evaluation object
    """

    github_pat = miner_eval.github_pat

    valid_prs = [
        PullRequest.from_graphql_response(raw_pr, uid, miner_eval.hotkey, miner_eval.github_id)
        for raw_pr in valid_raw_prs
    ]

    if not valid_prs or not len(valid_prs):
        bt.logging.info(f"No valid PRs found for miner {uid}: setting default score of 0.")
        return miner_eval

    total_prs = len(valid_prs)
    bt.logging.info(f"Scoring {total_prs} PRs for miner {uid}")

    for n, pr in enumerate(valid_prs, start=1):
        # if repo not in master list, default to .01 (shouldn't happen bc already filtered in github graphql method)
        repo_weight = master_repositories.get(pr.repository_full_name, {}).get("weight", 0.01)

        bt.logging.info(
            f"[{n}/{total_prs}] - Scoring PR #{pr.number} in {pr.repository_full_name} (weight: {repo_weight})"
        )

        file_changes = get_pull_request_file_changes(pr.repository_full_name, pr.number, github_pat)
        if not file_changes:
            bt.logging.warning("No file changes found for this PR.")
            continue

        pr.set_file_changes(file_changes)
        pr.set_base_score(pr.calculate_score_from_file_changes(programming_languages))

        apply_issue_resolvement_bonus(pr)

        apply_typo_detection_penalties(pr, uid)

        pr_score_before_repo_weight = pr.base_score
        bt.logging.info(
            f"Applying repo weight to earned PR score: {round(pr_score_before_repo_weight, 2)} x {float(repo_weight)} -> {round(pr_score_before_repo_weight * float(repo_weight), 2)}"
        )
        pr.set_base_score(pr_score_before_repo_weight * float(repo_weight))
        pr.set_earned_score(pr.base_score)

        miner_eval.add_pull_request(pr)

    return miner_eval


# query miner for synapse
async def query_miner(self, uid: int) -> GitPatSynapse:
    """
    Returns:
        GitPatSynapse: A gittensor protocol object with a miner github pat
    """

    bt.logging.debug(f"Querying UID {uid}")

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


# calculate score for a given miner
async def reward(
    self: Validator,
    uid: int,
    response: GitPatSynapse,
    master_repositories: Dict[str, Dict],
    programming_languages: Dict[str, float],
) -> MinerEvaluation:
    """
    Args:
        uid (int): The uid of the miner being evaluated
        response (GitPatSynapse): The GitPatSynapse (github access token) returned by the miner
        master_repositories (Dict[str, Dict]): The incentivized repositories and their metadata (weight, inactiveAt)
        programming_languages (Dict[str, float]): The programming languages and their weights

    Returns:
        MinerEvaluation: The object containing scores, valid_prs, etc.
    """

    bt.logging.info(f"******* Reward function called for UID: {uid} *******")

    miner_eval = validate_response_and_initialize_miner_evaluation(uid, response)
    if miner_eval.failed_reason is not None:
        bt.logging.info(f"UID {uid} not being evaluated: {miner_eval.failed_reason}")
        return miner_eval

    valid_raw_prs, open_pr_count = get_user_merged_prs_graphql(
        miner_eval.github_id, miner_eval.github_pat, master_repositories
    )

    miner_eval.total_open_prs = open_pr_count

    miner_eval = score_pull_requests(uid, miner_eval, valid_raw_prs, master_repositories, programming_languages)

    bt.logging.info("*" * 50)
    return miner_eval


# process scores for all miners
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
        miner_evaluation = await reward(self, uid, miner_response, master_repositories, programming_languages)
        miner_evaluations[uid] = miner_evaluation

    # Adjust scores for duplicate accounts
    detect_and_penalize_duplicates(responses, miner_evaluations)

    # Boost miners who contribute to more unique repos relative to other miners.
    apply_repository_uniqueness_boost(miner_evaluations)

    # Older contributions within the lookback window will get less score.
    apply_time_decay_for_repository_contributions(miner_evaluations)

    # Boost PRs that include the Gittensor tagline (and were not edited after merge).
    apply_boost_for_gittensor_tag_in_pr_description(miner_evaluations)

    # Normalize the rewards between [0,1] with a pareto boost for higher performing miners.
    normalized_rewards = normalize_rewards_with_pareto(miner_evaluations)

    # Scale rewards according to dynamic emission curve based off of miners total contributions.
    final_rewards = apply_dynamic_emissions_using_network_contributions(normalized_rewards, miner_evaluations)

    # Store miner evaluations after calculating all scores
    await self.bulk_store_evaluation(miner_evaluations)

    return np.array([final_rewards.get(uid, 0.0) for uid in sorted(uids)])
