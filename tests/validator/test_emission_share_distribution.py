"""Tests for per-repo emission_share distribution in blend_emission_pools (#1215)."""

from types import SimpleNamespace

import numpy as np
import pytest

from gittensor.classes import MinerEvaluation
from gittensor.validator.forward import blend_emission_pools
from gittensor.validator.utils.load_weights import RepositoryConfig


def _config(share: float) -> RepositoryConfig:
    return RepositoryConfig(emission_share=share)


def _scored_pr(repo: str, score: float):
    scored = SimpleNamespace()
    scored.pr = SimpleNamespace(repo_full_name=repo)
    scored.earned_score = score
    return scored


def _eval(uid: int, repo_scores: dict) -> MinerEvaluation:
    eval_ = MinerEvaluation(uid, 'dev')
    setattr(eval_, 'merged_prs', [_scored_pr(repo, score) for repo, score in repo_scores.items()])
    return eval_


class TestPerRepoEmissionShare:
    def test_flat_distribution_when_no_repo_data(self):
        """Without repo data, blend_emission_pools uses flat combined distribution."""
        oss = np.array([0.2, 0.3])
        issue = np.array([0.1, 0.0])
        result = blend_emission_pools(oss, issue, {1, 2})
        assert len(result) == 2
        assert result[0] > 0 and result[1] > 0

    def test_per_repo_distribution(self):
        """Repo emission_share controls per-repo allocation."""
        repos = {'repo/a': _config(0.3), 'repo/b': _config(0.2)}
        miner_uids = {1, 2}
        oss = np.array([0.5, 0.5])
        issue = np.array([0.0, 0.0])

        evaluations = {
            1: _eval(1, {'repo/a': 100.0}),
            2: _eval(2, {'repo/b': 100.0}),
        }

        result = blend_emission_pools(oss, issue, miner_uids, miner_evaluations=evaluations, master_repositories=repos)

        total_pool = 1.0 * 0.9  # oss_pool_share=0.9
        repo_a_share = (0.3 / 0.5) * total_pool
        repo_b_share = (0.2 / 0.5) * total_pool

        uid1_idx = sorted(miner_uids).index(1)
        uid2_idx = sorted(miner_uids).index(2)

        assert result[uid1_idx] == pytest.approx(repo_a_share, abs=1e-6)
        assert result[uid2_idx] == pytest.approx(repo_b_share, abs=1e-6)

    def test_multiple_miners_same_repo(self):
        """Miners in the same repo split the repo's allocation proportionally by score."""
        repos = {'repo/a': _config(0.5)}
        miner_uids = {1, 2}
        oss = np.array([0.5, 0.5])
        issue = np.array([0.0, 0.0])

        evaluations = {
            1: _eval(1, {'repo/a': 75.0}),
            2: _eval(2, {'repo/a': 25.0}),
        }

        result = blend_emission_pools(oss, issue, miner_uids, miner_evaluations=evaluations, master_repositories=repos)

        total_pool = 1.0 * 0.9
        repo_share = total_pool  # emission_share=0.5 / total=0.5 = 1.0 → 100% of pool

        uid1_idx = sorted(miner_uids).index(1)
        uid2_idx = sorted(miner_uids).index(2)

        assert result[uid1_idx] == pytest.approx(repo_share * 0.75, abs=1e-6)
        assert result[uid2_idx] == pytest.approx(repo_share * 0.25, abs=1e-6)


class TestWithinRepoSpill:
    def test_issue_side_empty_spills_to_pr_side(self):
        """When a repo has PR scorers but no issue scorers, full slice goes to PR side."""
        repos = {'repo/a': _config(0.3)}
        miner_uids = {1}
        oss = np.array([1.0])
        issue = np.array([0.0])

        evaluations = {1: _eval(1, {'repo/a': 100.0})}

        result = blend_emission_pools(oss, issue, miner_uids, miner_evaluations=evaluations, master_repositories=repos)
        idx = sorted(miner_uids).index(1)
        expected = 0.9  # pool = 1.0, oss_pool_share = 0.9, all to UID 1
        assert result[idx] == pytest.approx(expected, abs=1e-6)

    def test_both_sides_empty_recycles(self):
        """When a repo has no scorers on either side, its allocation recycles."""
        repos = {'repo/a': _config(0.5)}
        miner_uids = {1}
        oss = np.array([1.0])
        issue = np.array([0.0])

        # Miner evaluation with no merged PRs and no issue discovery score
        eval_ = MinerEvaluation(1, 'dev')
        setattr(eval_, 'merged_prs', [])

        result = blend_emission_pools(oss, issue, miner_uids, miner_evaluations={1: eval_}, master_repositories=repos)
        idx = sorted(miner_uids).index(1)
        assert result[idx] == 0.0
