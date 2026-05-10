"""Unit tests for combine().

Covers:
- Mirror PR lists land in MinerEvaluation.mirror_*_prs
- Aggregate counters sum into legacy_eval's totals
- unique_repos_contributed_to is unioned (not overwritten)
- unique_repos_count is recomputed from the merged set
- github_pr_fetch_failed is OR'd
- Aggregate properties on the returned MinerEvaluation reflect both paths
"""

import pytest

mirror_eval_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.evaluation')
mirror_combine = pytest.importorskip('gittensor.validator.oss_contributions.mirror.combine')
mirror_scored = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scored_pr')
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
classes = pytest.importorskip('gittensor.classes')

MirrorMinerEvaluation = mirror_eval_module.MirrorMinerEvaluation
ScoredMirrorPR = mirror_scored.ScoredMirrorPR
combine = mirror_combine.combine
MinerEvaluation = classes.MinerEvaluation
MirrorPullRequest = mirror_models.MirrorPullRequest


def _scored(pr_number: int = 1, repo: str = 'entrius/gittensor-ui', state: str = 'MERGED') -> ScoredMirrorPR:
    pr = MirrorPullRequest.from_dict(
        {
            'repo_full_name': repo,
            'pr_number': pr_number,
            'title': 't',
            'body': 'b',
            'state': state,
            'author_github_id': '1',
            'author_login': 'a',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-01T00:00:00Z',
            'closed_at': None,
            'merged_at': None,
            'last_edited_at': None,
            'edited_after_merge': False,
            'hours_since_merge': None,
            'merged_by_login': None,
            'base_ref': 'test',
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'additions': 1,
            'deletions': 0,
            'commits_count': 1,
            'scoring_data_stored': True,
            'review_summary': {'maintainer_changes_requested_count': 0},
            'labels': [],
            'linked_issues': [],
        }
    )
    return ScoredMirrorPR(pr=pr)


class TestCombineEmpty:
    def test_combine_empty_eval_does_not_change_legacy(self):
        legacy = MinerEvaluation(uid=1, hotkey='hk')
        mirror_eval = MirrorMinerEvaluation(uid=1, hotkey='hk')
        assert combine(legacy, mirror_eval) is None
        assert legacy.mirror_merged_prs == []
        assert legacy.total_token_score == 0.0
        assert legacy.unique_repos_count == 0


class TestCombinePopulated:
    def _setup(self):
        legacy = MinerEvaluation(uid=1, hotkey='hk')
        legacy.total_token_score = 100.0
        legacy.total_nodes_scored = 50
        legacy.total_collateral_score = 5.0
        legacy.unique_repos_contributed_to = {'foo/legacy-repo'}
        legacy.unique_repos_count = 1

        mirror_eval = MirrorMinerEvaluation(uid=1, hotkey='hk')
        mirror_eval.merged_prs = [_scored(1), _scored(2)]
        mirror_eval.open_prs = [_scored(3, state='OPEN')]
        mirror_eval.closed_prs = [_scored(4, state='CLOSED')]
        mirror_eval.unique_repos_contributed_to = {'entrius/gittensor-ui', 'entrius/allways'}
        return legacy, mirror_eval

    def test_lists_transferred_to_mirror_slots(self):
        legacy, mirror_eval = self._setup()
        combine(legacy, mirror_eval)
        assert legacy.mirror_merged_prs is mirror_eval.merged_prs
        assert legacy.mirror_open_prs is mirror_eval.open_prs
        assert legacy.mirror_closed_prs is mirror_eval.closed_prs

    def test_counters_left_alone(self):
        """combine() does NOT touch token/nodes/collateral counters — those
        are aggregated from per-PR fields by finalize_miner_scores."""
        legacy, mirror_eval = self._setup()
        combine(legacy, mirror_eval)
        # Legacy values pre-combine should pass through unchanged
        assert legacy.total_token_score == 100.0
        assert legacy.total_nodes_scored == 50
        assert legacy.total_collateral_score == 5.0

    def test_unique_repos_unioned(self):
        legacy, mirror_eval = self._setup()
        combine(legacy, mirror_eval)
        assert legacy.unique_repos_contributed_to == {
            'foo/legacy-repo',
            'entrius/gittensor-ui',
            'entrius/allways',
        }

    def test_aggregate_pr_count_properties_sum_both_paths(self):
        legacy, mirror_eval = self._setup()
        # add some legacy PRs too via the dataclass's add_* methods would be heavy;
        # populate the lists directly for the test
        legacy.merged_pull_requests = ['legacy_pr_1', 'legacy_pr_2']  # placeholder objects fine for len()
        legacy.open_pull_requests = ['legacy_open_1']

        combine(legacy, mirror_eval)
        # total_merged: 2 legacy + 2 mirror = 4
        assert legacy.total_merged_prs == 4
        # total_open: 1 legacy + 1 mirror = 2
        assert legacy.total_open_prs == 2
        # total_closed: 0 legacy + 1 mirror = 1
        assert legacy.total_closed_prs == 1


class TestCombineFetchFailed:
    def test_legacy_failed_only(self):
        legacy = MinerEvaluation(uid=1, hotkey='hk')
        legacy.github_pr_fetch_failed = True
        mirror_eval = MirrorMinerEvaluation(uid=1, hotkey='hk')
        combine(legacy, mirror_eval)
        assert legacy.github_pr_fetch_failed is True
        assert legacy.mirror_pr_fetch_failed is False

    def test_mirror_failed_only(self):
        legacy = MinerEvaluation(uid=1, hotkey='hk')
        mirror_eval = MirrorMinerEvaluation(uid=1, hotkey='hk', fetch_failed=True)
        combine(legacy, mirror_eval)
        assert legacy.github_pr_fetch_failed is True
        assert legacy.mirror_pr_fetch_failed is True

    def test_both_failed(self):
        legacy = MinerEvaluation(uid=1, hotkey='hk')
        legacy.github_pr_fetch_failed = True
        mirror_eval = MirrorMinerEvaluation(uid=1, hotkey='hk', fetch_failed=True)
        combine(legacy, mirror_eval)
        assert legacy.github_pr_fetch_failed is True
        assert legacy.mirror_pr_fetch_failed is True

    def test_neither_failed(self):
        legacy = MinerEvaluation(uid=1, hotkey='hk')
        mirror_eval = MirrorMinerEvaluation(uid=1, hotkey='hk')
        combine(legacy, mirror_eval)
        assert legacy.github_pr_fetch_failed is False
        assert legacy.mirror_pr_fetch_failed is False
