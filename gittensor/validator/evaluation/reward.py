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

    bt.logging.info(f"Valid PRs to score: {len(valid_prs)}")

    for pr in valid_prs:
        # if repo not in master list, default to .01 (shouldn't happen bc already filtered in github graphql method)
        repo_weight = master_repositories.get(pr.repository_full_name).get("weight", 0.01)
        file_changes = get_pull_request_file_changes(pr.repository_full_name, pr.number, github_pat)
        if not file_changes:
            continue

        pr.set_file_changes(file_changes)
        pr.set_earned_score(pr.calculate_score_from_file_changes(programming_languages))
        bt.logging.info(f"Calculated a base PR score from the file changes of {pr.earned_score}")

        apply_issue_resolvement_bonus(pr)

        pr_score_before_repo_weight = pr.earned_score
        bt.logging.info(f"Applying repo weight to earned PR score: {pr_score_before_repo_weight} x {float(repo_weight)} -> {pr_score_before_repo_weight * float(repo_weight)}")
        pr.set_earned_score(pr_score_before_repo_weight * float(repo_weight))

        miner_eval.add_pull_request(pr)

    return miner_eval


# query miner for synapse
async def query_miner(self, uid: int) -> GitPatSynapse:
    """
    Returns:
        GitPatSynapse: A gittensor protocol object with a miner github pat
    """
    import time

    bt.logging.info(f"→ Querying miner UID {uid}...")
    query_start = time.time()

    try:
        response = await self.dendrite(
            axons=[self.metagraph.axons[uid]],
            synapse=GitPatSynapse(),
            # Don't deserialize, get the GitPatSynapse objects directly
            deserialize=False,
        )

        # Extract the single response from the list
        miner_response = response[0] if response else None
        query_time = time.time() - query_start
        
        if miner_response:
            bt.logging.info(f"✓ Received response from UID {uid} in {query_time:.2f}s")
        else:
            bt.logging.warning(f"✗ No response from UID {uid} after {query_time:.2f}s")
        
        return miner_response

    except Exception as e:
        query_time = time.time() - query_start
        bt.logging.error(f"✗ Error querying miner UID {uid} after {query_time:.2f}s: {e}")
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

    bt.logging.info(f"{'='*60}")
    bt.logging.info(f"Evaluating UID {uid}")
    bt.logging.info(f"{'='*60}")

    # Validate response
    bt.logging.info(f"Validating response from UID {uid}...")
    miner_eval = validate_response_and_initialize_miner_evaluation(uid, response)
    if miner_eval.failed_reason is not None:
        bt.logging.warning(f"✗ UID {uid} validation failed: {miner_eval.failed_reason}")
        return miner_eval
    bt.logging.info(f"✓ Response validated for UID {uid}")
    bt.logging.info(f"  - GitHub ID: {miner_eval.github_id}")
    bt.logging.info(f"  - Hotkey: {miner_eval.hotkey}")

    # Fetch PRs
    bt.logging.info(f"Fetching merged PRs for UID {uid}...")
    valid_raw_prs, open_pr_count = get_user_merged_prs_graphql(
        miner_eval.github_id, miner_eval.github_pat, master_repositories
    )
    bt.logging.info(f"✓ Found {len(valid_raw_prs)} merged PRs")
    bt.logging.info(f"  - Open PRs: {open_pr_count}")

    miner_eval.total_open_prs = open_pr_count

    # Score PRs
    if valid_raw_prs:
        bt.logging.info(f"Scoring {len(valid_raw_prs)} PRs for UID {uid}...")
        miner_eval = score_pull_requests(uid, miner_eval, valid_raw_prs, master_repositories, programming_languages)
        bt.logging.info(f"✓ Scoring complete for UID {uid}")
        bt.logging.info(f"  - Total score: {miner_eval.total_score:.4f}")
        bt.logging.info(f"  - Valid PRs scored: {len(miner_eval.pull_requests)}")
    else:
        bt.logging.info(f"No PRs to score for UID {uid}")

    # Store evaluation
    await self.store_evaluation(uid, miner_eval)
    bt.logging.info(f"✓ Evaluation stored for UID {uid}")

    bt.logging.info(f"{'='*60}")
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

    bt.logging.info(f"Processing rewards for {len(uids)} miners: {sorted(uids)}")

    responses: Dict[int, GitPatSynapse] = {}
    miner_evaluations: Dict[int, MinerEvaluation] = {}

    # Query miners and calculate score
    bt.logging.info("Phase 1: Querying miners and calculating base scores...")
    for i, uid in enumerate(uids, 1):
        bt.logging.info(f"[{i}/{len(uids)}] Processing UID {uid}")

        # retrieve PAT
        miner_response = await query_miner(self, uid)
        responses[uid] = miner_response

        # Calculate score
        miner_evaluation = await reward(self, uid, miner_response, master_repositories, programming_languages)
        miner_evaluations[uid] = miner_evaluation
        
        if miner_evaluation.total_score > 0:
            bt.logging.info(f"✓ UID {uid} base score: {miner_evaluation.total_score:.4f}")
        else:
            bt.logging.info(f"○ UID {uid} has no score")

    bt.logging.info(f"✓ Phase 1 complete: {len(miner_evaluations)} miners evaluated")

    # Adjust scores for duplicate accounts
    bt.logging.info("Phase 2: Detecting and penalizing duplicate accounts...")
    detect_and_penalize_duplicates(responses, miner_evaluations)
    bt.logging.info("✓ Phase 2 complete")

    # Boost miners who contribute to more unique repos relative to other miners
    bt.logging.info("Phase 3: Applying repository uniqueness boost...")
    apply_repository_uniqueness_boost(miner_evaluations)
    bt.logging.info("✓ Phase 3 complete")

    # Older contributions within the lookback window will get less score
    bt.logging.info("Phase 4: Applying time decay to contributions...")
    apply_time_decay_for_repository_contributions(miner_evaluations)
    bt.logging.info("✓ Phase 4 complete")

    # Boost PRs that include the Gittensor tagline (and were not edited after merge)
    bt.logging.info("Phase 5: Applying Gittensor attribution boost...")
    apply_boost_for_gittensor_tag_in_pr_description(miner_evaluations)
    bt.logging.info("✓ Phase 5 complete")

    # Normalize the rewards between [0,1] with a pareto boost for higher performing miners
    bt.logging.info("Phase 6: Normalizing rewards with Pareto distribution...")
    normalized_rewards = normalize_rewards_with_pareto(miner_evaluations)
    bt.logging.info("✓ Phase 6 complete")

    # Scale rewards according to dynamic emission curve based off of miners total contributions
    bt.logging.info("Phase 7: Applying dynamic emissions scaling...")
    final_rewards = apply_dynamic_emissions_using_network_contributions(normalized_rewards, miner_evaluations)
    bt.logging.info("✓ Phase 7 complete")

    # Log final reward distribution
    bt.logging.info("Final reward distribution:")
    for uid in sorted(uids):
        final_score = final_rewards.get(uid, 0.0)
        if final_score > 0:
            bt.logging.info(f"  UID {uid}: {final_score:.6f}")

    return np.array([final_rewards.get(uid, 0.0) for uid in sorted(uids)])
