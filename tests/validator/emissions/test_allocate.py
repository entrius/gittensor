from types import SimpleNamespace
from typing import Dict, Optional, cast

import pytest

from gittensor.classes import MinerEvaluation
from gittensor.constants import ISSUES_TREASURY_EMISSION_SHARE, OSS_EMISSION_SHARE, RECYCLE_UID
from gittensor.validator.emissions.allocate import allocate_emissions
from gittensor.validator.forward import apply_collateral_deductions
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
from gittensor.validator.utils.load_weights import RepositoryConfig


def _eval(
    uid: int,
    repo_scores: Optional[Dict[str, float]] = None,
    issue_scores: Optional[Dict[str, float]] = None,
):
    evaluation = MinerEvaluation(uid=uid, hotkey=f'hk{uid}', github_id=str(uid))
    for repo, score in (repo_scores or {}).items():
        evaluation.merged_prs.append(cast(ScoredPR, SimpleNamespace(repository_full_name=repo, earned_score=score)))
    evaluation.issue_discovery_score_by_repo = issue_scores or {}
    return evaluation


def test_allocates_pr_scores_within_repository_slice():
    evaluations = {
        1: _eval(1, {'entrius/gittensor': 10.0}),
        2: _eval(2, {'entrius/gittensor': 30.0}),
    }
    repos = {'entrius/gittensor': RepositoryConfig(emission_share=0.5, issue_discovery_share=0.5)}

    rewards = allocate_emissions(evaluations, repos, {1, 2})

    # No issue scores are active, so the issue half spills to PRs.
    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE * 0.5 * 0.25)
    assert rewards[1] == pytest.approx(OSS_EMISSION_SHARE * 0.5 * 0.75)


def test_splits_pr_and_issue_scores_inside_active_repository_slice():
    evaluations = {
        1: _eval(1, {'entrius/gittensor': 10.0}, {'entrius/gittensor': 90.0}),
        2: _eval(2, {'entrius/gittensor': 30.0}, {'entrius/gittensor': 10.0}),
    }
    repos = {'entrius/gittensor': RepositoryConfig(emission_share=0.5, issue_discovery_share=0.2)}

    rewards = allocate_emissions(evaluations, repos, {1, 2})

    repo_pool = OSS_EMISSION_SHARE * 0.5
    assert rewards[0] == pytest.approx(repo_pool * 0.8 * 0.25 + repo_pool * 0.2 * 0.9)
    assert rewards[1] == pytest.approx(repo_pool * 0.8 * 0.75 + repo_pool * 0.2 * 0.1)


def test_recycles_inactive_repo_slices_and_registry_slack():
    evaluations = {1: _eval(1)}
    repos = {'entrius/gittensor': RepositoryConfig(emission_share=0.25)}

    rewards = allocate_emissions(evaluations, repos, {RECYCLE_UID, 1})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[1] == pytest.approx(0.0)


def test_treasury_share_is_added_when_treasury_uid_is_present():
    evaluations = {111: _eval(111)}
    rewards = allocate_emissions(evaluations, {}, {111})

    assert rewards[0] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)


def test_issue_discovery_share_zero_routes_full_slice_to_pr_side():
    evaluations = {1: _eval(1, {'entrius/oc-1': 5.0}, {'entrius/oc-1': 100.0})}
    repos = {'entrius/oc-1': RepositoryConfig(emission_share=0.5, issue_discovery_share=0.0)}

    rewards = allocate_emissions(evaluations, repos, {1})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE * 0.5)


def test_issue_discovery_share_one_routes_full_slice_to_issue_side():
    evaluations = {1: _eval(1, {'entrius/gittensor': 100.0}, {'entrius/gittensor': 5.0})}
    repos = {'entrius/gittensor': RepositoryConfig(emission_share=0.25, issue_discovery_share=1.0)}

    rewards = allocate_emissions(evaluations, repos, {1})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE * 0.25)


def test_pr_side_spills_to_issue_side_when_pr_inactive():
    evaluations = {
        1: _eval(1, issue_scores={'entrius/gittensor': 30.0}),
        2: _eval(2, issue_scores={'entrius/gittensor': 10.0}),
    }
    repos = {'entrius/gittensor': RepositoryConfig(emission_share=0.4, issue_discovery_share=0.25)}

    rewards = allocate_emissions(evaluations, repos, {1, 2})

    repo_pool = OSS_EMISSION_SHARE * 0.4
    assert rewards[0] == pytest.approx(repo_pool * 0.75)
    assert rewards[1] == pytest.approx(repo_pool * 0.25)


