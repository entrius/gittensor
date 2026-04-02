# The MIT License (MIT)
# Copyright © 2025 Entrius
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.utils.github_api_tools import load_miners_prs
from gittensor.validator import pat_storage
from gittensor.validator.oss_contributions.dynamic_emissions import apply_dynamic_emissions_using_network_contributions
from gittensor.validator.oss_contributions.inspections import (
    detect_and_penalize_miners_sharing_github,
    validate_response_and_initialize_miner_evaluation,
)
from gittensor.validator.oss_contributions.normalize import normalize_rewards_linear
from gittensor.validator.oss_contributions.scoring import (
    finalize_miner_scores,
    score_miner_prs,
)
from gittensor.validator.oss_contributions.tier_config import allocate_emissions_by_tier
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
) -> MinerEvaluation:
    """
    Entry point from taking a miners response -> Get PRs -> Score PRs by tier

    Args:
        uid: The uid of the miner being evaluated
        hotkey: The miner's hotkey
        pat: The miner's GitHub PAT (from local storage), or None if not available
        master_repositories: The incentivized repositories and their RepositoryConfig objects
        programming_languages: The programming languages and their weights
        token_config: Token-based scoring weights configuration

    Returns:
        MinerEvaluation: The object containing scores, valid_prs, etc.
    """

    bt.logging.info(f'******* Reward function called for UID: {uid} *******')

    miner_eval = validate_response_and_initialize_miner_evaluation(uid, hotkey, pat)
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
        if pat_entry:
            if pat_entry.get('hotkey') == hotkey:
                pat = pat_entry['pat']
            else:
                bt.logging.info(f'UID {uid}: stale PAT entry (hotkey mismatch) — miner must re-broadcast')

        # Calculate score
        miner_evaluation = await evaluate_miners_pull_requests(
            uid, hotkey, pat, master_repositories, programming_languages, token_config
        )
        miner_evaluations[uid] = miner_evaluation

    # If evaluation of miner was successful, store to cache, if api failure, fallback to previous successful evaluation if any
    cached_uids = self.store_or_use_cached_evaluation(miner_evaluations)

    # Adjust scores for duplicate accounts
    detect_and_penalize_miners_sharing_github(miner_evaluations)

    # Finalize scores: apply pioneer dividends, credibility, sum totals, deduct collateral
    finalize_miner_scores(miner_evaluations)

    # Allocate emissions by tier: replace total_score with tier-weighted allocations
    allocate_emissions_by_tier(miner_evaluations)

    # Normalize the rewards between [0,1]
    normalized_rewards = normalize_rewards_linear(miner_evaluations)

    # Scale rewards according to dynamic emission curve based off of miners total contributions.
    final_rewards = apply_dynamic_emissions_using_network_contributions(normalized_rewards, miner_evaluations)

    # Store miner evaluations after calculating all scores
    await self.bulk_store_evaluation(miner_evaluations, skip_uids=cached_uids)

    return (
        np.array([final_rewards.get(uid, 0.0) for uid in sorted(uids)]),
        miner_evaluations,
    )
