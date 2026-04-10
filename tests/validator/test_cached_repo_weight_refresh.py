# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for refreshing stale repo weights on cached evaluations.

When a miner evaluation is restored from cache (GitHub API returned no PRs),
the repo_weight_multiplier on each PR may be stale if master_repositories.json
was updated since the evaluation was originally scored. The _refresh_cached_repo_weights
function ensures cached evaluations use the current repository weights.

Verifies fix for: https://github.com/entrius/gittensor/issues/364

Run tests:
    pytest tests/validator/test_cached_repo_weight_refresh.py -v
"""

import pytest

from gittensor.classes import MinerEvaluation, PRState
from gittensor.validator.oss_contributions.reward import _refresh_cached_repo_weights
from gittensor.validator.utils.load_weights import RepositoryConfig
from tests.validator.conftest import PRBuilder


@pytest.fixture
def builder():
    return PRBuilder()


def _make_eval(uid, merged=None, open_prs=None, closed=None):
    """Helper to create a MinerEvaluation with given PR lists."""
    eval_ = MinerEvaluation(uid=uid, hotkey=f'hotkey_{uid}', github_id=str(uid))
    eval_.merged_pull_requests = merged or []
    eval_.open_pull_requests = open_prs or []
    eval_.closed_pull_requests = closed or []
    return eval_


class TestRefreshCachedRepoWeights:
    """Tests for _refresh_cached_repo_weights."""

    def test_stale_weight_is_updated_on_merged_pr(self, builder):
        """Merged PR with stale repo weight should be updated to current value."""
        pr = builder.create(state=PRState.MERGED, uid=1, repo='owner/repo-a')
        pr.repo_weight_multiplier = 0.05  # stale value

        evaluations = {1: _make_eval(1, merged=[pr])}
        cached_uids = {1}
        repos = {'owner/repo-a': RepositoryConfig(weight=0.20)}

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr.repo_weight_multiplier == 0.20

    def test_stale_weight_is_updated_on_open_pr(self, builder):
        """Open PR with stale repo weight should be updated to current value."""
        pr = builder.create(state=PRState.OPEN, uid=2, repo='owner/repo-b')
        pr.repo_weight_multiplier = 0.03

        evaluations = {2: _make_eval(2, open_prs=[pr])}
        cached_uids = {2}
        repos = {'owner/repo-b': RepositoryConfig(weight=0.15)}

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr.repo_weight_multiplier == 0.15

    def test_stale_weight_is_updated_on_closed_pr(self, builder):
        """Closed PR with stale repo weight should be updated to current value."""
        pr = builder.create(state=PRState.CLOSED, uid=3, repo='owner/repo-c')
        pr.repo_weight_multiplier = 0.10

        evaluations = {3: _make_eval(3, closed=[pr])}
        cached_uids = {3}
        repos = {'owner/repo-c': RepositoryConfig(weight=0.50)}

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr.repo_weight_multiplier == 0.50

    def test_non_cached_uids_are_not_modified(self, builder):
        """PRs from non-cached (freshly scored) evaluations should be untouched."""
        pr = builder.create(state=PRState.MERGED, uid=10, repo='owner/repo-a')
        pr.repo_weight_multiplier = 0.05  # this is the freshly scored value

        evaluations = {10: _make_eval(10, merged=[pr])}
        cached_uids = set()  # uid 10 is NOT cached
        repos = {'owner/repo-a': RepositoryConfig(weight=0.99)}

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr.repo_weight_multiplier == 0.05  # unchanged

    def test_repo_removed_from_master_falls_back_to_default(self, builder):
        """If a repo is no longer in master_repositories, weight should default to 0.01."""
        pr = builder.create(state=PRState.MERGED, uid=4, repo='removed/repo')
        pr.repo_weight_multiplier = 0.50  # old weight from when repo was active

        evaluations = {4: _make_eval(4, merged=[pr])}
        cached_uids = {4}
        repos = {}  # repo no longer in master list

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr.repo_weight_multiplier == 0.01

    def test_multiple_prs_across_repos_all_updated(self, builder):
        """All PRs in a cached evaluation should have their weights refreshed."""
        pr1 = builder.create(state=PRState.MERGED, uid=5, repo='org/alpha')
        pr1.repo_weight_multiplier = 0.10
        pr2 = builder.create(state=PRState.MERGED, uid=5, repo='org/beta')
        pr2.repo_weight_multiplier = 0.20
        pr3 = builder.create(state=PRState.OPEN, uid=5, repo='org/alpha')
        pr3.repo_weight_multiplier = 0.10

        evaluations = {5: _make_eval(5, merged=[pr1, pr2], open_prs=[pr3])}
        cached_uids = {5}
        repos = {
            'org/alpha': RepositoryConfig(weight=0.30),
            'org/beta': RepositoryConfig(weight=0.40),
        }

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr1.repo_weight_multiplier == 0.30
        assert pr2.repo_weight_multiplier == 0.40
        assert pr3.repo_weight_multiplier == 0.30

    def test_weight_is_rounded_to_two_decimals(self, builder):
        """Refreshed weight should be rounded to 2 decimal places (matches scoring.py behavior)."""
        pr = builder.create(state=PRState.MERGED, uid=6, repo='org/precise')
        pr.repo_weight_multiplier = 0.10

        evaluations = {6: _make_eval(6, merged=[pr])}
        cached_uids = {6}
        repos = {'org/precise': RepositoryConfig(weight=0.12345)}

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr.repo_weight_multiplier == 0.12

    def test_empty_cached_uids_is_noop(self, builder):
        """No-op when there are no cached UIDs."""
        pr = builder.create(state=PRState.MERGED, uid=7, repo='org/repo')
        pr.repo_weight_multiplier = 0.05

        evaluations = {7: _make_eval(7, merged=[pr])}
        cached_uids = set()
        repos = {'org/repo': RepositoryConfig(weight=0.99)}

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr.repo_weight_multiplier == 0.05

    def test_mixed_cached_and_fresh_evaluations(self, builder):
        """Only cached UIDs get refreshed weights; fresh UIDs are untouched."""
        cached_pr = builder.create(state=PRState.MERGED, uid=8, repo='org/repo')
        cached_pr.repo_weight_multiplier = 0.05  # stale

        fresh_pr = builder.create(state=PRState.MERGED, uid=9, repo='org/repo')
        fresh_pr.repo_weight_multiplier = 0.25  # freshly scored

        evaluations = {
            8: _make_eval(8, merged=[cached_pr]),
            9: _make_eval(9, merged=[fresh_pr]),
        }
        cached_uids = {8}  # only uid 8 is cached
        repos = {'org/repo': RepositoryConfig(weight=0.25)}

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert cached_pr.repo_weight_multiplier == 0.25  # refreshed
        assert fresh_pr.repo_weight_multiplier == 0.25  # unchanged (was already correct)

    def test_unchanged_weight_stays_same(self, builder):
        """If repo weight hasn't changed, the multiplier stays the same."""
        pr = builder.create(state=PRState.MERGED, uid=11, repo='org/stable')
        pr.repo_weight_multiplier = 0.15

        evaluations = {11: _make_eval(11, merged=[pr])}
        cached_uids = {11}
        repos = {'org/stable': RepositoryConfig(weight=0.15)}

        _refresh_cached_repo_weights(evaluations, cached_uids, repos)

        assert pr.repo_weight_multiplier == 0.15


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
