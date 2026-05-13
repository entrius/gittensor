from __future__ import annotations

from typing import Dict, Mapping, Set

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.validator.utils.load_weights import RepositoryConfig


def allocate_emissions(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Mapping[str, RepositoryConfig],
    miner_uids: Set[int],
) -> np.ndarray:
    """Allocate rewards through bounded per-repository emission slices.

    PR and issue-discovery scores stay raw until this point. Each configured
    repository owns an ``emission_share`` of the OSS pool, split by
    ``issue_discovery_share``. If only one side is active for a repository, that
    side receives the full repo slice. If neither side is active, the slice is
    recycled.
    """
    sorted_uids = sorted(miner_uids)
    uid_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids))

    pr_scores = _collect_pr_scores_by_repo(miner_evaluations)
    issue_scores = _collect_issue_scores_by_repo(miner_evaluations)

    recycle_share = 0.0
    for repo_name, config in master_repositories.items():
        repo_key = repo_name.lower()
        repo_share = config.emission_share
        issue_share = config.issue_discovery_share
        pr_slice = repo_share * (1.0 - issue_share)
        issue_slice = repo_share * issue_share

        repo_pr_scores = pr_scores.get(repo_key, {})
        repo_issue_scores = issue_scores.get(repo_key, {})
        has_pr_scores = any(score > 0.0 for score in repo_pr_scores.values())
        has_issue_scores = any(score > 0.0 for score in repo_issue_scores.values())

        if not has_pr_scores and not has_issue_scores:
            recycle_share += repo_share
            continue
        if not has_pr_scores:
            issue_slice += pr_slice
            pr_slice = 0.0
        if not has_issue_scores:
            pr_slice += issue_slice
            issue_slice = 0.0

        _add_proportional_slice(rewards, uid_index, repo_pr_scores, pr_slice * OSS_EMISSION_SHARE)
        _add_proportional_slice(rewards, uid_index, repo_issue_scores, issue_slice * OSS_EMISSION_SHARE)

    configured_share = sum(config.emission_share for config in master_repositories.values())
    registry_slack = max(0.0, 1.0 - configured_share)
    recycle_total = (recycle_share + registry_slack) * OSS_EMISSION_SHARE

    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in uid_index:
        rewards[uid_index[ISSUES_TREASURY_UID]] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    if RECYCLE_UID in uid_index:
        rewards[uid_index[RECYCLE_UID]] += recycle_total
        if recycle_total > 0:
            bt.logging.info(f'Recycling {recycle_total * 100:.2f}% unallocated OSS emissions')

    return rewards


def _collect_pr_scores_by_repo(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[str, Dict[int, float]]:
    by_repo: Dict[str, Dict[int, float]] = {}
    for uid, evaluation in miner_evaluations.items():
        if evaluation.failed_reason is not None:
            continue
        score_by_repo = by_repo
        for pr in evaluation.merged_prs:
            if pr.earned_score <= 0.0:
                continue
            repo = pr.repository_full_name.lower()
            repo_scores = score_by_repo.setdefault(repo, {})
            repo_scores[uid] = repo_scores.get(uid, 0.0) + pr.earned_score
    return by_repo


def _collect_issue_scores_by_repo(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[str, Dict[int, float]]:
    by_repo: Dict[str, Dict[int, float]] = {}
    for uid, evaluation in miner_evaluations.items():
        if evaluation.failed_reason is not None:
            continue
        for repo, score in evaluation.issue_discovery_score_by_repo.items():
            if score <= 0.0:
                continue
            repo_key = repo.lower()
            repo_scores = by_repo.setdefault(repo_key, {})
            repo_scores[uid] = repo_scores.get(uid, 0.0) + score
    return by_repo


def _add_proportional_slice(
    rewards: np.ndarray,
    uid_index: Dict[int, int],
    scores: Dict[int, float],
    emission_slice: float,
) -> None:
    if emission_slice <= 0.0:
        return
    total = sum(score for score in scores.values() if score > 0.0)
    if total <= 0.0:
        return
    for uid, score in scores.items():
        if score > 0.0 and uid in uid_index:
            rewards[uid_index[uid]] += emission_slice * (score / total)
