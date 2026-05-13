from collections.abc import Mapping

import numpy as np

from gittensor.constants import RECYCLE_UID


def allocate_split_repo_emission_slices(
    pr_scores: Mapping[str, Mapping[int, float]],
    issue_scores: Mapping[str, Mapping[int, float]],
    repo_emission_shares: Mapping[str, float],
    repo_issue_discovery_shares: Mapping[str, float],
    miner_uids: set[int],
    pool_share: float = 1.0,
) -> np.ndarray:
    """Allocate repo slices across PR and issue-discovery scorers.

    Each repo gets a fixed slice of the scoring pool. ``issue_discovery_share``
    splits that slice between issue discovery and PR scoring. If one side has no
    positive eligible scorer, its slice spills to the other side in the same
    repo. If neither side has positive score, the full repo slice recycles.
    """
    sorted_uids = sorted(miner_uids)
    uid_to_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids))

    for repo_name, emission_share in repo_emission_shares.items():
        repo_slice = float(emission_share) * pool_share
        issue_share = float(repo_issue_discovery_shares.get(repo_name, 0.5))
        issue_slice = repo_slice * issue_share
        pr_slice = repo_slice - issue_slice

        pr_total = _allocate_subpool(rewards, uid_to_index, pr_scores.get(repo_name, {}), pr_slice)
        issue_total = _allocate_subpool(rewards, uid_to_index, issue_scores.get(repo_name, {}), issue_slice)

        if pr_total == 0.0 and issue_total > 0.0:
            _allocate_subpool(rewards, uid_to_index, issue_scores.get(repo_name, {}), pr_slice)
        elif issue_total == 0.0 and pr_total > 0.0:
            _allocate_subpool(rewards, uid_to_index, pr_scores.get(repo_name, {}), issue_slice)
        elif pr_total == 0.0 and issue_total == 0.0 and RECYCLE_UID in uid_to_index:
            rewards[uid_to_index[RECYCLE_UID]] += repo_slice

    return rewards


def _allocate_subpool(
    rewards: np.ndarray,
    uid_to_index: dict[int, int],
    scores: Mapping[int, float],
    subpool: float,
) -> float:
    positive_scores = {uid: float(score) for uid, score in scores.items() if uid in uid_to_index and float(score) > 0.0}
    score_total = sum(positive_scores.values())
    if score_total == 0.0 or subpool == 0.0:
        return score_total

    for uid, score in positive_scores.items():
        rewards[uid_to_index[uid]] += subpool * (score / score_total)

    return score_total
