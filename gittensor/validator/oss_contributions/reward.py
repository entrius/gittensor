# The MIT License (MIT)
# Copyright © 2025 Entrius
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.utils.github_api_tools import load_miners_prs
from gittensor.validator import pat_storage
from gittensor.validator.oss_contributions.inspections import (
    detect_and_penalize_miners_sharing_github,
    validate_response_and_initialize_miner_evaluation,
)
from gittensor.validator.oss_contributions.normalize import normalize_rewards_linear
from gittensor.validator.oss_contributions.scoring import (
    finalize_miner_scores,
    score_miner_prs,
)
from gittensor.validator.utils.load_weights import LanguageConfig, RepositoryConfig, TokenConfig

# NOTE: there was a circular import error, needed this if to resolve it
if TYPE_CHECKING:
    from neurons.validator import Validator


async def evaluate_miners_pull_requests(
    uid: int,
    hotkey: str,
    pat: Optional[str],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    stale_hotkey: Optional[str] = None,
) -> MinerEvaluation:
    """
    Entry point from taking a miners response -> Get PRs -> Score PRs

    Args:
        uid: The uid of the miner being evaluated
        hotkey: The miner's hotkey
        pat: The miner's GitHub PAT (from local storage), or None if not available
        master_repositories: The incentivized repositories and their RepositoryConfig objects
        programming_languages: The programming languages and their weights
        token_config: Token-based scoring weights configuration
        stale_hotkey: If set, the UID has a stored PAT from this old hotkey (re-registration detected)

    Returns:
        MinerEvaluation: The object containing scores, valid_prs, etc.
    """

    bt.logging.info(f'******* Reward function called for UID: {uid} *******')

    miner_eval = validate_response_and_initialize_miner_evaluation(uid, hotkey, pat, stale_hotkey=stale_hotkey)
    if miner_eval.failed_reason is not None:
        bt.logging.info(f'UID {uid} not being evaluated: {miner_eval.failed_reason}')
        return miner_eval

    load_miners_prs(miner_eval, master_repositories)

    score_miner_prs(miner_eval, master_repositories, programming_languages, token_config)

    # Clear PAT after scoring to avoid storing sensitive data in memory
    miner_eval.github_pat = None

    bt.logging.info('*' * 50 + '\n')
    return miner_eval


async def get_rewards(
    self: Validator,
    uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation], set]:
    """Score OSS contributions for all miners.

    Returns:
        Tuple of (normalized_rewards_array, miner_evaluations, cached_uids).
        DB storage and emission blending are handled by the caller (forward.py).
    """

    bt.logging.info(f'UIDs: {uids}')

    # Snapshot PATs once at the start of the scoring round.
    # Mid-round broadcasts update the JSON file but do not affect this round.
    all_pats = pat_storage.load_all_pats()
    pat_by_uid = {entry['uid']: entry for entry in all_pats}

    bt.logging.info(f'PAT storage snapshot: {len(pat_by_uid)} miners have stored PATs')

    miner_evaluations: Dict[int, MinerEvaluation] = {}

    # Look up PATs and calculate score.
    for uid in uids:
        hotkey = self.metagraph.hotkeys[uid]
        pat_entry = pat_by_uid.get(uid)
        pat = None
        stale_hotkey = None
        if pat_entry:
            if pat_entry.get('hotkey') == hotkey:
                pat = pat_entry['pat']
            else:
                stale_hotkey = pat_entry.get('hotkey')

        # Calculate score
        miner_evaluation = await evaluate_miners_pull_requests(
            uid,
            hotkey,
            pat,
            master_repositories,
            programming_languages,
            token_config,
            stale_hotkey=stale_hotkey,
        )
        miner_evaluations[uid] = miner_evaluation

    # If evaluation of miner was successful, store to cache, if api failure, fallback to previous successful evaluation if any
    cached_uids = self.store_or_use_cached_evaluation(miner_evaluations)

    # Adjust scores for duplicate accounts
    detect_and_penalize_miners_sharing_github(miner_evaluations)

    # Remove penalized UIDs from cached_uids so their zeroed score is written to DB
    penalized_uids = {
        uid
        for uid, evaluation in miner_evaluations.items()
        if evaluation.total_score == 0.0 and evaluation.failed_reason is None and uid in cached_uids
    }
    cached_uids -= penalized_uids

    # Finalize scores: apply eligibility gate, credibility, pioneer dividends, collateral
    finalize_miner_scores(miner_evaluations)

    # Normalize the rewards between [0,1] — single flat pool
    normalized_rewards = normalize_rewards_linear(miner_evaluations)

    return (
        np.array([normalized_rewards.get(uid, 0.0) for uid in sorted(uids)]),
        miner_evaluations,
        cached_uids,
    )
