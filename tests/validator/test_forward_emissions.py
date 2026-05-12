import numpy as np
import pytest

from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.validator.forward import blend_emission_pools


def test_collapsed_emission_constants():
    assert OSS_EMISSION_SHARE == pytest.approx(0.90)
    assert ISSUES_TREASURY_EMISSION_SHARE == pytest.approx(0.10)


def test_combined_scoring_pool_receives_ninety_percent():
    miner_uids = {RECYCLE_UID, 1, 2, ISSUES_TREASURY_UID}
    oss_rewards = np.array([0.0, 1.0, 0.0, 0.0])
    issue_rewards = np.array([0.0, 0.0, 1.0, 0.0])

    rewards = blend_emission_pools(oss_rewards, issue_rewards, miner_uids)

    assert rewards.sum() == pytest.approx(1.0)
    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(0.45)
    assert rewards[2] == pytest.approx(0.45)
    assert rewards[3] == pytest.approx(0.10)


def test_empty_scoring_pool_recycles_ninety_percent_without_fixed_baseline():
    miner_uids = {RECYCLE_UID, 1, ISSUES_TREASURY_UID}
    empty_rewards = np.zeros(3)

    rewards = blend_emission_pools(empty_rewards, empty_rewards, miner_uids)

    assert rewards.sum() == pytest.approx(1.0)
    assert rewards[0] == pytest.approx(0.90)
    assert rewards[1] == pytest.approx(0.0)
    assert rewards[2] == pytest.approx(0.10)
