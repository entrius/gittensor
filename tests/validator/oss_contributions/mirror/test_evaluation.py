"""Unit tests for MirrorMinerEvaluation.

Covers field defaults and the per-state count properties (which mirror the
ones on MinerEvaluation but only see this container's lists).
"""

import pytest

evaluation_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.evaluation',
    reason='Requires gittensor mirror subpackage',
)
scored_pr_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.scored_pr',
    reason='Requires gittensor mirror subpackage',
)
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')

MirrorMinerEvaluation = evaluation_module.MirrorMinerEvaluation
ScoredMirrorPR = scored_pr_module.ScoredMirrorPR
MirrorPullRequest = mirror_models.MirrorPullRequest


def _make_scored_pr(state: str = 'MERGED') -> ScoredMirrorPR:
    pr = MirrorPullRequest.from_dict({
        'repo_full_name': 'entrius/gittensor-ui',
        'pr_number': 1,
        'title': 't', 'body': 'b',
        'state': state,
        'author_github_id': '1', 'author_login': 'a',
        'author_association': 'CONTRIBUTOR',
        'created_at': '2026-04-01T00:00:00Z',
        'closed_at': None, 'merged_at': None, 'last_edited_at': None,
        'edited_after_merge': False,
        'hours_since_merge': None,
        'merged_by_login': None,
        'base_ref': 'test',
        'head_sha': 'h', 'base_sha': 'b', 'merge_base_sha': 'mb',
        'additions': 1, 'deletions': 0, 'commits_count': 1,
        'scoring_data_stored': True,
        'review_summary': {'maintainer_changes_requested_count': 0},
        'labels': [], 'linked_issues': [],
    })
    return ScoredMirrorPR(pr=pr)


class TestDefaults:
    def test_required_fields(self):
        eval_ = MirrorMinerEvaluation(uid=42, hotkey='abc')
        assert eval_.uid == 42
        assert eval_.hotkey == 'abc'
        assert eval_.github_id is None

    def test_lists_start_empty(self):
        eval_ = MirrorMinerEvaluation(uid=1, hotkey='hk')
        assert eval_.merged_prs == []
        assert eval_.open_prs == []
        assert eval_.closed_prs == []

    def test_aggregate_counters_start_zero(self):
        eval_ = MirrorMinerEvaluation(uid=1, hotkey='hk')
        assert eval_.total_token_score == 0.0
        assert eval_.total_nodes_scored == 0
        assert eval_.total_structural_count == 0
        assert eval_.total_structural_score == 0.0
        assert eval_.total_leaf_count == 0
        assert eval_.total_leaf_score == 0.0
        assert eval_.total_collateral_score == 0.0

    def test_unique_repos_starts_empty(self):
        eval_ = MirrorMinerEvaluation(uid=1, hotkey='hk')
        assert eval_.unique_repos_contributed_to == set()

    def test_fetch_failed_default_false(self):
        eval_ = MirrorMinerEvaluation(uid=1, hotkey='hk')
        assert eval_.fetch_failed is False


class TestCountProperties:
    def test_empty_counts(self):
        eval_ = MirrorMinerEvaluation(uid=1, hotkey='hk')
        assert eval_.total_merged_prs == 0
        assert eval_.total_open_prs == 0
        assert eval_.total_closed_prs == 0

    def test_populated_counts(self):
        eval_ = MirrorMinerEvaluation(uid=1, hotkey='hk')
        eval_.merged_prs.extend([_make_scored_pr() for _ in range(3)])
        eval_.open_prs.append(_make_scored_pr(state='OPEN'))
        eval_.closed_prs.extend([_make_scored_pr(state='CLOSED') for _ in range(2)])

        assert eval_.total_merged_prs == 3
        assert eval_.total_open_prs == 1
        assert eval_.total_closed_prs == 2
