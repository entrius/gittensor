# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for repo-bounded round allocation.

These cover the allocation scenarios required by issue #1215: repo slices cap
PR throughput, empty slices recycle instead of redistributing across repos,
PR/issue sub-slices spill only within a repo, registry slack recycles, and the
fixed recycle baseline is gone.
"""

from datetime import datetime, timezone

import pytest

from gittensor.classes import Issue, MinerEvaluation
from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.utils.mirror.models import MirrorPullRequest, MirrorReviewSummary
from gittensor.validator.emission_allocation import blend_emission_pools
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
from gittensor.validator.utils.load_weights import RepositoryConfig, load_master_repo_weights


def _uids(*extra: int) -> set[int]:
    return set(extra) | {RECYCLE_UID, ISSUES_TREASURY_UID}


def _idx(uids: set[int], uid: int) -> int:
    return sorted(uids).index(uid)


def _evaluation(uid: int, prs=None, issues=None) -> MinerEvaluation:
    evaluation = MinerEvaluation(uid=uid, hotkey=f'hk-{uid}', github_id=str(uid))
    evaluation.merged_prs = list(prs or [])
    evaluation.issue_discovery_issues = list(issues or [])
    return evaluation


def _scored_pr(repo: str, number: int, earned_score: float) -> ScoredPR:
    now = datetime.now(timezone.utc)
    pr = MirrorPullRequest(
        repo_full_name=repo,
        pr_number=number,
        title='PR',
        body=None,
        state='MERGED',
        author_github_id='1',
        author_login='miner',
        author_association='CONTRIBUTOR',
        created_at=now,
        closed_at=now,
        merged_at=now,
        last_edited_at=None,
        edited_after_merge=False,
        hours_since_merge=1.0,
        merged_by_login='maintainer',
        base_ref='main',
        head_ref='feature',
        head_repo_full_name=repo,
        default_branch='main',
        head_sha='h',
        base_sha='b',
        merge_base_sha='mb',
        additions=1,
        deletions=0,
        commits_count=1,
        scoring_data_stored=True,
        review_summary=MirrorReviewSummary(),
    )
    return ScoredPR(pr=pr, earned_score=earned_score)


def _discovered_issue(repo: str, number: int, earned_score: float) -> Issue:
    return Issue(
        number=number,
        pr_number=number + 1000,
        repository_full_name=repo,
        title='issue',
        discovery_earned_score=earned_score,
    )


def _config(
    emission_share: float,
    issue_discovery_share: float = 0.5,
    maintainer_cut: float = 0.0,
) -> RepositoryConfig:
    return RepositoryConfig(
        emission_share=emission_share,
        issue_discovery_share=issue_discovery_share,
        maintainer_cut=maintainer_cut,
    )


class TestAllocationInvarianceToPrVolume:
    def test_one_eligible_pr_claims_full_repo_slice(self):
        repos = {'r/one': _config(emission_share=0.05, issue_discovery_share=0.0)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, prs=[_scored_pr('r/one', 100, earned_score=10.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.05 * OSS_EMISSION_SHARE)

    def test_fifty_eligible_prs_split_same_repo_slice_proportionally(self):
        repos = {'r/many': _config(emission_share=0.05, issue_discovery_share=0.0)}
        miner_uids = _uids(1, 2)
        evaluations = {
            1: _evaluation(1, prs=[_scored_pr('r/many', number, 30.0) for number in range(25)]),
            2: _evaluation(2, prs=[_scored_pr('r/many', number + 100, 20.0) for number in range(25)]),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        repo_slice = 0.05 * OSS_EMISSION_SHARE
        assert rewards[_idx(miner_uids, 1)] == pytest.approx(repo_slice * 0.6)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(repo_slice * 0.4)
        assert rewards[_idx(miner_uids, 1)] + rewards[_idx(miner_uids, 2)] == pytest.approx(repo_slice)


class TestCrossRepoIsolation:
    def test_empty_repo_slice_recycles_without_redistribution(self):
        repos = {
            'r/active': _config(emission_share=0.4, issue_discovery_share=0.0),
            'r/empty': _config(emission_share=0.6, issue_discovery_share=0.0),
        }
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, prs=[_scored_pr('r/active', 100, earned_score=10.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.4 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(0.6 * OSS_EMISSION_SHARE)


class TestWithinRepoSpill:
    def test_issue_side_empty_spills_to_pr_side(self):
        repos = {'r/spill-pr': _config(emission_share=0.1, issue_discovery_share=0.6)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, prs=[_scored_pr('r/spill-pr', 100, earned_score=10.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.1 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(0.9 * OSS_EMISSION_SHARE)

    def test_pr_side_empty_spills_to_issue_side(self):
        repos = {'r/spill-issue': _config(emission_share=0.1, issue_discovery_share=0.3)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, issues=[_discovered_issue('r/spill-issue', 10, earned_score=5.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.1 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(0.9 * OSS_EMISSION_SHARE)

    def test_both_sides_active_split_by_repo_issue_discovery_share(self):
        repos = {'r/both': _config(emission_share=0.1, issue_discovery_share=0.4)}
        miner_uids = _uids(1, 2)
        evaluations = {
            1: _evaluation(1, prs=[_scored_pr('r/both', 100, earned_score=10.0)]),
            2: _evaluation(2, issues=[_discovered_issue('r/both', 10, earned_score=20.0)]),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        repo_slice = 0.1 * OSS_EMISSION_SHARE
        assert rewards[_idx(miner_uids, 1)] == pytest.approx(repo_slice * 0.6)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(repo_slice * 0.4)


class TestRecyclePolicyShift:
    def test_full_activity_full_sum_sends_zero_to_recycle(self):
        repos = {
            'r/a': _config(emission_share=0.5, issue_discovery_share=0.0),
            'r/b': _config(emission_share=0.5, issue_discovery_share=0.0),
        }
        miner_uids = _uids(1, 2)
        evaluations = {
            1: _evaluation(1, prs=[_scored_pr('r/a', 1, earned_score=10.0)]),
            2: _evaluation(2, prs=[_scored_pr('r/b', 2, earned_score=10.0)]),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.5 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(0.5 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, ISSUES_TREASURY_UID)] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(0.0)

    def test_no_activity_anywhere_routes_oss_pool_to_recycle(self):
        repos = {
            'r/a': _config(emission_share=0.5),
            'r/b': _config(emission_share=0.5),
        }
        miner_uids = _uids(1, 2)
        evaluations = {1: _evaluation(1), 2: _evaluation(2)}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, ISSUES_TREASURY_UID)] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.0)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(0.0)

    def test_round_total_sums_to_one(self):
        repos = {
            'r/a': _config(emission_share=0.4, issue_discovery_share=0.5),
            'r/b': _config(emission_share=0.3, issue_discovery_share=1.0),
            'r/c': _config(emission_share=0.1, issue_discovery_share=0.0),
        }
        miner_uids = _uids(1, 2, 3)
        evaluations = {
            1: _evaluation(1, prs=[_scored_pr('r/a', 1, earned_score=5.0)]),
            2: _evaluation(2, issues=[_discovered_issue('r/b', 10, earned_score=7.0)]),
            3: _evaluation(3),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert float(rewards.sum()) == pytest.approx(1.0)


class TestRegistrySlack:
    def test_registry_sum_below_one_recycles_shortfall(self):
        repos = {
            'r/a': _config(emission_share=0.4, issue_discovery_share=0.0),
            'r/b': _config(emission_share=0.4, issue_discovery_share=0.0),
        }
        miner_uids = _uids(1, 2)
        evaluations = {
            1: _evaluation(1, prs=[_scored_pr('r/a', 1, earned_score=10.0)]),
            2: _evaluation(2, prs=[_scored_pr('r/b', 2, earned_score=10.0)]),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.4 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(0.4 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(0.2 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, ISSUES_TREASURY_UID)] == pytest.approx(ISSUES_TREASURY_EMISSION_SHARE)
        assert float(rewards.sum()) == pytest.approx(1.0)

    def test_registry_sum_one_no_shortfall_recycle(self):
        repos = {
            'r/a': _config(emission_share=0.5, issue_discovery_share=0.0),
            'r/b': _config(emission_share=0.5, issue_discovery_share=0.0),
        }
        miner_uids = _uids(1)
        evaluations = {
            1: _evaluation(
                1,
                prs=[
                    _scored_pr('r/a', 1, earned_score=10.0),
                    _scored_pr('r/b', 2, earned_score=10.0),
                ],
            )
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(0.0)


class TestIssueDiscoveryShareShortCircuits:
    def test_share_zero_routes_full_slice_to_pr_side(self):
        repos = {'r/pr-only': _config(emission_share=0.2, issue_discovery_share=0.0)}
        miner_uids = _uids(1)
        evaluations = {
            1: _evaluation(
                1,
                prs=[_scored_pr('r/pr-only', 100, earned_score=10.0)],
                issues=[_discovered_issue('r/pr-only', 10, earned_score=99.0)],
            )
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.2 * OSS_EMISSION_SHARE)

    def test_share_zero_with_only_issue_data_recycles_not_spills(self):
        repos = {'r/pr-only': _config(emission_share=0.2, issue_discovery_share=0.0)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, issues=[_discovered_issue('r/pr-only', 10, earned_score=50.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.0)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(OSS_EMISSION_SHARE)

    def test_share_one_routes_full_slice_to_issue_side(self):
        repos = {'r/issue-only': _config(emission_share=0.2, issue_discovery_share=1.0)}
        miner_uids = _uids(1)
        evaluations = {
            1: _evaluation(
                1,
                prs=[_scored_pr('r/issue-only', 100, earned_score=99.0)],
                issues=[_discovered_issue('r/issue-only', 10, earned_score=7.0)],
            )
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.2 * OSS_EMISSION_SHARE)

    def test_share_one_with_only_pr_data_recycles_not_spills(self):
        repos = {'r/issue-only': _config(emission_share=0.2, issue_discovery_share=1.0)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, prs=[_scored_pr('r/issue-only', 100, earned_score=50.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.0)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(OSS_EMISSION_SHARE)


class TestPreservedCompatibility:
    def test_live_registry_load_then_allocate(self):
        repos = load_master_repo_weights()
        miner_uids = _uids()

        rewards = blend_emission_pools({}, repos, miner_uids)

        assert float(rewards.sum()) == pytest.approx(OSS_EMISSION_SHARE + ISSUES_TREASURY_EMISSION_SHARE)


class TestCaseInsensitiveRepoMatching:
    def test_pr_scores_match_lowercase_registry_key_with_mixed_case_live_repo(self):
        repos = {'entrius/gittensor': _config(emission_share=0.2, issue_discovery_share=0.0)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, prs=[_scored_pr('Entrius/Gittensor', 100, earned_score=10.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.2 * OSS_EMISSION_SHARE)

    def test_issue_scores_match_lowercase_registry_key_with_mixed_case_live_repo(self):
        repos = {'entrius/gittensor': _config(emission_share=0.2, issue_discovery_share=1.0)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, issues=[_discovered_issue('Entrius/Gittensor', 10, earned_score=5.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids)

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.2 * OSS_EMISSION_SHARE)


class TestMaintainerCut:
    def test_default_and_empty_map_match_no_arg(self):
        repos = {'r/one': _config(emission_share=0.1, issue_discovery_share=0.0)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, prs=[_scored_pr('r/one', 100, earned_score=10.0)])}

        baseline = blend_emission_pools(evaluations, repos, miner_uids)
        with_none = blend_emission_pools(evaluations, repos, miner_uids, None)
        with_empty = blend_emission_pools(evaluations, repos, miner_uids, {})

        assert list(with_none) == list(baseline)
        assert list(with_empty) == list(baseline)

    def test_zero_cut_ignores_maintainer_map(self):
        repos = {'r/one': _config(emission_share=0.1, issue_discovery_share=0.0, maintainer_cut=0.0)}
        miner_uids = _uids(1, 2)
        evaluations = {
            1: _evaluation(1),
            2: _evaluation(2, prs=[_scored_pr('r/one', 100, earned_score=10.0)]),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids, {'r/one': [1]})

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.0)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(0.1 * OSS_EMISSION_SHARE)

    def test_single_maintainer_gets_full_carve_out(self):
        repos = {'r/one': _config(emission_share=0.1, issue_discovery_share=0.0, maintainer_cut=0.25)}
        miner_uids = _uids(1, 2)
        evaluations = {
            1: _evaluation(1),
            2: _evaluation(2, prs=[_scored_pr('r/one', 100, earned_score=10.0)]),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids, {'r/one': [1]})

        repo_slice = 0.1 * OSS_EMISSION_SHARE
        assert rewards[_idx(miner_uids, 1)] == pytest.approx(repo_slice * 0.25)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(repo_slice * 0.75)

    def test_even_split_among_n_maintainers(self):
        repos = {'r/m': _config(emission_share=0.3, issue_discovery_share=0.0, maintainer_cut=0.4)}
        miner_uids = _uids(1, 2, 3)
        evaluations = {1: _evaluation(1), 2: _evaluation(2), 3: _evaluation(3)}

        rewards = blend_emission_pools(evaluations, repos, miner_uids, {'r/m': [1, 2, 3]})

        per_maintainer = 0.3 * OSS_EMISSION_SHARE * 0.4 / 3
        assert rewards[_idx(miner_uids, 1)] == pytest.approx(per_maintainer)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(per_maintainer)
        assert rewards[_idx(miner_uids, 3)] == pytest.approx(per_maintainer)

    def test_maintainer_also_scores_prs_gets_both(self):
        repos = {'r/one': _config(emission_share=0.1, issue_discovery_share=0.0, maintainer_cut=0.2)}
        miner_uids = _uids(1, 2)
        evaluations = {
            1: _evaluation(1, prs=[_scored_pr('r/one', 1, earned_score=10.0)]),
            2: _evaluation(2, prs=[_scored_pr('r/one', 2, earned_score=10.0)]),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids, {'r/one': [1]})

        repo_slice = 0.1 * OSS_EMISSION_SHARE
        assert rewards[_idx(miner_uids, 1)] == pytest.approx(repo_slice * 0.2 + repo_slice * 0.8 * 0.5)
        assert rewards[_idx(miner_uids, 2)] == pytest.approx(repo_slice * 0.8 * 0.5)

    def test_multi_repo_maintainer_stacks(self):
        repos = {
            'r/a': _config(emission_share=0.2, issue_discovery_share=0.0, maintainer_cut=0.5),
            'r/b': _config(emission_share=0.3, issue_discovery_share=0.0, maintainer_cut=0.5),
        }
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1)}

        rewards = blend_emission_pools(evaluations, repos, miner_uids, {'r/a': [1], 'r/b': [1]})

        slice_a = 0.2 * OSS_EMISSION_SHARE
        slice_b = 0.3 * OSS_EMISSION_SHARE
        assert rewards[_idx(miner_uids, 1)] == pytest.approx(slice_a * 0.5 + slice_b * 0.5)

    def test_no_maintainer_falls_back_to_normal_scoring(self):
        repos = {'r/one': _config(emission_share=0.1, issue_discovery_share=0.0, maintainer_cut=0.5)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1, prs=[_scored_pr('r/one', 100, earned_score=10.0)])}

        rewards = blend_emission_pools(evaluations, repos, miner_uids, {})

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.1 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(0.9 * OSS_EMISSION_SHARE)

    def test_carve_out_plus_recycle_interaction(self):
        repos = {'r/one': _config(emission_share=1.0, issue_discovery_share=0.0, maintainer_cut=0.3)}
        miner_uids = _uids(1)
        evaluations = {1: _evaluation(1)}

        rewards = blend_emission_pools(evaluations, repos, miner_uids, {'r/one': [1]})

        assert rewards[_idx(miner_uids, 1)] == pytest.approx(0.3 * OSS_EMISSION_SHARE)
        assert rewards[_idx(miner_uids, RECYCLE_UID)] == pytest.approx(0.7 * OSS_EMISSION_SHARE)

    def test_round_total_sums_to_one_with_carve_out(self):
        repos = {
            'r/a': _config(emission_share=0.4, issue_discovery_share=0.0, maintainer_cut=0.5),
            'r/b': _config(emission_share=0.3, issue_discovery_share=0.5),
            'r/c': _config(emission_share=0.1, issue_discovery_share=0.0),
        }
        miner_uids = _uids(1, 2, 3)
        evaluations = {
            1: _evaluation(1, prs=[_scored_pr('r/a', 1, earned_score=5.0)]),
            2: _evaluation(2, issues=[_discovered_issue('r/b', 10, earned_score=7.0)]),
            3: _evaluation(3),
        }

        rewards = blend_emission_pools(evaluations, repos, miner_uids, {'r/a': [3]})

        assert float(rewards.sum()) == pytest.approx(1.0)
