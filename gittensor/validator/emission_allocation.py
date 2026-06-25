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

    for allocation in calculate_repo_emission_breakdown(
        miner_evaluations, master_repositories, miner_uids, maintainer_uids_by_repo
    ):
        recycle_share += allocation.recycled_amount
        for uid, reward in allocation.maintainer_rewards.items():
            rewards[uid_index[uid]] += reward
        for uid, reward in allocation.pr_rewards.items():
            rewards[uid_index[uid]] += reward
        for uid, reward in allocation.issue_discovery_rewards.items():
            rewards[uid_index[uid]] += reward

    # Issue treasury (10% flat to UID 111)
    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = uid_index[ISSUES_TREASURY_UID]
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    # Recycle receives registry slack and empty repo slices.
    if RECYCLE_UID in miner_uids:
        recycle_idx = uid_index[RECYCLE_UID]
        rewards[recycle_idx] += recycle_share
        if recycle_share > EMISSION_SHARE_TOLERANCE:
            bt.logging.info(f'Recycling {recycle_share * 100:.0f}% unclaimed emissions from repo allocation')

    return rewards


def calculate_repo_emission_breakdown(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
    maintainer_uids_by_repo: Optional[Dict[str, list[int]]] = None,
) -> Iterator[RepoEmissionAllocation]:
    """Return per-repository reward allocation details without adding treasury/slack.

    Two independent piles: the maintainer cut is paid at the repo's *base* rate
    (``maintainer_cut * emission_share * OSS``) and is never scaled, so a maintainer's
    take cannot be inflated by other repos going dead. Everything else forms a
    subnet-wide scoring pool released only to repos with PR/issue scorers this round,
    weighted by their post-cut scoring share. Ineligible/empty scoring shares thus flow
    to active scorers instead of recycling; with no active repos the scoring pool
    recycles. Registry slack (configured shares < 1.0) recycles either way.
    """
    maintainer_map = maintainer_uids_by_repo or {}

    # Pass 1: classify each repo and size the scoring pool vs the active scoring share.
    plans: list[tuple[str, RepositoryConfig, list[int], float, bool]] = []
    total_scoring_share = 0.0
    active_scoring_share = 0.0
    for repo_name, repo_config in master_repositories.items():
        if repo_config.emission_share <= 0:
            continue
        eligible_maintainers = (
            [uid for uid in (maintainer_map.get(repo_name) or []) if uid in miner_uids]
            if repo_config.maintainer_cut > 0.0
            else []
        )
        cut_fraction = repo_config.maintainer_cut if eligible_maintainers else 0.0
        scoring_share = repo_config.emission_share * (1.0 - cut_fraction)
        is_active = _repo_has_scorers(miner_evaluations, repo_name, repo_config, miner_uids)

        total_scoring_share += scoring_share
        if is_active:
            active_scoring_share += scoring_share
        plans.append((repo_name, repo_config, eligible_maintainers, scoring_share, is_active))

    # The scoring pool is released to active repos pro-rata by scoring share; with none
    # active the pool recycles (multiplier 0 routes each repo's scoring share to recycle).
    scoring_multiplier = total_scoring_share / active_scoring_share if active_scoring_share > 0 else 0.0

    # Pass 2: emit per-repo allocations.
    for repo_name, repo_config, eligible_maintainers, scoring_share, is_active in plans:
        allocation = RepoEmissionAllocation(
            repository_full_name=repo_name,
            emission_share=repo_config.emission_share,
            issue_discovery_share=repo_config.issue_discovery_share,
            repo_slice=repo_config.emission_share * OSS_EMISSION_SHARE,
            maintainer_cut=repo_config.maintainer_cut,
        )

        # Maintainer pile: base-rate carve-out split evenly among registered maintainers.
        if eligible_maintainers:
            carve_out = repo_config.maintainer_cut * repo_config.emission_share * OSS_EMISSION_SHARE
            per_maintainer = carve_out / len(eligible_maintainers)
            allocation.maintainer_carve_out = carve_out
            allocation.maintainer_rewards = {uid: per_maintainer for uid in eligible_maintainers}

        allocation.pr_scores = _collect_repo_pr_scores(miner_evaluations, repo_name, miner_uids)
        allocation.issue_discovery_scores = _collect_repo_issue_discovery_scores(
            miner_evaluations, repo_name, miner_uids
        )

        if not is_active:
            # Inactive repo's scoring share is redistributed to active repos; it only
            # recycles when nothing is active anywhere.
            if active_scoring_share <= 0:
                allocation.recycled_amount += scoring_share * OSS_EMISSION_SHARE
            yield allocation
            continue

        scoring_slice = scoring_share * OSS_EMISSION_SHARE * scoring_multiplier
        issue_share = repo_config.issue_discovery_share
        pr_scores = allocation.pr_scores if issue_share < 1.0 else {}
        issue_scores = allocation.issue_discovery_scores if issue_share > 0.0 else {}
        pr_total = sum(pr_scores.values())
        issue_total = sum(issue_scores.values())

        if pr_total > 0 and issue_total > 0:
            allocation.pr_slice = scoring_slice * (1.0 - issue_share)
            allocation.issue_discovery_slice = scoring_slice * issue_share
        elif pr_total > 0:
            allocation.pr_slice = scoring_slice
        else:
            allocation.issue_discovery_slice = scoring_slice

        allocation.pr_rewards, pr_unallocated = _calculate_score_rewards(pr_scores, allocation.pr_slice, miner_uids)
        allocation.issue_discovery_rewards, issue_unallocated = _calculate_score_rewards(
            issue_scores, allocation.issue_discovery_slice, miner_uids
        )
        allocation.recycled_amount += pr_unallocated + issue_unallocated
        yield allocation


def _repo_has_scorers(
    miner_evaluations: Dict[int, MinerEvaluation],
    repo_name: str,
    repo_config: RepositoryConfig,
    miner_uids: set[int],
) -> bool:
    """True when the repo has a scorer on a side that pays out (mirrors the split gates).

    Maintainer presence alone does NOT make a repo active: a maintainer-only repo pays
    its base-rate cut but its scoring share is redistributed to repos that did work.
    """
    issue_share = repo_config.issue_discovery_share
    if issue_share < 1.0 and _collect_repo_pr_scores(miner_evaluations, repo_name, miner_uids):
        return True
    if issue_share > 0.0 and _collect_repo_issue_discovery_scores(miner_evaluations, repo_name, miner_uids):
        return True
    return False


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
