# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Set, Tuple

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

    Emission blending:
    - OSS scoring pool: 90%, allocated by repository emission_share
    - Issue treasury: 10% (flat to UID 111)
    - Recycle: registry slack and inactive repo slices
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # 1. Score OSS contributions
        _oss_rewards, miner_evaluations, cached_uids, penalized_uids = await oss_contributions(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        # 2. Score issue discovery
        _issue_rewards = await issue_discovery(
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

        # 5. Allocate the scoring pool by per-repo emission_share
        rewards = blend_emission_pools(miner_evaluations, master_repositories, miner_uids)

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
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
) -> np.ndarray:
    """Allocate emissions by configured repo slices and route slack to recycle.

    Each repository receives at most ``emission_share * OSS_EMISSION_SHARE``.
    That repo slice is divided proportionally by raw PR and issue-discovery
    scores inside the repo. Registry slack and repo slices with no enabled
    nonzero scorers route to the recycle UID.
    """
    sorted_uids = sorted(miner_uids)
    rewards = np.zeros(len(sorted_uids))
    uid_index = {uid: idx for idx, uid in enumerate(sorted_uids)}

    recycle_amount = allocate_repo_scoring_pool(rewards, uid_index, miner_evaluations, master_repositories)

    # Issue treasury (10% flat to UID 111)
    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = uid_index[ISSUES_TREASURY_UID]
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    # Recycle receives registry slack plus unclaimed repo slices. There is no
    # fixed recycle baseline under the emission_share allocation model.
    if RECYCLE_UID in miner_uids:
        recycle_idx = uid_index[RECYCLE_UID]
        rewards[recycle_idx] += recycle_amount
        if recycle_amount > 0:
            bt.logging.info(f'Recycling {recycle_amount * 100:.2f}% unclaimed scoring-pool emissions')

    return rewards


def allocate_repo_scoring_pool(
    rewards: np.ndarray,
    uid_index: Dict[int, int],
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
) -> float:
    """Distribute the OSS scoring pool by repository emission shares.

    Returns the amount that should be paid to the recycle UID.
    """
    pr_scores, issue_scores = _collect_repo_scores(miner_evaluations)
    configured_share = sum(config.emission_share for config in master_repositories.values())
    recycle_amount = max(0.0, 1.0 - configured_share) * OSS_EMISSION_SHARE

    if recycle_amount > 0:
        bt.logging.info(f'Registry emission_share slack: {recycle_amount * 100:.2f}% routed to recycle')

    for repo_name, config in master_repositories.items():
        repo_key = repo_name.lower()
        repo_slice = config.emission_share * OSS_EMISSION_SHARE
        if repo_slice <= 0:
            continue

        pr_entries = pr_scores.get(repo_key, [])
        issue_entries = issue_scores.get(repo_key, [])
        pr_total = sum(score for _, score in pr_entries)
        issue_total = sum(score for _, score in issue_entries)

        issue_share = config.issue_discovery_share
        pr_share = 1.0 - issue_share
        pr_active = pr_share > 0 and pr_total > 0
        issue_active = issue_share > 0 and issue_total > 0

        if not pr_active and not issue_active:
            recycle_amount += repo_slice
            continue

        if pr_active and issue_active:
            _distribute_entries(rewards, uid_index, pr_entries, repo_slice * pr_share, pr_total)
            _distribute_entries(rewards, uid_index, issue_entries, repo_slice * issue_share, issue_total)
        elif pr_active:
            _distribute_entries(rewards, uid_index, pr_entries, repo_slice, pr_total)
        else:
            _distribute_entries(rewards, uid_index, issue_entries, repo_slice, issue_total)

    return recycle_amount


def _collect_repo_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
) -> Tuple[Dict[str, list[Tuple[int, float]]], Dict[str, list[Tuple[int, float]]]]:
    pr_scores: Dict[str, list[Tuple[int, float]]] = defaultdict(list)
    issue_scores: Dict[str, list[Tuple[int, float]]] = defaultdict(list)

    for uid, evaluation in miner_evaluations.items():
        for pr in _positive_pr_scores(evaluation):
            pr_scores[pr.repository_full_name.lower()].append((uid, float(pr.earned_score)))
        for issue in _positive_issue_scores(evaluation):
            issue_scores[issue.repository_full_name.lower()].append((uid, float(issue.discovery_earned_score)))

    return pr_scores, issue_scores


def _positive_pr_scores(evaluation: MinerEvaluation) -> Iterable:
    return (pr for pr in evaluation.merged_prs if getattr(pr, 'earned_score', 0.0) > 0)


def _positive_issue_scores(evaluation: MinerEvaluation) -> Iterable:
    return (
        issue
        for issue in getattr(evaluation, 'discovered_issues', [])
        if getattr(issue, 'discovery_earned_score', 0.0) > 0
    )


def _distribute_entries(
    rewards: np.ndarray,
    uid_index: Dict[int, int],
    entries: list[Tuple[int, float]],
    allocation: float,
    total_score: float,
) -> None:
    if allocation <= 0 or total_score <= 0:
        return
    for uid, score in entries:
        idx = uid_index.get(uid)
        if idx is None:
            continue
        rewards[idx] += allocation * score / total_score
