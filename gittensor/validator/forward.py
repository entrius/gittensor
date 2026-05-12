# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation, MinerEvaluationCache
from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.utils.uids import get_all_uids
from gittensor.validator.issue_competitions.forward import issue_competitions
from gittensor.validator.issue_discovery.normalize import (
    normalize_issue_discovery_rewards,
)
from gittensor.validator.issue_discovery.scan import run_issue_discovery
from gittensor.validator.oss_contributions.reward import get_rewards
from gittensor.validator.utils.config import (
    VALIDATOR_STEPS_INTERVAL,
    VALIDATOR_WAIT,
)
from gittensor.validator.utils.load_weights import (
    RepositoryConfig,
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
)

if TYPE_CHECKING:
    from neurons.validator import Validator


async def forward(self: 'Validator') -> None:
    """Execute the validator's forward pass.

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps:
    1. Score OSS contributions (mirror PR scoring)
    2. Score issue discovery
    3. Run issue bounties verification
    4. Store all evaluations to DB
    5. Blend emission pools and update scores

    Emission blending (hardcoded per-competition):
    - OSS contributions: 30%
    - Issue discovery:   30%
    - Issue treasury:    15% (flat to UID 111)
    - Recycle:           25% (flat to UID 0)
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # 1. Score OSS contributions
        _, miner_evaluations, cached_uids, penalized_uids = await oss_contributions(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        # 2. Score issue discovery
        _ = await issue_discovery(
            miner_evaluations,
            master_repositories,
            programming_languages,
            token_config,
            miner_uids,
            evaluation_cache=self.evaluation_cache,
        )

        # cached UIDs now have fresh issue-discovery fields — persist them
        cached_uids.clear()

        # 3. Issue bounties verification
        await issue_competitions(self, miner_evaluations)

        # 4. Store all evaluations to DB (includes issue discovery fields)
        await self.bulk_store_evaluation(miner_evaluations, skip_uids=cached_uids)

        # 5. Blend 4 emission pools into final rewards
        rewards = blend_emission_pools(miner_evaluations, miner_uids, master_repositories)

        self.update_scores(rewards, miner_uids, blacklisted_uids=sorted(penalized_uids))

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(
    self: 'Validator',
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation], Set[int], Set[int]]:
    """Score OSS contributions and return normalized rewards + miner evaluations + cached UIDs + penalized UIDs.

    Pure scoring — no DB storage or emission blending. Those are handled by forward().
    """
    tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

    bt.logging.info('***** Starting scoring round *****')
    bt.logging.info(f'Total Repositories loaded: {len(master_repositories)}')
    bt.logging.info(f'Total Languages loaded: {len(programming_languages)}')
    bt.logging.info(f'Token config: {tree_sitter_count} tree-sitter languages')
    bt.logging.info(f'Neurons to evaluate: {len(miner_uids)}')

    rewards, miner_evaluations, cached_uids, penalized_uids = await get_rewards(
        self, miner_uids, master_repositories, programming_languages, token_config
    )

    return rewards, miner_evaluations, cached_uids, penalized_uids


async def issue_discovery(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
    miner_uids: set[int],
    evaluation_cache: Optional[MinerEvaluationCache] = None,
) -> np.ndarray:
    """Score issue discovery and return normalized rewards array.

    Uses ``MirrorClient.get_miner_issues`` with authoritative ``solved_by_pr`` +
    inline ``solving_pr`` data, and a cross-miner cache of already-scored
    solving PRs so the base_score reflects real token scoring.

    Returns numpy array of normalized issue discovery rewards (sorted by UID).
    """
    await run_issue_discovery(
        miner_evaluations,
        master_repositories,
        programming_languages,
        token_config,
        evaluation_cache=evaluation_cache,
    )

    issue_rewards_dict = normalize_issue_discovery_rewards(miner_evaluations)

    sorted_uids = sorted(miner_uids)
    return np.array([issue_rewards_dict.get(uid, 0.0) for uid in sorted_uids])


def blend_emission_pools(
    miner_evaluations: Dict[int, MinerEvaluation],
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
) -> np.ndarray:
    """Blend the round emission pools using per-repo emission shares.

    - OSS scoring pool: 90% distributed by repo emission_share
    - Treasury: 10% flat to UID 111
    - Recycle: natural slack from unallocated registry share + unclaimed repo slices
    """
    sorted_uids = sorted(miner_uids)
    rewards = np.zeros(len(sorted_uids))
    uid_to_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    recycle_extra = 0.0

    total_registry_share = sum(repo.emission_share for repo in master_repositories.values())
    if total_registry_share < 1.0:
        recycle_extra += OSS_EMISSION_SHARE * (1.0 - total_registry_share)

    for repo_name, repo_config in master_repositories.items():
        repo_slice = OSS_EMISSION_SHARE * repo_config.emission_share
        if repo_slice <= 0.0:
            continue

        pr_scores: Dict[int, float] = {}
        issue_scores: Dict[int, float] = {}
        for uid, evaluation in miner_evaluations.items():
            pr_score = sum(
                pr.earned_score
                for pr in evaluation.merged_prs
                if pr.repository_full_name == repo_name and pr.earned_score > 0
            )
            if pr_score > 0:
                pr_scores[uid] = pr_score

            issue_score = evaluation.issue_discovery_scores_by_repo.get(repo_name, 0.0)
            if issue_score > 0:
                issue_scores[uid] = issue_score

        pr_total = sum(pr_scores.values())
        issue_total = sum(issue_scores.values())

        if pr_total <= 0.0 and issue_total <= 0.0:
            recycle_extra += repo_slice
            continue

        issue_subslice = repo_slice * repo_config.issue_discovery_share
        pr_subslice = repo_slice * (1.0 - repo_config.issue_discovery_share)

        if pr_total <= 0.0:
            issue_subslice += pr_subslice
            pr_subslice = 0.0
        elif issue_total <= 0.0:
            pr_subslice += issue_subslice
            issue_subslice = 0.0

        if pr_subslice > 0.0 and pr_total > 0.0:
            for uid, score in pr_scores.items():
                idx = uid_to_index.get(uid)
                if idx is not None:
                    rewards[idx] += pr_subslice * (score / pr_total)

        if issue_subslice > 0.0 and issue_total > 0.0:
            for uid, score in issue_scores.items():
                idx = uid_to_index.get(uid)
                if idx is not None:
                    rewards[idx] += issue_subslice * (score / issue_total)

    # Treasury pool (10% flat to UID 111)
    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
    else:
        recycle_extra += ISSUES_TREASURY_EMISSION_SHARE

    # Recycle pool: unclaimed repo slices + registry slack (+ treasury fallback)
    if RECYCLE_UID in miner_uids:
        recycle_idx = sorted_uids.index(RECYCLE_UID)
        rewards[recycle_idx] += recycle_extra

    return rewards
