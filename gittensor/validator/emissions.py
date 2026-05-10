# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Blend independent scoring pools into chain weights (shares from ``gittensor.constants``)."""

import bittensor as bt
import numpy as np

from gittensor.constants import (
    ISSUE_DISCOVERY_EMISSION_SHARE,
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_EMISSION_SHARE,
    RECYCLE_UID,
)


def blend_emission_pools(
    oss_rewards: np.ndarray,
    issue_rewards: np.ndarray,
    miner_uids: set[int],
) -> np.ndarray:
    """Blend emission pools into a single rewards vector (same UID order as ``sorted(miner_uids)``).

    - OSS contributions: ``OSS_EMISSION_SHARE`` of normalized OSS pool (or to recycle if empty)
    - Issue discovery: ``ISSUE_DISCOVERY_EMISSION_SHARE`` (or to recycle if empty)
    - Issue treasury: ``ISSUES_TREASURY_EMISSION_SHARE`` flat to ``ISSUES_TREASURY_UID`` when present
    - Recycle: ``RECYCLE_EMISSION_SHARE`` plus any share moved from empty OSS/issue pools
    """
    sorted_uids = sorted(miner_uids)
    rewards = np.zeros(len(sorted_uids))
    recycle_extra = 0.0

    oss_total = float(oss_rewards.sum())
    if oss_total > 0:
        rewards += oss_rewards * OSS_EMISSION_SHARE
    else:
        recycle_extra += OSS_EMISSION_SHARE

    issue_total = float(issue_rewards.sum())
    if issue_total > 0:
        rewards += issue_rewards * ISSUE_DISCOVERY_EMISSION_SHARE
    else:
        recycle_extra += ISSUE_DISCOVERY_EMISSION_SHARE

    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    if RECYCLE_UID in miner_uids:
        recycle_idx = sorted_uids.index(RECYCLE_UID)
        rewards[recycle_idx] += RECYCLE_EMISSION_SHARE + recycle_extra
        if recycle_extra > 0:
            bt.logging.info(f'Recycling {recycle_extra * 100:.0f}% unclaimed emissions from empty pools')

    return rewards