def test_issue_side_spills_to_pr_side_when_issue_inactive():
    evaluations = {
        1: _eval(1, {'entrius/gittensor': 30.0}),
        2: _eval(2, {'entrius/gittensor': 10.0}),
    }
    repos = {'entrius/gittensor': RepositoryConfig(emission_share=0.4, issue_discovery_share=0.25)}

    rewards = allocate_emissions(evaluations, repos, {1, 2})

    repo_pool = OSS_EMISSION_SHARE * 0.4
    assert rewards[0] == pytest.approx(repo_pool * 0.75)
    assert rewards[1] == pytest.approx(repo_pool * 0.25)


def test_cross_repo_issue_discovery_attaches_to_issue_home_repo():
    evaluations = {
        1: _eval(1, {'entrius/gittensor': 100.0}, {'entrius/gittensor-ui': 25.0}),
    }
    repos = {
        'entrius/gittensor': RepositoryConfig(emission_share=0.1, issue_discovery_share=0.0),
        'entrius/gittensor-ui': RepositoryConfig(emission_share=0.3, issue_discovery_share=1.0),
    }

    rewards = allocate_emissions(evaluations, repos, {1})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE * 0.4)


def test_uid_earning_from_multiple_repos_sums_slices():
    evaluations = {
        1: _eval(1, {'repo/a': 10.0, 'repo/b': 20.0}, {'repo/c': 30.0}),
    }
    repos = {
        'repo/a': RepositoryConfig(emission_share=0.1, issue_discovery_share=0.0),
        'repo/b': RepositoryConfig(emission_share=0.2, issue_discovery_share=0.0),
        'repo/c': RepositoryConfig(emission_share=0.3, issue_discovery_share=1.0),
    }

    rewards = allocate_emissions(evaluations, repos, {1})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE * 0.6)


def test_round_totals_sum_to_one_when_all_repos_active_and_registry_full():
    evaluations = {
        1: _eval(1, {'repo/a': 1.0}),
        2: _eval(2, issue_scores={'repo/b': 1.0}),
        111: _eval(111),
        RECYCLE_UID: _eval(RECYCLE_UID),
    }
    repos = {
        'repo/a': RepositoryConfig(emission_share=0.4),
        'repo/b': RepositoryConfig(emission_share=0.6),
    }

    rewards = allocate_emissions(evaluations, repos, {RECYCLE_UID, 1, 2, 111})

    assert rewards.sum() == pytest.approx(1.0)
    assert rewards[0] == pytest.approx(0.0)
    assert rewards[1] == pytest.approx(OSS_EMISSION_SHARE * 0.4)
    assert rewards[2] == pytest.approx(OSS_EMISSION_SHARE * 0.6)
    assert rewards[3] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)


def test_round_with_no_activity_recycles_oss_pool():
    evaluations = {
        RECYCLE_UID: _eval(RECYCLE_UID),
        111: _eval(111),
    }
    repos = {
        'repo/a': RepositoryConfig(emission_share=0.4),
        'repo/b': RepositoryConfig(emission_share=0.6),
    }

    rewards = allocate_emissions(evaluations, repos, {RECYCLE_UID, 111})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[1] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)
    assert rewards.sum() == pytest.approx(1.0)


def test_registry_slack_recycles_without_redistributing_to_active_repos():
    evaluations = {
        RECYCLE_UID: _eval(RECYCLE_UID),
        1: _eval(1, {'repo/a': 100.0}),
        2: _eval(2, {'repo/b': 100.0}),
        111: _eval(111),
    }
    repos = {
        'repo/a': RepositoryConfig(emission_share=0.3),
        'repo/b': RepositoryConfig(emission_share=0.5),
    }

    rewards = allocate_emissions(evaluations, repos, {RECYCLE_UID, 1, 2, 111})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE * 0.2)
    assert rewards[1] == pytest.approx(OSS_EMISSION_SHARE * 0.3)
    assert rewards[2] == pytest.approx(OSS_EMISSION_SHARE * 0.5)
    assert rewards[3] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)
    assert rewards.sum() == pytest.approx(1.0)


def test_failed_evaluations_do_not_receive_issue_side_rewards():
    evaluation = _eval(1, issue_scores={'repo/a': 100.0})
    evaluation.failed_reason = 'Penalized'
    evaluations = {
        RECYCLE_UID: _eval(RECYCLE_UID),
        1: evaluation,
    }
    repos = {'repo/a': RepositoryConfig(emission_share=0.5, issue_discovery_share=1.0)}

    rewards = allocate_emissions(evaluations, repos, {RECYCLE_UID, 1})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[1] == pytest.approx(0.0)


def test_collateral_deductions_reduce_allocated_scoring_reward():
    evaluations = {1: _eval(1, {'repo/a': 100.0})}
    evaluations[1].total_collateral_score = 0.1
    repos = {'repo/a': RepositoryConfig(emission_share=0.5)}
    rewards = allocate_emissions(evaluations, repos, {1})

    apply_collateral_deductions(rewards, evaluations, {1})

    assert rewards[0] == pytest.approx(OSS_EMISSION_SHARE * 0.4)
