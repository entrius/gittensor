import numpy as np
import pytest

from gittensor.constants import ISSUE_DISCOVERY_EMISSION_SHARE, OSS_EMISSION_SHARE, RECYCLE_EMISSION_SHARE, RECYCLE_UID
from gittensor.validator.forward import blend_emission_pools


def test_blend_recycles_unavailable_issue_discovery_share():
    miner_uids = {RECYCLE_UID, 1}
    oss_rewards = np.array([0.0, 0.0])
    issue_rewards = np.array([0.0, 0.5])

    rewards = blend_emission_pools(oss_rewards, issue_rewards, miner_uids)

    unavailable_issue_share = ISSUE_DISCOVERY_EMISSION_SHARE * 0.5
    assert rewards[0] == pytest.approx(RECYCLE_EMISSION_SHARE + OSS_EMISSION_SHARE + unavailable_issue_share)
    assert rewards[1] == pytest.approx(ISSUE_DISCOVERY_EMISSION_SHARE * 0.5)
