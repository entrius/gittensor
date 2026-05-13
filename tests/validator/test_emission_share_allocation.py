from types import SimpleNamespace
from typing import Any, cast

import numpy as np
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
            number=idx + 1,
            pr_number=idx + 10,
            repository_full_name=repo,
            title='issue',
            discovery_earned_score=score,
        )
        for idx, (repo, score) in enumerate(issue_scores or [])
    ]
    return evaluation


def test_active_repo_receives_fixed_slice_regardless_of_pr_count():
    repos = {
        'repo/a': RepositoryConfig(emission_share=0.05, issue_discovery_share=0.0),
        'repo/b': RepositoryConfig(emission_share=0.05, issue_discovery_share=0.0),
    }
    evaluations = {
        0: _eval(0),
        1: _eval(1, [('repo/a', 10.0)]),
        2: _eval(2, [('repo/b', 1.0) for _ in range(50)]),
    }

    rewards = blend_emission_pools(np.zeros(3), np.zeros(3), {0, 1, 2}, evaluations, repos)

    assert rewards[0] == pytest.approx(0.90 * 0.90)
    assert rewards[1] == pytest.approx(0.90 * 0.05)
    assert rewards[2] == pytest.approx(0.90 * 0.05)


def test_pr_and_issue_sides_split_and_spill_within_same_repo():
    repos = {
        'repo/split': RepositoryConfig(emission_share=0.4, issue_discovery_share=0.25),
        'repo/pr-only': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.5),
        'repo/empty': RepositoryConfig(emission_share=0.1, issue_discovery_share=0.5),
    }
    evaluations = {
        0: _eval(0),
        1: _eval(1, [('repo/split', 3.0)]),
        2: _eval(2, [('repo/split', 1.0)], [('repo/split', 4.0)]),
        3: _eval(3, [('repo/pr-only', 9.0)]),
    }

    rewards = blend_emission_pools(np.zeros(4), np.zeros(4), {0, 1, 2, 3}, evaluations, repos)

    assert rewards[0] == pytest.approx(0.90 * 0.4)  # registry slack + empty repo
    assert rewards[1] == pytest.approx(0.90 * 0.4 * 0.75 * 0.75)
    assert rewards[2] == pytest.approx((0.90 * 0.4 * 0.75 * 0.25) + (0.90 * 0.4 * 0.25))
    assert rewards[3] == pytest.approx(0.90 * 0.2)  # issue side spills to PR side


def test_no_repo_activity_recycles_scoring_pool_and_preserves_treasury():
    repos = {'repo/a': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    evaluations = {0: _eval(0), 1: _eval(1), 111: _eval(111)}

    rewards = blend_emission_pools(np.zeros(3), np.zeros(3), {0, 1, 111}, evaluations, repos)

    assert rewards[0] == pytest.approx(0.90)
    assert rewards[1] == pytest.approx(0.0)
    assert rewards[2] == pytest.approx(0.10)
    assert rewards.sum() == pytest.approx(1.0)


def test_fallback_blend_combines_legacy_normalized_arrays_when_raw_scores_unavailable():
    rewards = blend_emission_pools(
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, 0.0, 1.0]),
        {0, 1, 2},
    )

    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(0.45)
    assert rewards[2] == pytest.approx(0.45)
