from types import SimpleNamespace

import pytest

from gittensor.classes import MinerEvaluation
from gittensor.validator.forward import blend_emission_pools
from gittensor.validator.utils.load_weights import RepositoryConfig


def _evaluation(uid: int, prs=None, issues=None) -> MinerEvaluation:
    evaluation = MinerEvaluation(uid=uid, hotkey=f'hotkey-{uid}', github_id=str(uid))
    evaluation.merged_prs = list(prs or [])
    evaluation.discovered_issues = list(issues or [])
    return evaluation


def _pr(repo: str, score: float):
    return SimpleNamespace(repository_full_name=repo, earned_score=score)


def _issue(repo: str, score: float):
    return SimpleNamespace(repository_full_name=repo, discovery_earned_score=score)


def _rewards(miner_evaluations, repositories):
    uids = set(miner_evaluations) | {0, 111}
    rewards = blend_emission_pools(miner_evaluations, repositories, uids)
    return dict(zip(sorted(uids), rewards))


def test_single_pr_claims_full_repo_slice():
    repositories = {'foo/repo': RepositoryConfig(emission_share=0.05, issue_discovery_share=0.0)}
    miner_evaluations = {1: _evaluation(1, prs=[_pr('foo/repo', 10.0)])}

    rewards = _rewards(miner_evaluations, repositories)

    assert rewards[1] == pytest.approx(0.045)
    assert rewards[0] == pytest.approx(0.855)
    assert rewards[111] == pytest.approx(0.1)
    assert sum(rewards.values()) == pytest.approx(1.0)


def test_many_prs_split_same_repo_slice_proportionally():
    repositories = {'foo/repo': RepositoryConfig(emission_share=0.05, issue_discovery_share=0.0)}
    miner_evaluations = {
        1: _evaluation(1, prs=[_pr('foo/repo', 1.0)]),
        2: _evaluation(2, prs=[_pr('foo/repo', 1.0) for _ in range(49)]),
    }

    rewards = _rewards(miner_evaluations, repositories)

    assert rewards[1] == pytest.approx(0.045 / 50)
    assert rewards[2] == pytest.approx(0.045 * 49 / 50)
    assert rewards[0] == pytest.approx(0.855)


def test_issue_subslice_spills_to_pr_side_inside_same_repo():
    repositories = {'foo/repo': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.3)}
    miner_evaluations = {1: _evaluation(1, prs=[_pr('foo/repo', 5.0)])}

    rewards = _rewards(miner_evaluations, repositories)

    assert rewards[1] == pytest.approx(0.18)
    assert rewards[0] == pytest.approx(0.72)


def test_pr_subslice_spills_to_issue_side_inside_same_repo():
    repositories = {'foo/repo': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.3)}
    miner_evaluations = {1: _evaluation(1, issues=[_issue('foo/repo', 5.0)])}

    rewards = _rewards(miner_evaluations, repositories)

    assert rewards[1] == pytest.approx(0.18)
    assert rewards[0] == pytest.approx(0.72)


def test_disabled_issue_side_does_not_claim_repo_slice():
    repositories = {'foo/repo': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.0)}
    miner_evaluations = {1: _evaluation(1, issues=[_issue('foo/repo', 5.0)])}

    rewards = _rewards(miner_evaluations, repositories)

    assert rewards[1] == pytest.approx(0.0)
    assert rewards[0] == pytest.approx(0.9)
    assert rewards[111] == pytest.approx(0.1)


def test_no_activity_recycles_entire_scoring_pool():
    repositories = {'foo/repo': RepositoryConfig(emission_share=0.1, issue_discovery_share=0.5)}
    miner_evaluations = {1: _evaluation(1)}

    rewards = _rewards(miner_evaluations, repositories)

    assert rewards[1] == pytest.approx(0.0)
    assert rewards[0] == pytest.approx(0.9)
    assert rewards[111] == pytest.approx(0.1)
    assert sum(rewards.values()) == pytest.approx(1.0)


def test_registry_sum_point_eight_routes_shortfall_to_recycle():
    repositories = {
        'foo/a': RepositoryConfig(emission_share=0.4, issue_discovery_share=0.0),
        'foo/b': RepositoryConfig(emission_share=0.4, issue_discovery_share=0.0),
    }
    miner_evaluations = {
        1: _evaluation(1, prs=[_pr('foo/a', 1.0)]),
        2: _evaluation(2, prs=[_pr('foo/b', 1.0)]),
    }

    rewards = _rewards(miner_evaluations, repositories)

    assert rewards[1] == pytest.approx(0.36)
    assert rewards[2] == pytest.approx(0.36)
    assert rewards[0] == pytest.approx(0.18)
    assert rewards[111] == pytest.approx(0.1)


def test_full_registry_sum_with_all_repos_active_has_no_recycle():
    repositories = {
        'foo/a': RepositoryConfig(emission_share=0.5, issue_discovery_share=0.0),
        'foo/b': RepositoryConfig(emission_share=0.5, issue_discovery_share=0.0),
    }
    miner_evaluations = {
        1: _evaluation(1, prs=[_pr('foo/a', 1.0)]),
        2: _evaluation(2, prs=[_pr('foo/b', 1.0)]),
    }

    rewards = _rewards(miner_evaluations, repositories)

    assert rewards[1] == pytest.approx(0.45)
    assert rewards[2] == pytest.approx(0.45)
    assert rewards[0] == pytest.approx(0.0)
    assert rewards[111] == pytest.approx(0.1)
    assert sum(rewards.values()) == pytest.approx(1.0)
