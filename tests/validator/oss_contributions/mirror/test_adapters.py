"""Unit tests for mirror → legacy adapters."""

from datetime import datetime, timezone

import pytest

adapters = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.adapters',
    reason='Requires gittensor mirror subpackage',
)
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
classes = pytest.importorskip('gittensor.classes')
scored_pr_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scored_pr')

mirror_files_to_legacy = adapters.mirror_files_to_legacy
mirror_linked_issue_to_legacy_issue = adapters.mirror_linked_issue_to_legacy_issue
mirror_scored_pr_to_legacy_pull_request = adapters.mirror_scored_pr_to_legacy_pull_request
MirrorFile = mirror_models.MirrorFile
MirrorLinkedIssue = mirror_models.MirrorLinkedIssue
MirrorPullRequest = mirror_models.MirrorPullRequest
FileChange = classes.FileChange
Issue = classes.Issue
PullRequest = classes.PullRequest
PRState = classes.PRState
ScoredMirrorPR = scored_pr_module.ScoredMirrorPR


def _mirror_file(**overrides):
    base = {
        'filename': 'src/foo.py',
        'previous_filename': None,
        'status': 'modified',
        'additions': 5,
        'deletions': 2,
        'changes': 7,
        'is_binary': False,
        'byte_size': 100,
        'head_content': 'new',
        'base_content': 'old',
    }
    base.update(overrides)
    return MirrorFile.from_dict(base)


