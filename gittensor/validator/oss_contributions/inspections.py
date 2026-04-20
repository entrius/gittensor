# The MIT License (MIT)
# Copyright © 2025 Entrius

from collections import defaultdict
from typing import Dict, List, Optional, Set

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.constants import RECYCLE_UID
from gittensor.validator.utils.github_validation import validate_github_credentials


def detect_and_penalize_miners_sharing_github(miner_evaluations: Dict[int, MinerEvaluation]) -> Set[int]:
    """
    Detects miners that used the same github, duplicated across multiple uids.
    Will then penalize detected 'duplicate miners' with a score of 0.0.
    All miners sharing a GitHub account will be penalized equally.

    Args:
        miner_evaluations (Dict[int, MinerEvaluation]): Mapping of miner UID to their MinerEvaluation.

    Returns:
        Set of UIDs that were penalized (score zeroed).
    """

    bt.logging.info('Now checking for duplicate users across miners...')

    github_id_to_uids: Dict[str, List[int]] = defaultdict(list)

    for uid, evaluation in miner_evaluations.items():
        if evaluation.github_id and evaluation.github_id != '0':
            github_id_to_uids[evaluation.github_id].append(uid)

    penalized_uids: Set[int] = set()
    for _, uids in github_id_to_uids.items():
        if len(uids) <= 1:
            continue

        bt.logging.info(f'Detected UIDs {uids} sharing GitHub account')
        for uid in uids:
            bt.logging.info(f'PENALTY: Zeroing score for duplicate uid {uid}')
            miner_evaluations[uid] = MinerEvaluation(uid=uid, hotkey=miner_evaluations[uid].hotkey)
            penalized_uids.add(uid)

    bt.logging.info(f'Total duplicate miners penalized: {len(penalized_uids)}')
    return penalized_uids


def validate_response_and_initialize_miner_evaluation(
    uid: int, hotkey: str, pat: Optional[str], stale_hotkey: Optional[str] = None
) -> MinerEvaluation:
    """
    Validate a miner's stored PAT and initialize their evaluation object.

    Args:
        uid: The miner's unique identifier
        hotkey: The miner's hotkey
        pat: The miner's GitHub PAT from local storage (may be None if not stored)
        stale_hotkey: If set, the UID has a stored PAT from this old hotkey (re-registration detected)

    Returns:
        MinerEvaluation: Initialized evaluation object with failure reason if validation failed
    """
    # Handle special recycle UID case first
    if uid == RECYCLE_UID:
        return MinerEvaluation(uid=uid, hotkey='', failed_reason='SPECIAL CASE UID 0 - RECYCLE UID')

    if not hotkey:
        return MinerEvaluation(uid=uid, hotkey='', failed_reason=f'No hotkey for miner {uid}')

    if not pat:
        if stale_hotkey:
            reason = (
                f'New miner registered on UID {uid}: '
                f'hotkey changed {stale_hotkey[:16]}... → {hotkey[:16]}... — miner must run `gitt miner post`'
            )
        else:
            reason = f'No stored PAT for miner {uid} — miner must run `gitt miner post`'
        return MinerEvaluation(uid=uid, hotkey=hotkey, failed_reason=reason)

    miner_eval = MinerEvaluation(uid=uid, hotkey=hotkey)

    github_id, error = validate_github_credentials(uid, pat)
    if error:
        miner_eval.failed_reason = error
        return miner_eval

    miner_eval.github_id = github_id
    miner_eval.github_pat = pat
    return miner_eval
