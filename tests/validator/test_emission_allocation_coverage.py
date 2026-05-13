import pytest

from gittensor.constants import RECYCLE_UID
from gittensor.validator.emission_allocation import allocate_repo_emissions_for_tests


def test_repo_allocation_recycle_and_spill_cases_in_one_round():
    rewards = allocate_repo_emissions_for_tests(
        pr_scores={
            'repo/pr-and-issue': {1: 1.0},
            'repo/pr-only': {3: 1.0},
            'repo/empty': {},
        },
        issue_scores={
            'repo/pr-and-issue': {2: 1.0},
            'repo/pr-only': {},
            'repo/empty': {},
        },
        repo_emission_shares={
            'repo/pr-and-issue': 0.20,
            'repo/pr-only': 0.30,
            'repo/empty': 0.10,
        },
        repo_issue_shares={
            'repo/pr-and-issue': 0.25,
            'repo/pr-only': 0.50,
            'repo/empty': 0.50,
        },
        miner_uids={RECYCLE_UID, 1, 2, 3},
    )

    assert rewards[0] == pytest.approx(0.50)  # 0.10 empty repo + 0.40 registry slack
    assert rewards[1] == pytest.approx(0.15)  # PR side of repo/pr-and-issue
    assert rewards[2] == pytest.approx(0.05)  # Issue side of repo/pr-and-issue
    assert rewards[3] == pytest.approx(0.30)  # issue side spills to PR side
    assert rewards.sum() == pytest.approx(1.0)


def test_high_volume_repo_still_capped_at_configured_slice():
    rewards = allocate_repo_emissions_for_tests(
        pr_scores={'repo/high-volume': {1: 1000.0, 2: 1000.0}},
        issue_scores={'repo/high-volume': {}},
        repo_emission_shares={'repo/high-volume': 0.20},
        repo_issue_shares={'repo/high-volume': 0.50},
        miner_uids={RECYCLE_UID, 1, 2},
        pool_share=0.90,
    )

    assert rewards[1] + rewards[2] == pytest.approx(0.18)
    assert rewards[0] == pytest.approx(0.72)
    assert rewards.sum() == pytest.approx(0.90)
