from collections.abc import Mapping
from typing import Dict

import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import OSS_EMISSION_SHARE, RECYCLE_UID
from gittensor.validator.utils.load_weights import RepositoryConfig


def allocate_repo_scoring_pool(
    sorted_uids: list[int],
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Mapping[str, RepositoryConfig],
    pool_share: float = OSS_EMISSION_SHARE,
) -> np.ndarray:
    """Allocate the scoring pool by bounded repository emission shares.

    Each configured repo receives at most ``emission_share * pool_share``.
    Within that repo slice, PR and issue-discovery sub-slices split by
    ``issue_discovery_share`` and spill only to the active side of the same
    repo. Fully inactive repo slices and registry slack go to ``RECYCLE_UID``.
    """
    rewards = np.zeros(len(sorted_uids))
    uid_to_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    recycle_idx = uid_to_index.get(RECYCLE_UID)
    allocated_share = 0.0

    for repo_name, repo_config in master_repositories.items():
        repo_name = repo_name.lower()
        repo_share = float(repo_config.emission_share)
        allocated_share += repo_share
        repo_slice = pool_share * repo_share
        if repo_slice <= 0.0:
            continue

        pr_scores = _collect_pr_scores(repo_name, miner_evaluations)
        issue_scores = _collect_issue_scores(repo_name, miner_evaluations)
        pr_total = sum(pr_scores.values())
        issue_total = sum(issue_scores.values())

        if pr_total <= 0.0 and issue_total <= 0.0:
            _add_recycle(rewards, recycle_idx, repo_slice)
            continue

        issue_slice = repo_slice * float(repo_config.issue_discovery_share)
        pr_slice = repo_slice - issue_slice

        if pr_total <= 0.0:
            issue_slice += pr_slice
            pr_slice = 0.0
        elif issue_total <= 0.0:
            pr_slice += issue_slice
            issue_slice = 0.0

        _add_proportional_rewards(rewards, uid_to_index, pr_scores, pr_total, pr_slice)
        _add_proportional_rewards(rewards, uid_to_index, issue_scores, issue_total, issue_slice)

    slack_share = max(0.0, 1.0 - allocated_share)
    _add_recycle(rewards, recycle_idx, pool_share * slack_share)

    return rewards


def _collect_pr_scores(repo_name: str, miner_evaluations: Dict[int, MinerEvaluation]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        repo_score = sum(
            float(pr.earned_score)
            for pr in evaluation.merged_prs
            if pr.repository_full_name.lower() == repo_name and pr.earned_score > 0.0
        )
        if repo_score > 0.0:
            scores[uid] = repo_score
    return scores


def _collect_issue_scores(repo_name: str, miner_evaluations: Dict[int, MinerEvaluation]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        repo_score = sum(
            float(issue.discovery_earned_score)
            for issue in evaluation.discovered_issues
            if issue.repository_full_name.lower() == repo_name and issue.discovery_earned_score > 0.0
        )
        if repo_score > 0.0:
            scores[uid] = repo_score
    return scores


def _add_proportional_rewards(
    rewards: np.ndarray,
    uid_to_index: dict[int, int],
    scores: dict[int, float],
    total: float,
    amount: float,
) -> None:
    if total <= 0.0 or amount <= 0.0:
        return
    for uid, score in scores.items():
        idx = uid_to_index.get(uid)
        if idx is not None:
            rewards[idx] += amount * (score / total)


def _add_recycle(rewards: np.ndarray, recycle_idx: int | None, amount: float) -> None:
    if recycle_idx is not None and amount > 0.0:
        rewards[recycle_idx] += amount
