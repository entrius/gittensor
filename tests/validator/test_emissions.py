"""Regression tests for emission pool blending (percentages from ``gittensor.constants``)."""

import numpy as np

from gittensor.constants import (
    ISSUE_DISCOVERY_EMISSION_SHARE,
    ISSUES_TREASURY_EMISSION_SHARE,
    OSS_EMISSION_SHARE,
    RECYCLE_EMISSION_SHARE,
)
from gittensor.validator.emissions import blend_emission_pools


def test_blend_splits_normalized_pools():
    miner_uids = {0, 42, 111}
    oss = np.array([0.5, 0.3, 0.2])  # sums to 1, order 0, 42, 111
    issue = np.array([0.2, 0.3, 0.5])
    out = blend_emission_pools(oss, issue, miner_uids)
    expected = oss * OSS_EMISSION_SHARE + issue * ISSUE_DISCOVERY_EMISSION_SHARE
    expected[2] += ISSUES_TREASURY_EMISSION_SHARE  # treasury UID 111
    expected[0] += RECYCLE_EMISSION_SHARE
    np.testing.assert_allclose(out, expected, rtol=0, atol=1e-9)


def test_empty_oss_and_issue_pools_go_to_recycle():
    miner_uids = {0, 111}
    oss = np.array([0.0, 0.0])
    issue = np.array([0.0, 0.0])
    out = blend_emission_pools(oss, issue, miner_uids)
    # sorted uids: 0, 111 — treasury on 111, recycle gets base + both pools
    np.testing.assert_allclose(out[1], ISSUES_TREASURY_EMISSION_SHARE, rtol=0, atol=1e-9)
    np.testing.assert_allclose(
        out[0],
        RECYCLE_EMISSION_SHARE + OSS_EMISSION_SHARE + ISSUE_DISCOVERY_EMISSION_SHARE,
        rtol=0,
        atol=1e-9,
    )
