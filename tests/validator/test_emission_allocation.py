"""Unit tests for per-repo emission_share allocation (``allocate_round_emissions``)."""

from datetime import datetime, timezone

import pytest

from gittensor.classes import MinerEvaluation, PRState, PullRequest
from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.validator.emission_allocation import allocate_round_emissions
from gittensor.validator.utils.load_weights import RepositoryConfig


def _pr(repo: str, uid: int, earned: float) -> PullRequest:
    now = datetime.now(timezone.utc)
    return PullRequest(
        number=1,
        repository_full_name=repo,
        uid=uid,
        hotkey=f'hk{uid}',
        github_id=str(uid),
        title='t',
        author_login='a',
        merged_at=now,
        created_at=now,
        pr_state=PRState.MERGED,
        earned_score=earned,
        token_score=10.0,
    )


def _uids() -> set[int]:
    return {1, RECYCLE_UID, ISSUES_TREASURY_UID}


def test_full_activity_registry_sum_one_zero_recycle():
    """Active repo with one PR: miner receives the full OSS pool share; recycle gets 0 from slack."""
    ev = MinerEvaluation(uid=1, hotkey='hk', github_id='9')
    ev.is_eligible = True
    ev.merged_pull_requests = [_pr('acme/repo', 1, 100.0)]

    repos = {'acme/repo': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.0)}
    rewards = allocate_round_emissions({1: ev}, repos, _uids())
    idx = {u: i for i, u in enumerate(sorted(_uids()))}
    assert rewards.sum() == pytest.approx(1.0)
    assert rewards[idx[ISSUES_TREASURY_UID]] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(0.0, abs=1e-9)
    assert rewards[idx[1]] == pytest.approx(OSS_EMISSION_SHARE)


def test_no_pr_activity_recycles_repo_slice():
    """Repo slice with zero PR weight and zero issue weight goes to recycle."""
    ev = MinerEvaluation(uid=1, hotkey='hk', github_id='9')
    ev.is_eligible = True
    ev.merged_pull_requests = []

    repos = {'acme/repo': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    rewards = allocate_round_emissions({1: ev}, repos, _uids())
    idx = {u: i for i, u in enumerate(sorted(_uids()))}
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[idx[1]] == pytest.approx(0.0, abs=1e-9)


def test_registry_slack_routes_to_recycle():
    """When Σ emission_share < 1, (1 - sum) × OSS pool goes to recycle alongside any unclaimed repo slice."""
    ev = MinerEvaluation(uid=1, hotkey='hk', github_id='9')
    ev.is_eligible = True
    ev.merged_pull_requests = [_pr('acme/a', 1, 10.0)]
    ev.is_issue_eligible = True
    ev.issue_discovery_repo_scores = {'acme/a': 5.0}

    repos = {
        'acme/a': RepositoryConfig(emission_share=0.8, issue_discovery_share=0.5),
    }
    rewards = allocate_round_emissions({1: ev}, repos, _uids())
    idx = {u: i for i, u in enumerate(sorted(_uids()))}
    # Treasury flat
    assert rewards[idx[ISSUES_TREASURY_UID]] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)
    # Registry slack: 0.2 * OSS_EMISSION_SHARE
    slack = 0.2 * OSS_EMISSION_SHARE
    # Repo slice 0.8 * OSS split half/half by weight 10 vs 5 → PR gets 2/3 of 0.8*OSS, issue 1/3
    repo_total = 0.8 * OSS_EMISSION_SHARE
    pr_part = repo_total * (10.0 / 15.0)
    iss_part = repo_total * (5.0 / 15.0)
    assert rewards[idx[1]] == pytest.approx(pr_part + iss_part)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(slack, abs=1e-6)
    assert rewards.sum() == pytest.approx(1.0, abs=1e-5)


def test_pr_count_does_not_change_repo_slice_total():
    """Same repo slice is split across PRs by relative earned_score (two PRs vs one)."""
    repos = {'acme/r': RepositoryConfig(emission_share=0.5, issue_discovery_share=0.0)}

    ev1 = MinerEvaluation(uid=1, hotkey='a', github_id='1')
    ev1.is_eligible = True
    ev1.merged_pull_requests = [_pr('acme/r', 1, 10.0)]

    ev2 = MinerEvaluation(uid=2, hotkey='b', github_id='2')
    ev2.is_eligible = True
    ev2.merged_pull_requests = [
        _pr('acme/r', 2, 10.0),
        _pr('acme/r', 2, 10.0),
    ]

    slice_amt = 0.5 * OSS_EMISSION_SHARE
    uids = {1, 2, RECYCLE_UID, ISSUES_TREASURY_UID}
    idx = {u: i for i, u in enumerate(sorted(uids))}

    r1 = allocate_round_emissions({1: ev1}, repos, uids)
    r2 = allocate_round_emissions({2: ev2}, repos, uids)
    assert r1[idx[1]] == pytest.approx(slice_amt)
    assert r2[idx[2]] == pytest.approx(slice_amt)


def test_issue_discovery_share_spills_pr_subslice_to_issues():
    """PR nominal sub-slice with no PR weight but positive issue weight → full repo slice to issues."""
    ev = MinerEvaluation(uid=1, hotkey='hk', github_id='9')
    ev.is_eligible = True
    ev.merged_pull_requests = []
    ev.is_issue_eligible = True
    ev.issue_discovery_repo_scores = {'acme/r': 3.0}

    repos = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.3)}
    rewards = allocate_round_emissions({1: ev}, repos, _uids())
    idx = {u: i for i, u in enumerate(sorted(_uids()))}
    assert rewards[idx[1]] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(0.0, abs=1e-9)
