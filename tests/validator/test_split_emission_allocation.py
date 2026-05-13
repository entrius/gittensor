import pytest

from gittensor.constants import RECYCLE_UID
from gittensor.validator.emission_allocation import allocate_split_repo_emission_slices


def test_repo_slice_splits_between_pr_and_issue_scorers():
    rewards = allocate_split_repo_emission_slices(
        pr_scores={'repo/a': {1: 1.0}},
        issue_scores={'repo/a': {2: 1.0}},
        repo_emission_shares={'repo/a': 0.10},
        repo_issue_discovery_shares={'repo/a': 0.30},
        miner_uids={RECYCLE_UID, 1, 2},
    )

    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(0.07)
    assert rewards[2] == pytest.approx(0.03)


def test_empty_issue_side_spills_to_pr_side_in_same_repo():
    rewards = allocate_split_repo_emission_slices(
        pr_scores={'repo/a': {1: 1.0, 2: 3.0}},
        issue_scores={'repo/a': {}},
        repo_emission_shares={'repo/a': 0.10},
        repo_issue_discovery_shares={'repo/a': 0.30},
        miner_uids={RECYCLE_UID, 1, 2},
    )

    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(0.025)
    assert rewards[2] == pytest.approx(0.075)
    assert rewards.sum() == pytest.approx(0.10)


def test_empty_pr_side_spills_to_issue_side_in_same_repo():
    rewards = allocate_split_repo_emission_slices(
        pr_scores={'repo/a': {}},
        issue_scores={'repo/a': {1: 2.0, 2: 2.0}},
        repo_emission_shares={'repo/a': 0.10},
        repo_issue_discovery_shares={'repo/a': 0.30},
        miner_uids={RECYCLE_UID, 1, 2},
    )

    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(0.05)
    assert rewards[2] == pytest.approx(0.05)
    assert rewards.sum() == pytest.approx(0.10)


def test_empty_repo_recycles_full_repo_slice():
    rewards = allocate_split_repo_emission_slices(
        pr_scores={'repo/a': {1: 0.0}},
        issue_scores={'repo/a': {2: 0.0}},
        repo_emission_shares={'repo/a': 0.10},
        repo_issue_discovery_shares={'repo/a': 0.30},
        miner_uids={RECYCLE_UID, 1, 2},
    )

    assert rewards[0] == pytest.approx(0.10)
    assert rewards[1] == pytest.approx(0.0)
    assert rewards[2] == pytest.approx(0.0)
