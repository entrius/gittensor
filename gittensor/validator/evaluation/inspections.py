# The MIT License (MIT)
# Copyright © 2025 Entrius

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import bittensor as bt

from gittensor.classes import GitPatSynapse, MinerEvaluation
from gittensor.constants import (
    RECYCLE_UID,
    MIN_GITHUB_ACCOUNT_AGE,
)
from gittensor.utils.github_api_tools import (
    get_github_account_age_days,
    get_github_id,
)


def detect_and_penalize_duplicates(
    miner_responses: Dict[int, GitPatSynapse], miner_evaluations: Dict[int, MinerEvaluation]
):
    """
    Detects miners that used the same github, duplicated across multiple uids.
    Will then penalize detected 'duplicate miners' with a score of 0.0.
    All miners sharing a GitHub account will be penalized equally.

    Args:
        miner_responses (Dict[int, GitPatSynapse]): Mapping of miner uid to their GitPatSynapse response.
        miner_evaluations (Dict[int, MinerEvaluation]): Mapping of miner UID to their MinerEvaluation.
    """

    bt.logging.info("Now checking for duplicate users across miners...")

    github_id_to_uids: Dict[str, List[int]] = defaultdict(list)

    for uid, synapse in miner_responses.items():
        if not synapse or not synapse.github_access_token:
            continue

        if github_id := get_github_id(synapse.github_access_token):
            github_id_to_uids[github_id].append(uid)

    duplicate_count = 0
    for github_id, uids in github_id_to_uids.items():
        if len(uids) <= 1:
            continue

        bt.logging.info(f"Detected UIDs {uids} sharing GitHub account")
        for uid in uids:
            bt.logging.info(f"PENALTY: Zeroing score for duplicate uid {uid}")
            miner_evaluations[uid] = MinerEvaluation(uid=uid, hotkey=miner_evaluations[uid].hotkey)
            duplicate_count += 1

    bt.logging.info(f"Total duplicate miners penalized: {duplicate_count}")


def validate_response_and_initialize_miner_evaluation(uid: int, response: GitPatSynapse) -> MinerEvaluation:

    miner_eval = MinerEvaluation(uid=uid, hotkey=response.axon.hotkey)

    if uid == RECYCLE_UID:
        miner_eval.set_invalid_response_reason("SPECIAL CASE UID 0 - RECYCLE UID")
        return miner_eval

    if not response:
        miner_eval.set_invalid_response_reason(f"No response provided by miner {uid}")
        return miner_eval

    github_id, error = _validate_github_credentials(uid, response.github_access_token)
    if error:
        miner_eval.set_invalid_response_reason(error)
        return miner_eval

    miner_eval.github_id = github_id
    miner_eval.github_pat = response.github_access_token
    return miner_eval


def _validate_github_credentials(uid: int, pat: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Validate PAT and return (github_id, error_reason) tuple."""
    if not pat:
        return None, f"No Github PAT provided by miner {uid}"
    
    github_id = get_github_id(pat)
    if not github_id:
        return None, f"No Github id found for miner {uid}'s PAT"
    
    account_age = get_github_account_age_days(pat)
    if not account_age:
        return None, f"Could not determine Github account age for miner {uid}"
    if account_age < MIN_GITHUB_ACCOUNT_AGE:
        return None, f"Miner {uid}'s Github account too young ({account_age} < {MIN_GITHUB_ACCOUNT_AGE} days)"
    
    return github_id, None