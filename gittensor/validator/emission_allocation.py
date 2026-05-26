# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Round-level emission allocation by repository emission shares."""

from typing import Dict, Optional

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
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

    for repo_name, repo_config in master_repositories.items():
        repo_slice = repo_config.emission_share * OSS_EMISSION_SHARE
        if repo_slice <= 0:
            continue

        # Maintainer carve-out: route maintainer_cut of the repo slice evenly to
        # the repo's registered maintainer miners, off the top before the
        # PR/issue split. Skipped when no maintainer miner is present.
        maintainer_uids = (maintainer_uids_by_repo or {}).get(repo_name) or []
        if repo_config.maintainer_cut > 0.0 and maintainer_uids:
            carve_out = repo_config.maintainer_cut * repo_slice
            per_maintainer = carve_out / len(maintainer_uids)
            for uid in maintainer_uids:
                idx = uid_index.get(uid)
                if idx is not None:
                    rewards[idx] += per_maintainer
            repo_slice -= carve_out

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
