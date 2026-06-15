# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Round-level emission allocation by repository emission shares."""

from typing import Dict, Iterator, Optional

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation, RepoEmissionAllocation
from gittensor.constants import (
    EMISSION_SHARE_TOLERANCE,
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.validator.utils.load_weights import RepositoryConfig


def blend_emission_pools(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
    maintainer_uids_by_repo: Optional[Dict[str, list[int]]] = None,
) -> np.ndarray:
    """Allocate the combined scoring pool by bounded repository emission_share.

    Each repo's ``emission_share * OSS_EMISSION_SHARE`` slice is distributed
    only within that repo. PR and issue-discovery sub-slices are split by the
    repo's ``issue_discovery_share`` and spill only inside the same repo when
    exactly one side has eligible non-zero scorers. Empty repo slices and
    registry slack recycle to UID 0.

    When a repo sets ``maintainer_cut`` and ``maintainer_uids_by_repo`` lists
    registered maintainer miners for it, ``maintainer_cut`` of that repo's slice
    is carved off the top and split evenly among those maintainers; the
    remainder scores normally. Repos with no listed maintainers are unaffected.
    """
    sorted_uids = sorted(miner_uids)
    uid_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids))

    total_configured_share = sum(config.emission_share for config in master_repositories.values())
    recycle_share = max(0.0, 1.0 - total_configured_share) * OSS_EMISSION_SHARE

    repos_allocated = 0
    repos_fully_recycled = 0
    maintainer_carve_out_total = 0.0
    rewarded_uids: set[int] = set()

    for allocation in calculate_repo_emission_breakdown(
        miner_evaluations, master_repositories, miner_uids, maintainer_uids_by_repo
    ):
        recycle_share += allocation.recycled_amount
        if allocation.maintainer_rewards or allocation.pr_rewards or allocation.issue_discovery_rewards:
            repos_allocated += 1
        else:
            repos_fully_recycled += 1
        maintainer_carve_out_total += sum(allocation.maintainer_rewards.values())
        rewarded_uids.update(allocation.maintainer_rewards, allocation.pr_rewards, allocation.issue_discovery_rewards)
        for uid, reward in allocation.maintainer_rewards.items():
            rewards[uid_index[uid]] += reward
        for uid, reward in allocation.pr_rewards.items():
            rewards[uid_index[uid]] += reward
        for uid, reward in allocation.issue_discovery_rewards.items():
            rewards[uid_index[uid]] += reward

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

    bt.logging.info('')
    bt.logging.info(
        f'Emission blend complete | {repos_allocated} repos allocated | {repos_fully_recycled} fully recycled | '
        f'{len(rewarded_uids)} miners rewarded | maintainer carve-out {maintainer_carve_out_total * 100:.1f}% | '
        f'recycled {recycle_share * 100:.1f}%'
    )

    return rewards


def calculate_repo_emission_breakdown(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
    maintainer_uids_by_repo: Optional[Dict[str, list[int]]] = None,
) -> Iterator[RepoEmissionAllocation]:
    """Return per-repository reward allocation details without adding treasury/slack."""
    for repo_name, repo_config in master_repositories.items():
        repo_slice = repo_config.emission_share * OSS_EMISSION_SHARE
        if repo_slice <= 0:
            continue

        allocation = RepoEmissionAllocation(
            repository_full_name=repo_name,
            emission_share=repo_config.emission_share,
            issue_discovery_share=repo_config.issue_discovery_share,
            repo_slice=repo_slice,
            maintainer_cut=repo_config.maintainer_cut,
        )

        # Maintainer carve-out: route maintainer_cut of the repo slice evenly to
        # the repo's registered maintainer miners, off the top before the
        # PR/issue split. Skipped when no maintainer miner is present.
        maintainer_uids = (maintainer_uids_by_repo or {}).get(repo_name) or []
        scoring_slice = repo_slice
        if repo_config.maintainer_cut > 0.0 and maintainer_uids:
            carve_out = repo_config.maintainer_cut * repo_slice
            eligible_maintainers = [uid for uid in maintainer_uids if uid in miner_uids]
            if eligible_maintainers:
                per_maintainer = carve_out / len(eligible_maintainers)
                allocation.maintainer_carve_out = carve_out
                allocation.maintainer_rewards = {uid: per_maintainer for uid in eligible_maintainers}
            else:
                allocation.recycled_amount += carve_out
            scoring_slice -= carve_out

        issue_share = repo_config.issue_discovery_share
        raw_pr_scores = _collect_repo_pr_scores(miner_evaluations, repo_name, miner_uids)
        raw_issue_scores = _collect_repo_issue_discovery_scores(miner_evaluations, repo_name, miner_uids)
        pr_scores = raw_pr_scores if issue_share < 1.0 else {}
        issue_scores = raw_issue_scores if issue_share > 0.0 else {}

        allocation.pr_scores = raw_pr_scores
        allocation.issue_discovery_scores = raw_issue_scores

        pr_total = sum(pr_scores.values())
        issue_total = sum(issue_scores.values())

        if pr_total <= 0 and issue_total <= 0:
            allocation.recycled_amount += scoring_slice
            yield allocation
            continue

        if pr_total > 0 and issue_total > 0:
            allocation.pr_slice = scoring_slice * (1.0 - issue_share)
            allocation.issue_discovery_slice = scoring_slice * issue_share
        elif pr_total > 0:
            allocation.pr_slice = scoring_slice
        else:
            allocation.issue_discovery_slice = scoring_slice

        allocation.pr_rewards, pr_unallocated = _calculate_score_rewards(
            pr_scores,
            allocation.pr_slice,
            miner_uids,
        )
        allocation.issue_discovery_rewards, issue_unallocated = _calculate_score_rewards(
            issue_scores,
            allocation.issue_discovery_slice,
            miner_uids,
        )
        allocation.recycled_amount += pr_unallocated + issue_unallocated
        yield allocation


def _calculate_score_rewards(
    scores: Dict[int, float],
    allocation: float,
    miner_uids: set[int],
) -> tuple[Dict[int, float], float]:
    if allocation <= 0:
        return {}, 0.0

    total = sum(scores.values())
    if total <= 0:
        return {}, allocation

    rewards: Dict[int, float] = {}
    unallocated = 0.0
    for uid, score in scores.items():
        share = allocation * (score / total)
        if uid in miner_uids:
            rewards[uid] = share
        else:
            unallocated += share

    return rewards, unallocated


def _collect_repo_pr_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
    repo_name: str,
    miner_uids: set[int],
) -> Dict[int, float]:
    scores: Dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        if not _is_scoring_evaluation(uid, evaluation, miner_uids):
            continue

        repo_eval = evaluation.repo_evaluations.get(repo_name)
        if repo_eval is None:
            continue

        score = repo_eval.total_score
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
        if not _is_scoring_evaluation(uid, evaluation, miner_uids):
            continue

        score = sum(
            issue.discovery_earned_score
            for issue in evaluation.issue_discovery_issues
            if issue.repository_full_name.lower() == repo_name and issue.discovery_earned_score > 0
        )
        if score > 0:
            scores[uid] = score

    return scores


def _is_scoring_evaluation(uid: int, evaluation: MinerEvaluation, miner_uids: set[int]) -> bool:
    return uid in miner_uids and evaluation.failed_reason is None
