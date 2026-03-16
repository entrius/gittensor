# Entrius 2025

"""
Guard-rail tests: emission shares and top-K constant configuration.

Ensures:
- Combined non-OSS emission shares (treasury + predictions) never reach 100%.
- PREDICTIONS_TOP_K_SHARES sums to exactly 1.0 and has length == PREDICTIONS_TOP_K.

Run:
    pytest tests/validator/test_emission_shares.py -v
"""

import pytest

from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    PREDICTIONS_EMISSIONS_SHARE,
    PREDICTIONS_TOP_K,
    PREDICTIONS_TOP_K_SHARES,
)


def test_combined_emission_shares_leave_room_for_oss():
    """Issue bounties + merge predictions must not consume all emissions."""
    combined = ISSUES_TREASURY_EMISSION_SHARE + PREDICTIONS_EMISSIONS_SHARE
    oss_share = 1.0 - combined

    assert combined < 1.0, (
        f'Combined non-OSS emission shares ({ISSUES_TREASURY_EMISSION_SHARE} + {PREDICTIONS_EMISSIONS_SHARE} '
        f'= {combined}) must be < 1.0, otherwise OSS contributions get nothing'
    )
    assert oss_share > 0.0


def test_top_k_shares_sum_to_one():
    """Top-K shares must sum to exactly 1.0."""
    assert sum(PREDICTIONS_TOP_K_SHARES) == pytest.approx(1.0), (
        f'PREDICTIONS_TOP_K_SHARES must sum to 1.0, got {sum(PREDICTIONS_TOP_K_SHARES)}'
    )


def test_top_k_shares_length_matches_top_k():
    """PREDICTIONS_TOP_K_SHARES length must equal PREDICTIONS_TOP_K."""
    assert len(PREDICTIONS_TOP_K_SHARES) == PREDICTIONS_TOP_K, (
        f'PREDICTIONS_TOP_K_SHARES has {len(PREDICTIONS_TOP_K_SHARES)} entries '
        f'but PREDICTIONS_TOP_K is {PREDICTIONS_TOP_K}'
    )
