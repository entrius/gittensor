"""Unit tests for ``build_round_reward_vector`` (repo emission_share allocation)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, cast

import pytest

from gittensor.classes import MinerEvaluation
from gittensor.constants import ISSUES_TREASURY_UID, OSS_EMISSION_SHARE, RECYCLE_UID
from gittensor.validator.oss_contributions.emission_allocate import build_round_reward_vector
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
from gittensor.validator.utils.load_weights import RepositoryConfig


@dataclass
class _StubScoredPR:
    repository_full_name: str
    earned_score: float


def _merged_prs(*stubs: _StubScoredPR) -> List[ScoredPR]:
    return cast(List[ScoredPR], list(stubs))


def _uid_index(miner_uids):
    return {u: i for i, u in enumerate(sorted(miner_uids))}


def test_full_activity_sum_shares_one_zero_recycle():
    """Σ emission_share = 1, one repo with PR scorer → miner gets full OSS pool; treasury flat; recycle 0."""
    master = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    ev = MinerEvaluation(uid=5, hotkey='h', github_id='1')
    ev.merged_prs = _merged_prs(_StubScoredPR('acme/r', 50.0))
    ev.issue_discovery_repo_scores = {}
    miner_uids = {RECYCLE_UID, 5, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({5: ev}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[5]] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(0.0)
    assert rewards[idx[ISSUES_TREASURY_UID]] == pytest.approx(0.10)
    assert float(rewards.sum()) == pytest.approx(1.0)


def test_no_activity_all_recycle_except_treasury():
    master = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    miner_uids = {RECYCLE_UID, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[idx[ISSUES_TREASURY_UID]] == pytest.approx(0.10)
    assert float(rewards.sum()) == pytest.approx(1.0)


def test_registry_slack_sum_below_one():
    """Σ emission_share = 0.8 → 72% to active OSS miners, 18% registry slack to recycle, 10% treasury."""
    master = {'acme/r': RepositoryConfig(emission_share=0.8, issue_discovery_share=0.5)}
    ev = MinerEvaluation(uid=7, hotkey='h', github_id='1')
    ev.merged_prs = _merged_prs(_StubScoredPR('acme/r', 1.0))
    miner_uids = {RECYCLE_UID, 7, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({7: ev}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[7]] == pytest.approx(0.72)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(0.18)
    assert rewards[idx[ISSUES_TREASURY_UID]] == pytest.approx(0.10)
    assert float(rewards.sum()) == pytest.approx(1.0)


def test_repo_slice_recycles_when_both_sides_empty():
    master = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    miner_uids = {RECYCLE_UID, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(0.90)


def test_issue_discovery_share_spill_pr_empty_to_issue():
    """issue_discovery_share=0.3, PR side empty, issue side active → full repo slice on issues."""
    master = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.3)}
    ev = MinerEvaluation(uid=3, hotkey='h', github_id='1')
    ev.merged_prs = []
    ev.issue_discovery_repo_scores = {'acme/r': 12.0}
    miner_uids = {RECYCLE_UID, 3, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({3: ev}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[3]] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(0.0)


def test_issue_side_empty_spill_to_pr():
    """issue_discovery_share=0.7, issue side empty, PR side active → full repo slice on PRs."""
    master = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.7)}
    ev = MinerEvaluation(uid=2, hotkey='h', github_id='1')
    ev.merged_prs = _merged_prs(_StubScoredPR('acme/r', 40.0))
    ev.issue_discovery_repo_scores = {}
    miner_uids = {RECYCLE_UID, 2, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({2: ev}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[2]] == pytest.approx(OSS_EMISSION_SHARE)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(0.0)


def test_emission_share_slice_invariant_one_pr_vs_many_same_miner():
    """Repo slice is emission_share × OSS pool; intra-repo split is by relative earned_score."""
    share = 0.05
    master = {'acme/r': RepositoryConfig(emission_share=share, issue_discovery_share=0.0)}
    one_pr = MinerEvaluation(uid=1, hotkey='a', github_id='1')
    one_pr.merged_prs = _merged_prs(_StubScoredPR('acme/r', 100.0))
    many = MinerEvaluation(uid=1, hotkey='a', github_id='1')
    many.merged_prs = _merged_prs(*[_StubScoredPR('acme/r', 10.0) for _ in range(50)])
    miner_uids = {RECYCLE_UID, 1, ISSUES_TREASURY_UID}
    r1 = build_round_reward_vector({1: one_pr}, master, miner_uids)
    r2 = build_round_reward_vector({1: many}, master, miner_uids)
    idx = _uid_index(miner_uids)
    expected = OSS_EMISSION_SHARE * share
    assert r1[idx[1]] == pytest.approx(expected)
    assert r2[idx[1]] == pytest.approx(expected)


def test_all_zero_earned_scores_recycle_repo_slice():
    """PR and issue weights are zero → repo slice recycles within the round."""
    master = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.5)}
    ev = MinerEvaluation(uid=4, hotkey='h', github_id='1')
    ev.merged_prs = _merged_prs(_StubScoredPR('acme/r', 0.0))
    ev.issue_discovery_repo_scores = {'acme/r': 0.0}
    miner_uids = {RECYCLE_UID, 4, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({4: ev}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[4]] == pytest.approx(0.0)
    assert rewards[idx[RECYCLE_UID]] == pytest.approx(OSS_EMISSION_SHARE)


def test_pr_count_invariant_same_repo_slice_one_vs_many_prs():
    """Same repo slice is split by relative earned_score (two miners, two PRs)."""
    master = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=0.0)}
    a = MinerEvaluation(uid=1, hotkey='a', github_id='1')
    a.merged_prs = _merged_prs(_StubScoredPR('acme/r', 10.0))
    b = MinerEvaluation(uid=2, hotkey='b', github_id='2')
    b.merged_prs = _merged_prs(_StubScoredPR('acme/r', 30.0))
    miner_uids = {RECYCLE_UID, 1, 2, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({1: a, 2: b}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[1]] / rewards[idx[2]] == pytest.approx(10.0 / 30.0)
    assert rewards[idx[1]] + rewards[idx[2]] == pytest.approx(OSS_EMISSION_SHARE)


def test_issue_side_only_issue_discovery_share_one():
    master = {'acme/r': RepositoryConfig(emission_share=1.0, issue_discovery_share=1.0)}
    ev = MinerEvaluation(uid=9, hotkey='h', github_id='1')
    ev.merged_prs = []
    ev.issue_discovery_repo_scores = {'acme/r': 5.0}
    miner_uids = {RECYCLE_UID, 9, ISSUES_TREASURY_UID}
    rewards = build_round_reward_vector({9: ev}, master, miner_uids)
    idx = _uid_index(miner_uids)
    assert rewards[idx[9]] == pytest.approx(OSS_EMISSION_SHARE)
