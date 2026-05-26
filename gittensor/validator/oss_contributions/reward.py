# The MIT License (MIT)
# Copyright © 2025 Entrius
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.utils.mirror.client import MirrorClient
from gittensor.validator import pat_storage
from gittensor.validator.oss_contributions.inspections import (
    detect_and_penalize_miners_sharing_github,
    validate_response_and_initialize_miner_evaluation,
)
from gittensor.validator.oss_contributions.mirror.load import load_miner_prs
from gittensor.validator.oss_contributions.mirror.scoring import score_miner_prs
from gittensor.validator.oss_contributions.scoring import finalize_miner_scores
from gittensor.validator.utils.config import MINER_EVALUATION_CONCURRENCY
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
    stored_github_id: Optional[str] = None,
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
        stored_github_id: GitHub id recorded when the stored PAT was accepted

    Returns:
        MinerEvaluation: The object containing scores, valid_prs, etc.
    """

    bt.logging.info(f'******* Reward function called for UID: {uid} *******')

    miner_eval = validate_response_and_initialize_miner_evaluation(
        uid,
        hotkey,
        pat,
        stale_hotkey=stale_hotkey,
        stored_github_id=stored_github_id,
    )
    if miner_eval.failed_reason is not None:
        bt.logging.info(f'UID {uid} not being evaluated: {miner_eval.failed_reason}')
        return miner_eval

    if miner_eval.github_pr_fetch_failed:
        bt.logging.warning(f'UID {uid}: GitHub identity lookup failed transiently; deferring to cache fallback')
        return miner_eval

    with MirrorClient() as mirror_client:
        await asyncio.to_thread(load_miner_prs, miner_eval, master_repositories, client=mirror_client)
        await score_miner_prs(
            miner_eval, master_repositories, programming_languages, token_config, client=mirror_client
        )

    bt.logging.info('*' * 50 + '\n')
    return miner_eval


async def get_rewards(
    self: Validator,
    uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
) -> Tuple[Dict[int, MinerEvaluation], Set[int], Set[int]]:
    """Score OSS contributions for all miners.

    Returns:
        Tuple of (miner_evaluations, cached_uids, penalized_uids). DB storage
        and repo-bounded emission blending are handled by the caller
        (forward.py).
    """

    bt.logging.info(f'UIDs: {uids}')

    # Snapshot PATs once at the start of the scoring round.
    # Mid-round broadcasts update the JSON file but do not affect this round.
    all_pats = pat_storage.load_all_pats()
    pat_by_uid = {entry['uid']: entry for entry in all_pats}

    bt.logging.info(f'PAT storage snapshot: {len(pat_by_uid)} miners have stored PATs')

    semaphore = asyncio.Semaphore(MINER_EVALUATION_CONCURRENCY)

    async def evaluate_one(uid: int) -> Tuple[int, MinerEvaluation]:
        hotkey = self.metagraph.hotkeys[uid]
        pat_entry = pat_by_uid.get(uid)
        pat = None
        stale_hotkey = None
        stored_github_id = None
        if pat_entry:
            if pat_entry.get('hotkey') == hotkey:
                pat = pat_entry['pat']
                stored_github_id = pat_entry.get('github_id')
            else:
                stale_hotkey = pat_entry.get('hotkey')

        async with semaphore:
            evaluation = await evaluate_miners_pull_requests(
                uid,
                hotkey,
                pat,
                master_repositories,
                programming_languages,
                token_config,
                stale_hotkey=stale_hotkey,
                stored_github_id=stored_github_id,
            )
        return uid, evaluation

    # Score miners concurrently (bounded by the semaphore) so the mirror's
    # per-request latency overlaps across miners instead of summing. Each miner
    # uses its own MirrorClient, so the tasks share no mutable state.
    results = await asyncio.gather(*(evaluate_one(uid) for uid in uids))
    miner_evaluations: Dict[int, MinerEvaluation] = dict(results)

    # If evaluation of miner was successful, store to cache, if api failure, fallback to previous successful evaluation if any
    cached_uids = self.store_or_use_cached_evaluation(miner_evaluations)

    # Adjust scores for duplicate accounts; returns penalized UIDs so they are
    # removed from cached_uids and their zeroed scores are written to DB.
    penalized_uids = detect_and_penalize_miners_sharing_github(miner_evaluations)
    cached_uids -= penalized_uids
    # The cache store path ran before the penalty. Drop those snapshots so a
    # future fetch failure cannot restore pre-penalty PR scores.
    if penalized_uids:
        self.evaluation_cache.evict_many(penalized_uids)

    # Finalize scores: apply eligibility gate, credibility, collateral
    finalize_miner_scores(miner_evaluations, master_repositories)

    return miner_evaluations, cached_uids, penalized_uids
