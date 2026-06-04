"""Tests for _roll_up_miner_totals: credibility must use weighted ratio, not max().

Issue #1438: using max() across per-repo credibility values lets a miner with
one perfect micro-repo report round-level credibility of 1.0 regardless of how
many PRs were rejected across their other repos.
"""

from __future__ import annotations

import pytest

scoring_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.scoring',
    reason='Requires gittensor oss_contributions subpackage',
)
classes = pytest.importorskip('gittensor.classes')

_roll_up_miner_totals = scoring_module._roll_up_miner_totals
MinerEvaluation = classes.MinerEvaluation
RepoEvaluation = classes.RepoEvaluation


def _repo_eval(repo: str, merged: int, closed: int) -> RepoEvaluation:
    re = RepoEvaluation(repository_full_name=repo)
    re.total_merged_prs = merged
    re.total_closed_prs = closed
    return re


class TestRollUpMinerTotalsCredibility:
    """_roll_up_miner_totals: credibility is weighted ratio across all repos, not max()."""

    def test_multi_repo_weighted_ratio(self):
        """One perfect micro-repo must not inflate credibility to 1.0."""
        ev = MinerEvaluation(uid=1, hotkey='hk')
        ev.repo_evaluations = {
            'a/a': _repo_eval('a/a', merged=1, closed=0),   # per-repo 1.0
            'b/b': _repo_eval('b/b', merged=2, closed=8),   # per-repo 0.20
            'c/c': _repo_eval('c/c', merged=3, closed=12),  # per-repo 0.20
        }
        _roll_up_miner_totals(ev)
        # true weighted = 6 / (6+20) ≈ 0.23  (old max() would give 1.0)
        assert ev.credibility == pytest.approx(6 / 26)
        assert ev.credibility < 0.5

    def test_zero_attempts_yields_zero(self):
        ev = MinerEvaluation(uid=1, hotkey='hk')
        ev.repo_evaluations = {'a/a': _repo_eval('a/a', merged=0, closed=0)}
        _roll_up_miner_totals(ev)
        assert ev.credibility == 0.0

    def test_single_repo_matches_ratio(self):
        ev = MinerEvaluation(uid=1, hotkey='hk')
        ev.repo_evaluations = {'a/a': _repo_eval('a/a', merged=8, closed=2)}
        _roll_up_miner_totals(ev)
        assert ev.credibility == pytest.approx(0.8)

    def test_all_merged_yields_one(self):
        ev = MinerEvaluation(uid=1, hotkey='hk')
        ev.repo_evaluations = {
            'a/a': _repo_eval('a/a', merged=5, closed=0),
            'b/b': _repo_eval('b/b', merged=3, closed=0),
        }
        _roll_up_miner_totals(ev)
        assert ev.credibility == pytest.approx(1.0)
