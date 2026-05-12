from types import SimpleNamespace

import pytest

from gittensor.constants import ISSUES_TREASURY_UID, RECYCLE_UID
from gittensor.validator.forward import blend_emission_pools
from gittensor.validator.utils.load_weights import RepositoryConfig


def _eval(uid: int, pr_scores_by_repo: dict[str, list[float]] | None = None, issue_scores_by_repo: dict[str, float] | None = None):
    merged_prs = []
    for repo, scores in (pr_scores_by_repo or {}).items():
        for score in scores:
            merged_prs.append(SimpleNamespace(repository_full_name=repo, earned_score=score))
    return SimpleNamespace(uid=uid, merged_prs=merged_prs, issue_discovery_scores_by_repo=issue_scores_by_repo or {})


def _idx(uids: list[int], uid: int) -> int:
    return sorted(uids).index(uid)


def test_sum_one_with_activity_sends_zero_to_recycle():
    uids = [RECYCLE_UID, 1, ISSUES_TREASURY_UID]
    repos = {"r/a": RepositoryConfig(emission_share=1.0, issue_discovery_share=0.0)}
    evals = {1: _eval(1, pr_scores_by_repo={"r/a": [10.0]})}
    rewards = blend_emission_pools(evals, set(uids), repos)
    assert rewards[_idx(uids, 1)] == pytest.approx(0.9)
    assert rewards[_idx(uids, ISSUES_TREASURY_UID)] == pytest.approx(0.1)
    assert rewards[_idx(uids, RECYCLE_UID)] == pytest.approx(0.0)


def test_no_activity_sends_oss_pool_to_recycle():
    uids = [RECYCLE_UID, 1, ISSUES_TREASURY_UID]
    repos = {"r/a": RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    evals = {1: _eval(1)}
    rewards = blend_emission_pools(evals, set(uids), repos)
    assert rewards[_idx(uids, ISSUES_TREASURY_UID)] == pytest.approx(0.1)
    assert rewards[_idx(uids, RECYCLE_UID)] == pytest.approx(0.9)


def test_registry_slack_recycles_same_round():
    uids = [RECYCLE_UID, 1, ISSUES_TREASURY_UID]
    repos = {"r/a": RepositoryConfig(emission_share=0.8, issue_discovery_share=0.0)}
    evals = {1: _eval(1, pr_scores_by_repo={"r/a": [1.0]})}
    rewards = blend_emission_pools(evals, set(uids), repos)
    assert rewards[_idx(uids, 1)] == pytest.approx(0.72)
    assert rewards[_idx(uids, RECYCLE_UID)] == pytest.approx(0.18)
    assert rewards[_idx(uids, ISSUES_TREASURY_UID)] == pytest.approx(0.1)


def test_within_repo_spill_pr_active_issue_empty():
    uids = [RECYCLE_UID, 1, ISSUES_TREASURY_UID]
    repos = {"r/a": RepositoryConfig(emission_share=1.0, issue_discovery_share=0.3)}
    evals = {1: _eval(1, pr_scores_by_repo={"r/a": [5.0]})}
    rewards = blend_emission_pools(evals, set(uids), repos)
    assert rewards[_idx(uids, 1)] == pytest.approx(0.9)
    assert rewards[_idx(uids, RECYCLE_UID)] == pytest.approx(0.0)


def test_repo_slice_constant_across_pr_count():
    uids = [RECYCLE_UID, 1, 2, ISSUES_TREASURY_UID]
    repos = {"r/a": RepositoryConfig(emission_share=0.05, issue_discovery_share=0.0)}

    one_pr = blend_emission_pools({1: _eval(1, pr_scores_by_repo={"r/a": [1.0]})}, set(uids), repos)
    many_pr = blend_emission_pools(
        {
            1: _eval(1, pr_scores_by_repo={"r/a": [1.0] * 25}),
            2: _eval(2, pr_scores_by_repo={"r/a": [1.0] * 25}),
        },
        set(uids),
        repos,
    )
    assert one_pr[_idx(uids, 1)] == pytest.approx(0.045)
    assert many_pr[_idx(uids, 1)] + many_pr[_idx(uids, 2)] == pytest.approx(0.045)
