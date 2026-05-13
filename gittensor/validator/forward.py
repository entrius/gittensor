# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation, MinerEvaluationCache
from gittensor.constants import ISSUES_TREASURY_EMISSION_SHARE, ISSUES_TREASURY_UID, OSS_EMISSION_SHARE, RECYCLE_UID
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
    5. Allocate repo emission slices and update scores

    Emission allocation:
    - Combined scoring pool: 90%, allocated by repo emission_share
    - Issue treasury:       10% (flat to UID 111)
    - Recycle:              unclaimed repo slices and registry slack to UID 0
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
        await issue_discovery(
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

        # 5. Allocate repo emission slices into final rewards
        rewards = blend_emission_pools(miner_uids, miner_evaluations, master_repositories)

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
    miner_uids: set[int],
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
) -> np.ndarray:
    """Allocate the combined scoring pool by repo emission_share.

    Allocation is repo-first: each active repo receives exactly its configured
    slice of ``OSS_EMISSION_SHARE`` and distributes it within the repo by raw score.
    """
    sorted_uids = sorted(miner_uids)
    rewards = np.zeros(len(sorted_uids))

    rewards += _allocate_repo_scoring_pool(sorted_uids, miner_evaluations, master_repositories)

    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    return rewards


def _allocate_repo_scoring_pool(
    sorted_uids: list[int],
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
) -> np.ndarray:
    rewards = np.zeros(len(sorted_uids))
    uid_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    recycle_idx = uid_index.get(RECYCLE_UID)
    allocated_share = 0.0

    for repo_name, repo_config in master_repositories.items():
        repo_key = repo_name.lower()
        repo_share = repo_config.emission_share
        allocated_share += repo_share
        repo_slice = OSS_EMISSION_SHARE * repo_share
        if repo_slice <= 0:
            continue

        pr_scores: dict[int, float] = {}
        issue_scores: dict[int, float] = {}
        issue_share = repo_config.issue_discovery_share
        pr_share = 1.0 - issue_share
        for uid, evaluation in miner_evaluations.items():
            if uid not in uid_index:
                continue
            if pr_share > 0:
                pr_score = _repo_pr_score(evaluation, repo_key)
                if pr_score > 0:
                    pr_scores[uid] = pr_score
            if issue_share > 0:
                for issue in evaluation.discovered_issues:
                    if issue.repository_full_name.lower() == repo_key and issue.discovery_earned_score > 0:
                        issue_scores[uid] = issue_scores.get(uid, 0.0) + float(issue.discovery_earned_score)

        pr_total = sum(pr_scores.values())
        issue_total = sum(issue_scores.values())
        if pr_total <= 0 and issue_total <= 0:
            if recycle_idx is not None:
                rewards[recycle_idx] += repo_slice
            continue

        pr_slice = repo_slice * pr_share
        issue_slice = repo_slice * issue_share
        if pr_total <= 0:
            issue_slice += pr_slice
            pr_slice = 0.0
        elif issue_total <= 0:
            pr_slice += issue_slice
            issue_slice = 0.0

        _add_proportional_rewards(rewards, uid_index, pr_scores, pr_total, pr_slice)
        _add_proportional_rewards(rewards, uid_index, issue_scores, issue_total, issue_slice)

    slack_share = max(0.0, 1.0 - allocated_share)
    if slack_share > 0 and recycle_idx is not None:
        rewards[recycle_idx] += OSS_EMISSION_SHARE * slack_share

    return rewards


def _repo_pr_score(evaluation: MinerEvaluation, repo_name: str) -> float:
    return sum(
        float(pr.earned_score)
        for pr in evaluation.merged_prs
        if pr.repository_full_name.lower() == repo_name and pr.earned_score > 0
    )


def _add_proportional_rewards(
    rewards: np.ndarray,
    uid_index: dict[int, int],
    scores: dict[int, float],
    total: float,
    amount: float,
) -> None:
    if total <= 0 or amount <= 0:
        return
    for uid, score in scores.items():
        idx = uid_index.get(uid)
        if idx is not None:
            rewards[idx] += amount * score / total
