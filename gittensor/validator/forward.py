# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation, MinerEvaluationCache
from gittensor.constants import (
    EMISSION_SHARE_TOLERANCE,
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.utils.uids import get_all_uids
from gittensor.validator.issue_competitions.forward import issue_competitions
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

    Emission blending:
    - Combined scoring pool: 90%, allocated by repository emission_share
    - Issue treasury:       10%, flat to UID 111
    - Recycle:              registry slack and inactive repo slices to UID 0
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # 1. Score OSS contributions
        miner_evaluations, cached_uids, penalized_uids = await oss_contributions(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        # 2. Score issue discovery
        await issue_discovery(
            miner_evaluations,
            master_repositories,
            programming_languages,
            token_config,
            evaluation_cache=self.evaluation_cache,
        )

        # cached UIDs now have fresh issue-discovery fields — persist them
        cached_uids.clear()

        # 3. Issue bounties verification
        await issue_competitions(self, miner_evaluations)

        # 4. Store all evaluations to DB (includes issue discovery fields)
        await self.bulk_store_evaluation(miner_evaluations, skip_uids=cached_uids)

        # 5. Allocate repo-bounded emission shares into final rewards
        rewards = blend_emission_pools(miner_evaluations, master_repositories, miner_uids)

        self.update_scores(rewards, miner_uids, blacklisted_uids=sorted(penalized_uids))

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(
    self: 'Validator',
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
) -> Tuple[Dict[int, MinerEvaluation], Set[int], Set[int]]:
    """Score OSS contributions and return miner evaluations + cached UIDs + penalized UIDs.

    Pure scoring — no DB storage or emission blending. Those are handled by forward().
    """
    tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

    bt.logging.info('***** Starting scoring round *****')
    bt.logging.info(f'Total Repositories loaded: {len(master_repositories)}')
    bt.logging.info(f'Total Languages loaded: {len(programming_languages)}')
    bt.logging.info(f'Token config: {tree_sitter_count} tree-sitter languages')
    bt.logging.info(f'Neurons to evaluate: {len(miner_uids)}')

    miner_evaluations, cached_uids, penalized_uids = await get_rewards(
        self, miner_uids, master_repositories, programming_languages, token_config
    )

    return miner_evaluations, cached_uids, penalized_uids


async def issue_discovery(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
    evaluation_cache: Optional[MinerEvaluationCache] = None,
) -> None:
    """Score issue discovery fields on miner evaluations.

    Uses ``MirrorClient.get_miner_issues`` with authoritative ``solved_by_pr`` +
    inline ``solving_pr`` data, and a cross-miner cache of already-scored
    solving PRs so the base_score reflects real token scoring.
    """
    await run_issue_discovery(
        miner_evaluations,
        master_repositories,
        programming_languages,
        token_config,
        evaluation_cache=evaluation_cache,
    )


def blend_emission_pools(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
) -> np.ndarray:
    """Allocate the combined scoring pool by bounded repository emission_share.

    Each repo's ``emission_share * OSS_EMISSION_SHARE`` slice is distributed
    only within that repo. PR and issue-discovery sub-slices are split by the
    repo's ``issue_discovery_share`` and spill only inside the same repo when
    exactly one side has eligible non-zero scorers. Empty repo slices and
    registry slack recycle to UID 0.
    """
    sorted_uids = sorted(miner_uids)
    uid_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids))

    total_configured_share = sum(config.emission_share for config in master_repositories.values())
    recycle_share = max(0.0, 1.0 - total_configured_share) * OSS_EMISSION_SHARE

    for repo_name, repo_config in master_repositories.items():
        repo_slice = repo_config.emission_share * OSS_EMISSION_SHARE
        if repo_slice <= 0:
            continue

        issue_share = repo_config.issue_discovery_share
        pr_scores = _collect_repo_pr_scores(miner_evaluations, repo_name, miner_uids) if issue_share < 1.0 else {}
        issue_scores = (
            _collect_repo_issue_discovery_scores(miner_evaluations, repo_name, miner_uids) if issue_share > 0.0 else {}
        )

        pr_total = sum(pr_scores.values())
        issue_total = sum(issue_scores.values())

        if pr_total <= 0 and issue_total <= 0:
            recycle_share += repo_slice
            continue

        if pr_total > 0 and issue_total > 0:
            recycle_share += _allocate_scores_to_rewards(
                rewards,
                uid_index,
                pr_scores,
                repo_slice * (1.0 - issue_share),
            )
            recycle_share += _allocate_scores_to_rewards(rewards, uid_index, issue_scores, repo_slice * issue_share)
        elif pr_total > 0:
            recycle_share += _allocate_scores_to_rewards(rewards, uid_index, pr_scores, repo_slice)
        else:
            recycle_share += _allocate_scores_to_rewards(rewards, uid_index, issue_scores, repo_slice)

    # Issue treasury (10% flat to UID 111)
    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    # Recycle receives registry slack and empty repo slices.
    if RECYCLE_UID in miner_uids:
        recycle_idx = sorted_uids.index(RECYCLE_UID)
        rewards[recycle_idx] += recycle_share
        if recycle_share > EMISSION_SHARE_TOLERANCE:
            bt.logging.info(f'Recycling {recycle_share * 100:.0f}% unclaimed emissions from repo allocation')

    return rewards


def _collect_repo_pr_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
    repo_name: str,
    miner_uids: set[int],
) -> Dict[int, float]:
    scores: Dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        if uid not in miner_uids:
            continue

        earned = sum(
            pr.earned_score
            for pr in evaluation.merged_prs
            if pr.repository_full_name == repo_name and pr.earned_score > 0
        )
        collateral = sum(
            pr.collateral_score
            for pr in evaluation.open_prs
            if pr.repository_full_name == repo_name and pr.collateral_score > 0
        )
        score = max(0.0, earned - collateral)
        if score > 0:
            scores[uid] = score

    return scores


def _collect_repo_issue_discovery_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
    repo_name: str,
    miner_uids: set[int],
) -> Dict[int, float]:
    scores: Dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        if uid not in miner_uids:
            continue

        score = sum(
            issue.discovery_earned_score
            for issue in evaluation.issue_discovery_issues
            if issue.repository_full_name == repo_name and issue.discovery_earned_score > 0
        )
        if score > 0:
            scores[uid] = score

    return scores


def _allocate_scores_to_rewards(
    rewards: np.ndarray,
    uid_index: Dict[int, int],
    scores: Dict[int, float],
    allocation: float,
) -> float:
    if allocation <= 0:
        return 0.0

    total = sum(scores.values())
    if total <= 0:
        return allocation

    unallocated = 0.0
    for uid, score in scores.items():
        share = allocation * (score / total)
        idx = uid_index.get(uid)
        if idx is None:
            unallocated += share
        else:
            rewards[idx] += share

    return unallocated
