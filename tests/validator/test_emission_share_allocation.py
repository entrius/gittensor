from types import SimpleNamespace
from typing import Any, cast

import pytest

from gittensor.classes import Issue, MinerEvaluation
from gittensor.validator.forward import blend_emission_pools
from gittensor.validator.utils.load_weights import RepositoryConfig


def _eval(uid: int, repo_scores=None, issue_scores=None) -> MinerEvaluation:
    evaluation = MinerEvaluation(uid=uid, hotkey=f'hotkey-{uid}', github_id=f'github-{uid}')
    evaluation.merged_prs = cast(
        Any,
        [SimpleNamespace(repository_full_name=repo, earned_score=score) for repo, score in (repo_scores or [])],
    )
    evaluation.discovered_issues = [
        Issue(
            number=idx + 1, pr_number=idx + 10, repository_full_name=repo, title='issue', discovery_earned_score=score
        )
        for idx, (repo, score) in enumerate(issue_scores or [])
    ]
    return evaluation


def test_active_repo_receives_fixed_slice_regardless_of_pr_count():
    repos = {
        'repo/a': RepositoryConfig(emission_share=0.05, issue_discovery_share=0.0),
        'repo/b': RepositoryConfig(emission_share=0.05, issue_discovery_share=0.0),
    }
    many_prs = [('repo/b', 1.0) for _ in range(50)]
    evaluations = {
        1: _eval(1, [('repo/a', 10.0)]),
        2: _eval(2, many_prs),
        0: _eval(0),
    }

    rewards = blend_emission_pools({0, 1, 2}, evaluations, repos)

    assert rewards[1] == pytest.approx(0.90 * 0.05)
    assert rewards[2] == pytest.approx(0.90 * 0.05)
    assert rewards[0] == pytest.approx(0.90 * 0.90)


def test_issue_slice_spills_to_pr_side_within_same_repo():
    repos = {'repo/a': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.3)}
    evaluations = {
        1: _eval(1, [('repo/a', 10.0)]),
        0: _eval(0),
    }

    rewards = blend_emission_pools({0, 1}, evaluations, repos)

    assert rewards[1] == pytest.approx(0.90 * 0.2)
    assert rewards[0] == pytest.approx(0.90 * 0.8)


def test_pr_slice_spills_to_issue_side_within_same_repo():
    repos = {'repo/a': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.3)}
    evaluations = {
        1: _eval(1, issue_scores=[('repo/a', 10.0)]),
        0: _eval(0),
    }

    rewards = blend_emission_pools({0, 1}, evaluations, repos)

    assert rewards[1] == pytest.approx(0.90 * 0.2)
    assert rewards[0] == pytest.approx(0.90 * 0.8)


def test_issue_discovery_share_zero_disables_issue_rewards_and_recycles_empty_pr_side():
    repos = {'repo/a': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.0)}
    evaluations = {
        1: _eval(1, issue_scores=[('repo/a', 10.0)]),
        0: _eval(0),
    }

    rewards = blend_emission_pools({0, 1}, evaluations, repos)

    assert rewards[1] == pytest.approx(0.0)
    assert rewards[0] == pytest.approx(0.90)


def test_repo_slice_recycles_when_both_sides_are_empty():
    repos = {'repo/a': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    evaluations = {0: _eval(0), 1: _eval(1)}

    rewards = blend_emission_pools({0, 1}, evaluations, repos)

    assert rewards[0] == pytest.approx(0.90)
    assert rewards[1] == pytest.approx(0.0)


def test_pr_and_issue_sides_split_by_repo_config():
    repos = {'repo/a': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.25)}
    evaluations = {
        1: _eval(1, [('repo/a', 3.0)]),
        2: _eval(2, [('repo/a', 1.0)], [('repo/a', 4.0)]),
        0: _eval(0),
    }

    rewards = blend_emission_pools({0, 1, 2}, evaluations, repos)

    assert rewards[1] == pytest.approx(0.90 * 0.75 * 0.75)
    assert rewards[2] == pytest.approx((0.90 * 0.75 * 0.25) + (0.90 * 0.25))
    assert rewards[0] == pytest.approx(0.0)


def test_fully_active_full_registry_sends_nothing_to_recycle():
    repos = {'repo/a': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.0)}
    evaluations = {
        0: _eval(0),
        1: _eval(1, [('repo/a', 5.0)]),
        111: _eval(111),
    }

    rewards = blend_emission_pools({0, 1, 111}, evaluations, repos)

    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(0.90)
    assert rewards[2] == pytest.approx(0.10)
    assert rewards.sum() == pytest.approx(1.0)


def test_no_repo_activity_recycles_scoring_pool_and_preserves_treasury():
    repos = {'repo/a': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    evaluations = {0: _eval(0), 1: _eval(1), 111: _eval(111)}

    rewards = blend_emission_pools({0, 1, 111}, evaluations, repos)

    assert rewards[0] == pytest.approx(0.90)
    assert rewards[1] == pytest.approx(0.0)
    assert rewards[2] == pytest.approx(0.10)
    assert rewards.sum() == pytest.approx(1.0)


def test_registry_slack_recycles_without_redistributing_to_active_repos():
    repos = {'repo/a': RepositoryConfig(emission_share=0.8, issue_discovery_share=0.0)}
    evaluations = {
        0: _eval(0),
        1: _eval(1, [('repo/a', 5.0)]),
        111: _eval(111),
    }

    rewards = blend_emission_pools({0, 1, 111}, evaluations, repos)

    assert rewards[0] == pytest.approx(0.90 * 0.2)
    assert rewards[1] == pytest.approx(0.90 * 0.8)
    assert rewards[2] == pytest.approx(0.10)
    assert rewards.sum() == pytest.approx(1.0)