def _linked_issue(**overrides):
    base = {
        'number': 42,
        'title': 'test issue',
        'state': 'CLOSED',
        'state_reason': 'COMPLETED',
        'author_github_id': '123',
        'author_association': 'CONTRIBUTOR',
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


def _make_mirror_pr(
    state: str = 'MERGED',
    pr_number: int = 100,
    repo: str = 'entrius/gittensor-ui',
    body: str = 'PR body',
    additions: int = 5,
    deletions: int = 2,
    commits_count: int = 3,
    head_sha: str = 'abc',
    base_sha: str = 'def',
    maintainer_changes_requested: int = 0,
):
    return MirrorPullRequest.from_dict(
        {
            'repo_full_name': repo,
            'pr_number': pr_number,
            'title': 'A PR title',
            'body': body,
            'state': state,
            'author_github_id': '218712309',
            'author_login': 'bittoby',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-15T00:00:00Z',
            'closed_at': '2026-04-18T10:00:00Z' if state in ('CLOSED', 'MERGED') else None,
            'merged_at': '2026-04-18T10:00:00Z' if state == 'MERGED' else None,
            'last_edited_at': '2026-04-17T00:00:00Z',
            'edited_after_merge': False,
            'hours_since_merge': 1.0 if state == 'MERGED' else None,
            'merged_by_login': 'anderdc' if state == 'MERGED' else None,
            'base_ref': 'main',
            'head_ref': 'feature/foo',
            'head_repo_full_name': repo,
            'default_branch': 'main',
            'head_sha': head_sha,
            'base_sha': base_sha,
            'merge_base_sha': 'mb',
            'additions': additions,
            'deletions': deletions,
            'commits_count': commits_count,
            'scoring_data_stored': True,
            'review_summary': {'maintainer_changes_requested_count': maintainer_changes_requested},
            'labels': [],
            'linked_issues': [],
        }
    )


class TestMirrorScoredPrToLegacyPullRequest:
    def test_full_adapter_field_mapping(self):
        pr = _make_mirror_pr()
        scored = ScoredMirrorPR(pr=pr)
        # Populate scoring fields as if score_mirror_miner_prs already ran
        scored.base_score = 30.5
        scored.earned_score = 25.0
        scored.token_score = 100.0
        scored.repo_weight_multiplier = 0.7
        scored.label_multiplier = 1.5
        scored.label = 'enhancement'
        scored.time_decay_multiplier = 0.95
        scored.review_quality_multiplier = 0.8
        scored.credibility_multiplier = 0.9
        scored.issue_multiplier = 1.33
        scored.open_pr_spam_multiplier = 1.0
        scored.collateral_score = 0.0
        scored.pioneer_dividend = 2.0
        scored.pioneer_rank = 1
        scored.code_density = 0.85
        scored.structural_count = 10
        scored.structural_score = 50.0
        scored.leaf_count = 20
        scored.leaf_score = 50.0
        scored.total_nodes_scored = 30

        adapted = mirror_scored_pr_to_legacy_pull_request(scored, uid=42, hotkey='hk-abc', github_id='218712309')

        assert isinstance(adapted, PullRequest)
        # Field-name remappings
        assert adapted.number == 100  # was pr.pr_number
        assert adapted.repository_full_name == 'entrius/gittensor-ui'  # was pr.repo_full_name
        assert adapted.commits == 3  # was pr.commits_count
        assert adapted.description == 'PR body'  # was pr.body
        assert adapted.head_ref_oid == 'abc'  # was pr.head_sha
        assert adapted.base_ref_oid == 'def'  # was pr.base_sha
        # State string → PRState enum
        assert adapted.pr_state == PRState.MERGED
        # Identity passed in from caller
        assert adapted.uid == 42
        assert adapted.hotkey == 'hk-abc'
        assert adapted.github_id == '218712309'
        # Author + raw response data
        assert adapted.author_login == 'bittoby'
        assert adapted.title == 'A PR title'
        assert adapted.merged_by_login == 'anderdc'
        assert adapted.additions == 5
        assert adapted.deletions == 2
        # Scoring fields all transferred
        assert adapted.base_score == 30.5
        assert adapted.earned_score == 25.0
        assert adapted.token_score == 100.0
        assert adapted.repo_weight_multiplier == 0.7
        assert adapted.label_multiplier == 1.5
        assert adapted.label == 'enhancement'
        assert adapted.time_decay_multiplier == 0.95
        assert adapted.review_quality_multiplier == 0.8
        assert adapted.credibility_multiplier == 0.9
        assert adapted.issue_multiplier == 1.33
        assert adapted.open_pr_spam_multiplier == 1.0
        assert adapted.pioneer_dividend == 2.0
        assert adapted.pioneer_rank == 1
        assert adapted.total_nodes_scored == 30
        assert adapted.code_density == 0.85
        # file_changes / issues left None — written separately
        assert adapted.file_changes is None
        assert adapted.issues is None

    def test_open_pr_state_translation(self):
        pr = _make_mirror_pr(state='OPEN')
        scored = ScoredMirrorPR(pr=pr)
        adapted = mirror_scored_pr_to_legacy_pull_request(scored, 1, 'hk', 'gid')
        assert adapted.pr_state == PRState.OPEN
        assert adapted.merged_at is None
        assert adapted.merged_by_login is None

    def test_closed_pr_state_translation(self):
        pr = _make_mirror_pr(state='CLOSED')
        scored = ScoredMirrorPR(pr=pr)
        adapted = mirror_scored_pr_to_legacy_pull_request(scored, 1, 'hk', 'gid')
        assert adapted.pr_state == PRState.CLOSED

    def test_changes_requested_count_from_review_summary(self):
        pr = _make_mirror_pr(maintainer_changes_requested=4)
        scored = ScoredMirrorPR(pr=pr)
        adapted = mirror_scored_pr_to_legacy_pull_request(scored, 1, 'hk', 'gid')
        # Mirror's maintainer-only count is what scoring uses; same field on PullRequest
        assert adapted.changes_requested_count == 4

    def test_default_scoring_fields_when_pr_not_yet_scored(self):
        """ScoredMirrorPR with default (unscored) fields → adapted PR has neutral
        multipliers and zero scores."""
        pr = _make_mirror_pr(state='OPEN')
        scored = ScoredMirrorPR(pr=pr)
        # Don't set any scoring fields
        adapted = mirror_scored_pr_to_legacy_pull_request(scored, 1, 'hk', 'gid')
        assert adapted.base_score == 0.0
        assert adapted.earned_score == 0.0
        assert adapted.repo_weight_multiplier == 1.0
        assert adapted.pioneer_rank == 0
        assert adapted.label is None
