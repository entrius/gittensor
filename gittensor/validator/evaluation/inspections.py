# The MIT License (MIT)
# Copyright © 2025 Entrius

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import bittensor as bt

from gittensor.classes import GitPatSynapse, MinerEvaluation
from gittensor.constants import (
    MIN_GITHUB_ACCOUNT_AGE,
    RECYCLE_UID,
)
from gittensor.utils.github_api_tools import (
    get_github_account_age_days,
    get_github_id,
)


def detect_and_penalize_miners_sharing_github(miner_evaluations: Dict[int, MinerEvaluation]):
    """
    Detects miners that used the same github, duplicated across multiple uids.
    Will then penalize detected 'duplicate miners' with a score of 0.0.
    All miners sharing a GitHub account will be penalized equally.

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Mapping of miner UID to their MinerEvaluation.
    """

    bt.logging.info("Now checking for duplicate users across miners...")

    github_id_to_uids: Dict[str, List[int]] = defaultdict(list)

    for uid, evaluation in miner_evaluations.items():
        if evaluation.github_id and evaluation.github_id != '0':
            github_id_to_uids[evaluation.github_id].append(uid)

    duplicate_count = 0
    for _, uids in github_id_to_uids.items():
        if len(uids) <= 1:
            continue

        bt.logging.info(f"Detected UIDs {uids} sharing GitHub account")
        for uid in uids:
            bt.logging.info(f"PENALTY: Zeroing score for duplicate uid {uid}")
            miner_evaluations[uid] = MinerEvaluation(uid=uid, hotkey=miner_evaluations[uid].hotkey)
            duplicate_count += 1

    bt.logging.info(f"Total duplicate miners penalized: {duplicate_count}")


def validate_response_and_initialize_miner_evaluation(uid: int, response: GitPatSynapse) -> MinerEvaluation:
    """
    Validate a miner's response and initialize their evaluation object.

    Args:
        uid: The miner's unique identifier
        response: The GitPatSynapse response from the miner (may be None if miner didn't respond)

    Returns:
        MinerEvaluation: Initialized evaluation object with failure reason if validation failed
    """
    # Handle special recycle UID case first
    if uid == RECYCLE_UID:
        return MinerEvaluation(uid=uid, hotkey="", failed_reason="SPECIAL CASE UID 0 - RECYCLE UID")

    # Check for null response before accessing any attributes to prevent crashes
    if not response or not response.axon:
        return MinerEvaluation(uid=uid, hotkey="", failed_reason=f"No response provided by miner {uid}")

    # Now safe to access response.axon.hotkey
    miner_eval = MinerEvaluation(uid=uid, hotkey=response.axon.hotkey)

    github_id, error = validate_github_credentials(uid, response.github_access_token)
    if error:
        miner_eval.set_invalid_response_reason(error)
        return miner_eval

    miner_eval.github_id = github_id
    miner_eval.github_pat = response.github_access_token
    return miner_eval


def validate_github_credentials(uid: int, pat: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
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

