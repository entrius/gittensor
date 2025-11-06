# The MIT License (MIT)
# Copyright © 2025 Entrius

from collections import defaultdict
from typing import Dict, List

import bittensor as bt

from gittensor.classes import GitPatSynapse, MinerEvaluation
from gittensor.constants import RECYCLE_UID, MIN_GITHUB_ACCOUNT_AGE
from gittensor.utils.github_api_tools import (
    get_github_account_age_days,
    get_github_id,
)


def detect_and_penalize_duplicates(
    miner_responses: Dict[int, GitPatSynapse], miner_evaluations: Dict[int, MinerEvaluation]
):
    """
    Detects miners that used the same github, duplicated across multiple uids.
    Will then penalize detected 'duplicate miners' with a score of 0.0

    Args:
        miner_responses (Dict[int, GitPatSynapse]): Mapping of miner uid to their GitPatSynapse response.
        miner_evaluations (Dict[int, MinerEvaluation]): Mapping of miner UID to their MinerEvaluation.

    Note:
        This function modifies the `miner_evaluations` dictionary in-place.
        All miners sharing a GitHub account will be penalized equally.
    """

    bt.logging.info("Now checking for duplicate users across miners...")

    github_id_to_uids: Dict[str, List[int]] = defaultdict(list)

    for uid, synapse in miner_responses.items():

        if not synapse:
            bt.logging.info(f"Synapse not found for uid {uid} when attempting to detect duplicates")
            continue

        miner_pat = synapse.github_access_token
        if not miner_pat:
            bt.logging.info(f"miner_pat not found for uid {uid} when attempting to detect duplicates")
            continue

        github_id = get_github_id(miner_pat)
        if not github_id:
            bt.logging.info(f"github_id not found for uid {uid} when attempting to detect duplicates")
            continue

        # Map GitHub ID to UID
        github_id_to_uids[github_id].append(uid)

    duplicate_count = 0
    for github_id, uids in github_id_to_uids.items():
        if len(uids) > 1:
            bt.logging.info(f"Detected UIDs {uids} sharing GitHub account")
            for uid in uids:
                _penalize_miner(uid, miner_evaluations)
                duplicate_count += 1

    bt.logging.info(f"Total duplicate miners penalized: {duplicate_count}")


def _penalize_miner(uid: int, miner_evaluations: Dict[int, MinerEvaluation]):
    """Reset a miner's evaluation and set reward to 0."""

    bt.logging.info(f"PENALTY: Duplicate detected, zeroing score for uid {uid}")

    # We reset the miner evaluation here so it's not counted towards metrics.
    miner_evaluations[uid] = MinerEvaluation(uid=uid, hotkey=miner_evaluations[uid].hotkey)


def validate_response_and_initialize_miner_evaluation(uid: int, response: GitPatSynapse) -> MinerEvaluation:

    miner_eval = MinerEvaluation(
        uid=int(uid), hotkey=response.axon.hotkey
    )  # uid is type np.int64, convert to int for less issues down the line

    # UID 0 is special case
    if uid == RECYCLE_UID:
        miner_eval.set_invalid_response_reason(f"SPECIAL CASE UID 0")
        return miner_eval

    # synapse check
    if not response:
        miner_eval.set_invalid_response_reason(f"No response provided by miner {uid}: setting default score of 0.")
        return miner_eval

    # PAT check
    miner_pat = response.github_access_token
    if not miner_pat:
        miner_eval.set_invalid_response_reason(f"No Github PAT provided by miner {uid}: setting default score of 0.")
        return miner_eval

    # github ID check
    miner_gh_id = get_github_id(miner_pat)
    if not miner_gh_id:
        miner_eval.set_invalid_response_reason(
            f"No Github id was found given miner {uid}'s provided PAT: setting default score of 0."
        )
        return miner_eval

    # account age check
    miner_gh_age = get_github_account_age_days(miner_pat)
    if not miner_gh_age:  # if api request failed, tragically have to set score to 0
        miner_eval.set_invalid_response_reason(
            f"No Github age was found given miner {uid}'s provided PAT: setting default score of 0."
        )
    if miner_gh_age < MIN_GITHUB_ACCOUNT_AGE:
        miner_eval.set_invalid_response_reason(
            f"Miner {uid}'s Github account is too young, {miner_gh_age} days < {MIN_GITHUB_ACCOUNT_AGE} days: setting default score of 0."
        )

    miner_eval.github_id = miner_gh_id
    miner_eval.github_pat = miner_pat
    return miner_eval
