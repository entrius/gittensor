"""Unit tests for mirror → legacy adapters."""

from datetime import datetime, timezone

import pytest

adapters = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.adapters',
    reason='Requires gittensor mirror subpackage',
)
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
classes = pytest.importorskip('gittensor.classes')

mirror_files_to_legacy = adapters.mirror_files_to_legacy
mirror_linked_issue_to_legacy_issue = adapters.mirror_linked_issue_to_legacy_issue
MirrorFile = mirror_models.MirrorFile
MirrorLinkedIssue = mirror_models.MirrorLinkedIssue
FileChange = classes.FileChange
Issue = classes.Issue


def _mirror_file(**overrides):
    base = {
        'filename': 'src/foo.py', 'previous_filename': None,
        'status': 'modified',
        'additions': 5, 'deletions': 2, 'changes': 7,
        'is_binary': False, 'byte_size': 100,
        'head_content': 'new', 'base_content': 'old',
    }
    base.update(overrides)
    return MirrorFile.from_dict(base)


def _linked_issue(**overrides):
    base = {
        'number': 42, 'title': 'test issue',
        'state': 'CLOSED', 'state_reason': 'COMPLETED',
        'author_github_id': '123', 'author_association': 'CONTRIBUTOR',
        'created_at': '2026-04-01T00:00:00Z',
        'closed_at': '2026-04-10T00:00:00Z',
        'updated_at': '2026-04-10T00:00:00Z',
        'is_transferred': False,
        'solved_by_pr': 100,
        'labels': [],
    }
    base.update(overrides)
    return MirrorLinkedIssue.from_dict(base)


class TestMirrorFilesToLegacy:
    def test_returns_file_changes_and_contents(self):
        files = [_mirror_file()]
        fcs, contents = mirror_files_to_legacy('owner/repo', 42, files)
        assert len(fcs) == 1
        assert isinstance(fcs[0], FileChange)
        assert contents['src/foo.py'].old_content == 'old'
        assert contents['src/foo.py'].new_content == 'new'

    def test_empty_input(self):
        fcs, contents = mirror_files_to_legacy('owner/repo', 42, [])
        assert fcs == []
        assert contents == {}


class TestMirrorLinkedIssueToLegacy:
    def test_full_adapter(self):
        li = _linked_issue()
        issue = mirror_linked_issue_to_legacy_issue(li, pr_number=100, repo_full_name='entrius/gittensor-ui')

        assert isinstance(issue, Issue)
        assert issue.number == 42
        assert issue.pr_number == 100
        assert issue.repository_full_name == 'entrius/gittensor-ui'
        assert issue.title == 'test issue'
        assert issue.state == 'CLOSED'
        assert issue.state_reason == 'COMPLETED'
        assert issue.author_github_id == '123'
        assert issue.author_association == 'CONTRIBUTOR'
        # mirror doesn't carry author_login on linked issues
        assert issue.author_login is None
        # mirror's updated_at is noisy; no precise edit timestamp
        assert issue.body_or_title_edited_at is None

    def test_datetimes_preserved_as_aware_utc(self):
        li = _linked_issue()
        issue = mirror_linked_issue_to_legacy_issue(li, 100, 'o/r')
        assert issue.created_at.tzinfo is not None
        assert issue.created_at == datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        assert issue.closed_at == datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)

    def test_nullable_state_reason(self):
        li = _linked_issue(state_reason=None, state='OPEN', closed_at=None)
        issue = mirror_linked_issue_to_legacy_issue(li, 100, 'o/r')
        assert issue.state_reason is None
        assert issue.state == 'OPEN'
