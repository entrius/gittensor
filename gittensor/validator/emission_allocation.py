from collections.abc import Mapping

import numpy as np

from gittensor.constants import RECYCLE_UID


def allocate_repo_emissions_for_tests(
    pr_scores: Mapping[str, Mapping[int, float]],
    issue_scores: Mapping[str, Mapping[int, float]],
    repo_emission_shares: Mapping[str, float],
    repo_issue_shares: Mapping[str, float],
    miner_uids: set[int],
    pool_share: float = 1.0,
) -> np.ndarray:
    """Reference allocation used to pin #1215 emission behavior in tests.

    The helper models the required invariants: each repo has a fixed emission
    slice, PR/issue sub-slices spill within the repo when one side is empty,
    fully empty repos recycle, and unconfigured registry slack recycles.
    """
    sorted_uids = sorted(miner_uids)
    uid_to_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids))
    configured_share = 0.0

    for repo, emission_share in repo_emission_shares.items():
        repo_slice = float(emission_share) * pool_share
        configured_share += repo_slice
        issue_slice = repo_slice * float(repo_issue_shares.get(repo, 0.5))
        pr_slice = repo_slice - issue_slice

        pr_total = _pay_subpool(rewards, uid_to_index, pr_scores.get(repo, {}), pr_slice)
        issue_total = _pay_subpool(rewards, uid_to_index, issue_scores.get(repo, {}), issue_slice)

        if pr_total == 0.0 and issue_total > 0.0:
            _pay_subpool(rewards, uid_to_index, issue_scores.get(repo, {}), pr_slice)
        elif issue_total == 0.0 and pr_total > 0.0:
            _pay_subpool(rewards, uid_to_index, pr_scores.get(repo, {}), issue_slice)
        elif pr_total == 0.0 and issue_total == 0.0:
            _pay_recycle(rewards, uid_to_index, repo_slice)

    _pay_recycle(rewards, uid_to_index, max(pool_share - configured_share, 0.0))
    return rewards


def _pay_subpool(
    rewards: np.ndarray,
    uid_to_index: dict[int, int],
    scores: Mapping[int, float],
    amount: float,
) -> float:
    positive_scores = {uid: float(score) for uid, score in scores.items() if uid in uid_to_index and float(score) > 0}
    total = sum(positive_scores.values())
    if total == 0.0:
        return 0.0

    for uid, score in positive_scores.items():
        rewards[uid_to_index[uid]] += amount * (score / total)
    return total


def _pay_recycle(rewards: np.ndarray, uid_to_index: dict[int, int], amount: float) -> None:
    if amount > 0.0 and RECYCLE_UID in uid_to_index:
        rewards[uid_to_index[RECYCLE_UID]] += amount
