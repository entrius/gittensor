from collections.abc import Mapping
from typing import Dict

import numpy as np

from gittensor.constants import RECYCLE_UID


def allocate_repo_emission_slices(
    repo_scores: Mapping[str, Mapping[int, float]],
    repo_emission_shares: Mapping[str, float],
    miner_uids: set[int],
    pool_share: float = 1.0,
) -> np.ndarray:
    """Allocate each repo's fixed emission slice across that repo's scorers.

    A repo with positive eligible score pays exactly
    ``repo_emission_share * pool_share`` across its scorers, proportionally by
    score. A repo with no positive score sends its slice to the recycle UID.
    Registry-level slack is intentionally handled by the caller.
    """
    sorted_uids = sorted(miner_uids)
    uid_to_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids))

    for repo_name, emission_share in repo_emission_shares.items():
        repo_slice = float(emission_share) * pool_share
        scores = repo_scores.get(repo_name, {})
        positive_scores: Dict[int, float] = {
            uid: float(score) for uid, score in scores.items() if uid in uid_to_index and float(score) > 0.0
        }
        score_total = sum(positive_scores.values())

        if score_total > 0.0:
            for uid, score in positive_scores.items():
                rewards[uid_to_index[uid]] += repo_slice * (score / score_total)
        elif RECYCLE_UID in uid_to_index:
            rewards[uid_to_index[RECYCLE_UID]] += repo_slice

    return rewards
