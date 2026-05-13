import numpy as np
import pytest

from gittensor.constants import RECYCLE_UID
from gittensor.validator.emission_allocation import recycle_unallocated_emission


def test_unconfigured_registry_slack_goes_to_recycle_uid():
    rewards = np.array([0.0, 0.25, 0.25])

    updated = recycle_unallocated_emission(
        rewards,
        miner_uids={RECYCLE_UID, 1, 2},
        configured_share=0.50,
    )

    assert updated[0] == pytest.approx(0.50)
    assert updated[1] == pytest.approx(0.25)
    assert updated[2] == pytest.approx(0.25)
    assert updated.sum() == pytest.approx(1.0)


def test_no_slack_is_added_when_registry_is_fully_allocated():
    rewards = np.array([0.0, 0.5, 0.5])

    updated = recycle_unallocated_emission(
        rewards,
        miner_uids={RECYCLE_UID, 1, 2},
        configured_share=1.0,
    )

    assert updated.tolist() == pytest.approx(rewards.tolist())


def test_missing_recycle_uid_leaves_rewards_unchanged():
    rewards = np.array([0.25, 0.25])

    updated = recycle_unallocated_emission(
        rewards,
        miner_uids={1, 2},
        configured_share=0.50,
    )

    assert updated.tolist() == pytest.approx(rewards.tolist())
