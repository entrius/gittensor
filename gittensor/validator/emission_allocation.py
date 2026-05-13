import numpy as np

from gittensor.constants import RECYCLE_UID


def recycle_unallocated_emission(
    rewards: np.ndarray,
    miner_uids: set[int],
    configured_share: float,
    pool_share: float = 1.0,
) -> np.ndarray:
    """Return rewards with unallocated registry slack paid to recycle UID.

    ``configured_share`` is the sum of configured repo emission shares. Any
    positive ``pool_share - configured_share`` is unallocated by the registry and
    therefore belongs to the recycle UID.
    """
    slack = max(float(pool_share) - float(configured_share), 0.0)
    if slack == 0.0 or RECYCLE_UID not in miner_uids:
        return rewards

    updated = rewards.copy()
    updated[sorted(miner_uids).index(RECYCLE_UID)] += slack
    return updated
