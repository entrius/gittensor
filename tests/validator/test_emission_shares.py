# Entrius 2025

"""
Guard-rail test: emission shares must never exceed 100% cumulatively.

If ISSUES_TREASURY_EMISSION_SHARE + PREDICTIONS_EMISSIONS_SHARE >= 1.0,
OSS contributions would receive zero or negative share, breaking the reward system.

Run:
    pytest tests/validator/test_emission_shares.py -v
"""

from gittensor.constants import ISSUES_TREASURY_EMISSION_SHARE, PREDICTIONS_EMISSIONS_SHARE


def test_combined_emission_shares_leave_room_for_oss():
    """Issue bounties + merge predictions must not consume all emissions."""
    combined = ISSUES_TREASURY_EMISSION_SHARE + PREDICTIONS_EMISSIONS_SHARE
    oss_share = 1.0 - combined

    assert combined < 1.0, (
        f'Combined non-OSS emission shares ({ISSUES_TREASURY_EMISSION_SHARE} + {PREDICTIONS_EMISSIONS_SHARE} '
        f'= {combined}) must be < 1.0, otherwise OSS contributions get nothing'
    )
    assert oss_share > 0.0
