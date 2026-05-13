import pytest

from gittensor.constants import RECYCLE_UID
from gittensor.validator.emission_allocation import allocate_repo_emission_slices


def test_active_repo_with_one_scorer_pays_full_slice_to_that_scorer():
    rewards = allocate_repo_emission_slices(
        repo_scores={'repo/a': {1: 25.0}},
        repo_emission_shares={'repo/a': 0.05},
        miner_uids={RECYCLE_UID, 1},
    )

    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(0.05)


def test_active_repo_with_many_scorers_splits_same_slice_by_score():
    rewards = allocate_repo_emission_slices(
        repo_scores={'repo/a': {1: 10.0, 2: 30.0, 3: 60.0}},
        repo_emission_shares={'repo/a': 0.05},
        miner_uids={RECYCLE_UID, 1, 2, 3},
    )

    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(0.005)
    assert rewards[2] == pytest.approx(0.015)
    assert rewards[3] == pytest.approx(0.03)
    assert rewards.sum() == pytest.approx(0.05)


def test_repo_with_zero_positive_score_recycles_slice():
    rewards = allocate_repo_emission_slices(
        repo_scores={'repo/a': {1: 0.0, 2: -5.0}},
        repo_emission_shares={'repo/a': 0.05},
        miner_uids={RECYCLE_UID, 1, 2},
    )

    assert rewards[0] == pytest.approx(0.05)
    assert rewards[1] == pytest.approx(0.0)
    assert rewards[2] == pytest.approx(0.0)


def test_high_throughput_repo_cannot_exceed_its_slice():
    rewards = allocate_repo_emission_slices(
        repo_scores={
            'repo/a': {1: 1000.0, 2: 1000.0},
            'repo/b': {3: 1.0},
        },
        repo_emission_shares={'repo/a': 0.05, 'repo/b': 0.10},
        miner_uids={RECYCLE_UID, 1, 2, 3},
    )

    assert rewards[1] + rewards[2] == pytest.approx(0.05)
    assert rewards[3] == pytest.approx(0.10)
