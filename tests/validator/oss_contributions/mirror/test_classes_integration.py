"""Verify MinerEvaluation helpers (get_all_*) walk mirror data through adapters.

Also tests that pickle round-trips an old-shape MinerEvaluation into the new
shape with mirror_* fields defaulting cleanly — guards against breaking the
cache fallback when the dataclass grows new fields.
"""

import pickle

import pytest

classes = pytest.importorskip('gittensor.classes')
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
scored_pr_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scored_pr')

MinerEvaluation = classes.MinerEvaluation
FileChange = classes.FileChange
Issue = classes.Issue
MirrorPullRequest = mirror_models.MirrorPullRequest
MirrorFile = mirror_models.MirrorFile
ScoredPR = scored_pr_module.ScoredPR


def _mirror_pr_with_files_and_issue():
    pr = MirrorPullRequest.from_dict(
        {
            'repo_full_name': 'entrius/gittensor-ui',
            'pr_number': 100,
            'title': 't',
            'body': 'b',
            'state': 'MERGED',
            'author_github_id': '218712309',
            'author_login': 'a',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-15T00:00:00Z',
            'closed_at': '2026-04-18T10:00:00Z',
            'merged_at': '2026-04-18T10:00:00Z',
            'last_edited_at': None,
            'edited_after_merge': False,
            'hours_since_merge': 1.0,
            'merged_by_login': 'm',
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
            'linked_issues': [
                {
                    'number': 50,
                    'title': 'bug',
                    'state': 'CLOSED',
                    'state_reason': 'COMPLETED',
                    'author_github_id': '999',
                    'author_association': 'CONTRIBUTOR',
                    'created_at': '2026-04-01T00:00:00Z',
                    'closed_at': '2026-04-18T10:00:00Z',
                    'updated_at': '2026-04-18T10:00:00Z',
                    'is_transferred': False,
                    'solved_by_pr': 100,
                    'labels': [],
                }
            ],
        }
    )
    scored = ScoredPR(pr=pr)
    scored.files = [
        MirrorFile.from_dict(
            {
                'filename': 'src/x.py',
                'previous_filename': None,
                'status': 'modified',
                'additions': 1,
                'deletions': 1,
                'changes': 2,
                'is_binary': False,
                'byte_size': 100,
                'head_content': 'new',
                'base_content': 'old',
            }
        ),
    ]
    return scored


class TestGetAllIssuesWalksMirror:
    def test_mirror_linked_issues_adapted_into_legacy_issue_list(self):
        eval_ = MinerEvaluation(uid=1, hotkey='hk')
        eval_.merged_prs = [_mirror_pr_with_files_and_issue()]

        issues = eval_.get_all_issues()
        assert len(issues) == 1
        issue = issues[0]
        assert isinstance(issue, Issue)
        assert issue.number == 50
        assert issue.pr_number == 100
        assert issue.repository_full_name == 'entrius/gittensor-ui'
        assert issue.state_reason == 'COMPLETED'


class TestGetAllFileChangesWalksMirror:
    def test_mirror_files_adapted_into_legacy_file_change_list(self):
        eval_ = MinerEvaluation(uid=1, hotkey='hk')
        eval_.merged_prs = [_mirror_pr_with_files_and_issue()]

        file_changes = eval_.get_all_file_changes()
        assert len(file_changes) == 1
        fc = file_changes[0]
        assert isinstance(fc, FileChange)
        assert fc.filename == 'src/x.py'
        assert fc.pr_number == 100
        assert fc.repository_full_name == 'entrius/gittensor-ui'

    def test_mirror_pr_without_fetched_files_is_skipped(self):
        eval_ = MinerEvaluation(uid=1, hotkey='hk')
        scored = _mirror_pr_with_files_and_issue()
        scored.files = None  # not fetched
        eval_.merged_prs = [scored]

        file_changes = eval_.get_all_file_changes()
        assert file_changes == []


class TestCacheSerdeCompat:
    def test_pickle_roundtrip_populates_mirror_defaults(self):
        """Pickled old-shape MinerEvaluation without mirror_* fields must unpickle
        cleanly and default the mirror lists to empty."""
        eval_ = MinerEvaluation(uid=1, hotkey='hk', github_id='123')
        eval_.total_score = 42.0

        dumped = pickle.dumps(eval_)
        restored = pickle.loads(dumped)

        assert restored.uid == 1
        assert restored.total_score == 42.0
        assert restored.merged_prs == []
        assert restored.open_prs == []
        assert restored.closed_prs == []

    def test_roundtrip_preserves_populated_mirror_prs(self):
        eval_ = MinerEvaluation(uid=1, hotkey='hk', github_id='123')
        eval_.merged_prs = [_mirror_pr_with_files_and_issue()]

        dumped = pickle.dumps(eval_)
        restored = pickle.loads(dumped)

        assert len(restored.merged_prs) == 1
        assert restored.merged_prs[0].pr.pr_number == 100
