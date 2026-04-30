#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for gittensor.utils.mirror.models.

Covers from_dict parsing for each response dataclass:
- Happy-path field mapping
- Nullable fields (None for missing / backfilled actor attribution)
- Timestamps normalized to timezone-aware UTC
- Nested shapes (labels, linked_issues, solving_pr)
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import msgspec
import pytest

models = pytest.importorskip('gittensor.utils.mirror.models', reason='Requires gittensor package')


MirrorFile = models.MirrorFile
MirrorIssue = models.MirrorIssue
MirrorIssuesResponse = models.MirrorIssuesResponse
MirrorLabel = models.MirrorLabel
MirrorLinkedIssue = models.MirrorLinkedIssue
MirrorPullRequest = models.MirrorPullRequest
MirrorPullRequestFilesResponse = models.MirrorPullRequestFilesResponse
MirrorPullRequestsResponse = models.MirrorPullRequestsResponse
MirrorReviewSummary = models.MirrorReviewSummary
MirrorSolvingPR = models.MirrorSolvingPR


# ============================================================================
# Fixtures — representative payloads based on real mirror responses
# ============================================================================


@pytest.fixture
def label_dict():
    return {
        'name': 'refactor',
        'actor_github_id': '61125407',
        'actor_association': 'COLLABORATOR',
    }


@pytest.fixture
def label_dict_backfilled():
    """Labels applied before the mirror started tracking actors come back null."""
    return {'name': 'legacy-label', 'actor_github_id': None, 'actor_association': None}


@pytest.fixture
def review_summary_dict():
    return {
        'maintainer_changes_requested_count': 2,
        'changes_requested_count': 3,
        'approved_count': 1,
        'commented_count': 0,
    }


@pytest.fixture
def linked_issue_dict():
    return {
        'number': 553,
        'title': 'Some issue',
        'state': 'CLOSED',
        'state_reason': 'COMPLETED',
        'author_github_id': '170233626',
        'author_association': 'CONTRIBUTOR',
        'created_at': '2026-03-20T00:00:00Z',
        'closed_at': '2026-04-18T10:00:00Z',
        'updated_at': '2026-04-18T10:00:00+00:00',
        'is_transferred': False,
        'solved_by_pr': 518,
        'labels': [{'name': 'bug', 'actor_github_id': None, 'actor_association': None}],
    }


@pytest.fixture
def pull_request_dict(review_summary_dict, linked_issue_dict):
    return {
        'repo_full_name': 'entrius/gittensor-ui',
        'pr_number': 518,
        'title': 'Example PR',
        'body': 'body text',
        'state': 'MERGED',
        'author_github_id': '218712309',
        'author_login': 'bittoby',
        'author_association': 'CONTRIBUTOR',
        'created_at': '2026-04-01T00:00:00Z',
        'closed_at': '2026-04-18T10:00:00Z',
        'merged_at': '2026-04-18T10:00:00Z',
        'last_edited_at': '2026-04-17T00:00:00Z',
        'edited_after_merge': False,
        'hours_since_merge': 73.16,
        'merged_by_login': 'anderdc',
        'base_ref': 'test',
        'head_ref': 'feature-branch',
        'head_repo_full_name': 'entrius/gittensor-ui',
        'default_branch': 'main',
        'head_sha': 'aaa',
        'base_sha': 'bbb',
        'merge_base_sha': 'ccc',
        'additions': 2,
        'deletions': 2,
        'commits_count': 2,
        'scoring_data_stored': True,
        'review_summary': review_summary_dict,
        'labels': [
            {
                'name': 'refactor',
                'actor_github_id': '61125407',
                'actor_association': 'COLLABORATOR',
            }
        ],
        'linked_issues': [linked_issue_dict],
    }


@pytest.fixture
def solving_pr_dict():
    """The minimal PR shape inlined on issue responses — only the scoring fields."""
    return {
        'pr_number': 529,
        'author_github_id': '218712309',
        'state': 'MERGED',
        'merged_at': '2026-04-02T12:00:00Z',
        'hours_since_merge': 433.55,
        'edited_after_merge': False,
        'head_sha': 'h',
        'base_sha': 'b',
        'merge_base_sha': 'mb',
        'labels': [],
        'review_summary': {'maintainer_changes_requested_count': 1},
    }


@pytest.fixture
def issue_dict(solving_pr_dict):
    return {
        'repo_full_name': 'entrius/gittensor-ui',
        'issue_number': 487,
        'title': 'Some issue',
        'state': 'CLOSED',
        'state_reason': 'COMPLETED',
        'author_github_id': '170233626',
        'author_login': 'someone',
        'author_association': 'CONTRIBUTOR',
        'created_at': '2026-03-20T00:00:00Z',
        'closed_at': '2026-04-02T12:00:00Z',
        'updated_at': '2026-04-02T12:00:00Z',
        'last_edited_at': '2026-03-21T00:00:00Z',
        'is_transferred': False,
        'solved_by_pr': 529,
        'labels': [],
        'solving_pr': solving_pr_dict,
    }


@pytest.fixture
def file_dict():
    return {
        'filename': 'src/components/MinerCard.tsx',
        'previous_filename': None,
        'status': 'modified',
        'additions': 5,
        'deletions': 2,
        'changes': 7,
        'is_binary': False,
        'byte_size': 17867,
        'head_content': 'new content',
        'base_content': 'old content',
    }


# ============================================================================
# MirrorLabel
# ============================================================================


class TestMirrorLabel:
    def test_parses_full_label(self, label_dict):
        label = MirrorLabel.from_dict(label_dict)
        assert label.name == 'refactor'
        assert label.actor_github_id == '61125407'
        assert label.actor_association == 'COLLABORATOR'

    def test_null_actor_attribution_on_backfilled_label(self, label_dict_backfilled):
        label = MirrorLabel.from_dict(label_dict_backfilled)
        assert label.name == 'legacy-label'
        assert label.actor_github_id is None
        assert label.actor_association is None

    def test_missing_actor_keys_default_to_none(self):
        label = MirrorLabel.from_dict({'name': 'docs'})
        assert label.name == 'docs'
        assert label.actor_github_id is None
        assert label.actor_association is None


# ============================================================================
# MirrorReviewSummary
# ============================================================================


class TestMirrorReviewSummary:
    def test_parses_full_summary(self, review_summary_dict):
        rs = MirrorReviewSummary.from_dict(review_summary_dict)
        assert rs.maintainer_changes_requested_count == 2
        assert rs.changes_requested_count == 3
        assert rs.approved_count == 1
        assert rs.commented_count == 0

    def test_minimal_summary_from_solving_pr(self):
        """Inlined solving_pr only populates maintainer_changes_requested_count."""
        rs = MirrorReviewSummary.from_dict({'maintainer_changes_requested_count': 1})
        assert rs.maintainer_changes_requested_count == 1
        assert rs.changes_requested_count == 0
        assert rs.approved_count == 0
        assert rs.commented_count == 0

    def test_empty_summary_all_zero(self):
        rs = MirrorReviewSummary.from_dict({})
        assert rs.maintainer_changes_requested_count == 0
        assert rs.changes_requested_count == 0

    @patch('gittensor.utils.mirror.models.bt.logging')
    def test_warns_when_maintainer_changes_requested_count_absent(self, mock_logging):
        MirrorReviewSummary.from_dict({'changes_requested_count': 1})
        mock_logging.warning.assert_called_once()
        assert 'maintainer_changes_requested_count' in mock_logging.warning.call_args[0][0]

    @patch('gittensor.utils.mirror.models.bt.logging')
    def test_no_warn_when_field_present_even_if_zero(self, mock_logging):
        MirrorReviewSummary.from_dict({'maintainer_changes_requested_count': 0})
        mock_logging.warning.assert_not_called()


# ============================================================================
# MirrorLinkedIssue
# ============================================================================


class TestMirrorLinkedIssue:
    def test_parses_linked_issue(self, linked_issue_dict):
        li = MirrorLinkedIssue.from_dict(linked_issue_dict)
        assert li.number == 553
        assert li.state == 'CLOSED'
        assert li.state_reason == 'COMPLETED'
        assert li.solved_by_pr == 518
        assert li.is_transferred is False
        assert len(li.labels) == 1
        assert li.labels[0].name == 'bug'

    def test_datetimes_parsed_to_aware_utc(self, linked_issue_dict):
        li = MirrorLinkedIssue.from_dict(linked_issue_dict)
        assert li.created_at.tzinfo is not None
        assert li.created_at.utcoffset().total_seconds() == 0
        assert li.closed_at.tzinfo is not None
        assert li.updated_at.tzinfo is not None

    def test_null_state_reason_is_none(self, linked_issue_dict):
        linked_issue_dict['state_reason'] = None
        li = MirrorLinkedIssue.from_dict(linked_issue_dict)
        assert li.state_reason is None

    def test_missing_labels_defaults_empty_list(self, linked_issue_dict):
        del linked_issue_dict['labels']
        li = MirrorLinkedIssue.from_dict(linked_issue_dict)
        assert li.labels == []


# ============================================================================
# MirrorPullRequest
# ============================================================================


class TestMirrorPullRequest:
    def test_parses_full_pr(self, pull_request_dict):
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.pr_number == 518
        assert pr.state == 'MERGED'
        assert pr.author_github_id == '218712309'
        assert pr.edited_after_merge is False
        assert pr.hours_since_merge == 73.16
        assert pr.scoring_data_stored is True

    def test_nested_review_summary_parsed(self, pull_request_dict):
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.review_summary.maintainer_changes_requested_count == 2
        assert pr.review_summary.approved_count == 1

    def test_nested_labels_and_linked_issues(self, pull_request_dict):
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert len(pr.labels) == 1
        assert pr.labels[0].actor_association == 'COLLABORATOR'
        assert len(pr.linked_issues) == 1
        assert pr.linked_issues[0].state_reason == 'COMPLETED'

    def test_nullable_merged_at_for_open_pr(self, pull_request_dict):
        pull_request_dict['state'] = 'OPEN'
        pull_request_dict['merged_at'] = None
        pull_request_dict['closed_at'] = None
        pull_request_dict['hours_since_merge'] = None
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.merged_at is None
        assert pr.closed_at is None
        assert pr.hours_since_merge is None

    def test_created_at_required_and_parsed_to_utc(self, pull_request_dict):
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.created_at == datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)

    def test_author_github_id_coerced_to_string(self, pull_request_dict):
        """Mirror returns string IDs; we coerce defensively in case of int coming through."""
        pull_request_dict['author_github_id'] = 218712309
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.author_github_id == '218712309'

    def test_parity_fields_parsed(self, pull_request_dict):
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.head_ref == 'feature-branch'
        assert pr.head_repo_full_name == 'entrius/gittensor-ui'
        assert pr.default_branch == 'main'

    def test_parity_fields_default_to_none_when_missing(self, pull_request_dict):
        # Older mirror responses (pre-schema) may omit these keys entirely;
        # from_dict should not raise and should yield None.
        for key in ('head_ref', 'head_repo_full_name', 'default_branch'):
            pull_request_dict.pop(key, None)
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.head_ref is None
        assert pr.head_repo_full_name is None
        assert pr.default_branch is None

    def test_repo_names_lowercased_at_parse(self, pull_request_dict):
        # Mirror responses may carry GitHub's canonical mixed-case names; we
        # normalize at the boundary so every downstream lookup against the
        # lowercased mirror_repos dict matches without per-site .lower() calls.
        pull_request_dict['repo_full_name'] = 'Entrius/AllWays'
        pull_request_dict['head_repo_full_name'] = 'Forker/AllWays'
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.repo_full_name == 'entrius/allways'
        assert pr.head_repo_full_name == 'forker/allways'

    def test_head_repo_full_name_none_preserved(self, pull_request_dict):
        pull_request_dict['head_repo_full_name'] = None
        pr = MirrorPullRequest.from_dict(pull_request_dict)
        assert pr.head_repo_full_name is None


# ============================================================================
# MirrorSolvingPR
# ============================================================================


class TestMirrorSolvingPR:
    def test_parses_minimal_solving_pr(self, solving_pr_dict):
        sp = MirrorSolvingPR.from_dict(solving_pr_dict)
        assert sp.pr_number == 529
        assert sp.state == 'MERGED'
        assert sp.edited_after_merge is False
        assert sp.hours_since_merge == 433.55
        assert sp.review_summary.maintainer_changes_requested_count == 1
        # solving_pr omits the other review counts; defaults kick in
        assert sp.review_summary.approved_count == 0


# ============================================================================
# MirrorIssue
# ============================================================================


class TestMirrorIssue:
    def test_parses_full_issue(self, issue_dict):
        issue = MirrorIssue.from_dict(issue_dict)
        assert issue.issue_number == 487
        assert issue.state == 'CLOSED'
        assert issue.state_reason == 'COMPLETED'
        assert issue.solved_by_pr == 529
        assert issue.solving_pr is not None
        assert issue.solving_pr.pr_number == 529

    def test_unsolved_issue_has_no_solving_pr(self, issue_dict):
        issue_dict['solving_pr'] = None
        issue_dict['solved_by_pr'] = None
        issue = MirrorIssue.from_dict(issue_dict)
        assert issue.solving_pr is None
        assert issue.solved_by_pr is None

    def test_missing_solving_pr_key_treated_as_none(self, issue_dict):
        del issue_dict['solving_pr']
        issue = MirrorIssue.from_dict(issue_dict)
        assert issue.solving_pr is None

    def test_repo_full_name_lowercased_at_parse(self, issue_dict):
        issue_dict['repo_full_name'] = 'Entrius/AllWays'
        issue = MirrorIssue.from_dict(issue_dict)
        assert issue.repo_full_name == 'entrius/allways'


# ============================================================================
# MirrorFile
# ============================================================================


class TestMirrorFile:
    def test_parses_modified_file(self, file_dict):
        f = MirrorFile.from_dict(file_dict)
        assert f.filename == 'src/components/MinerCard.tsx'
        assert f.status == 'modified'
        assert f.additions == 5
        assert f.head_content == 'new content'
        assert f.base_content == 'old content'

    def test_binary_has_null_content(self, file_dict):
        file_dict['is_binary'] = True
        file_dict['head_content'] = None
        file_dict['base_content'] = None
        f = MirrorFile.from_dict(file_dict)
        assert f.is_binary is True
        assert f.head_content is None
        assert f.base_content is None

    def test_added_file_has_null_base(self, file_dict):
        file_dict['status'] = 'added'
        file_dict['base_content'] = None
        f = MirrorFile.from_dict(file_dict)
        assert f.base_content is None

    def test_renamed_file_has_previous_filename(self, file_dict):
        file_dict['status'] = 'renamed'
        file_dict['previous_filename'] = 'src/old/MinerCard.tsx'
        f = MirrorFile.from_dict(file_dict)
        assert f.previous_filename == 'src/old/MinerCard.tsx'


# ============================================================================
# Top-level response wrappers
# ============================================================================


class TestMirrorPullRequestsResponse:
    def test_parses_response_envelope(self, pull_request_dict):
        payload = {
            'github_id': '218712309',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T15:00:00Z',
            'pull_requests': [pull_request_dict],
        }
        resp = MirrorPullRequestsResponse.from_dict(payload)
        assert resp.github_id == '218712309'
        assert resp.since == datetime(2026, 3, 15, tzinfo=timezone.utc)
        assert resp.generated_at == datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc)
        assert len(resp.pull_requests) == 1
        assert resp.pull_requests[0].pr_number == 518

    def test_empty_pull_requests_list(self):
        payload = {
            'github_id': '123',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T15:00:00Z',
            'pull_requests': [],
        }
        resp = MirrorPullRequestsResponse.from_dict(payload)
        assert resp.pull_requests == []

    @patch('gittensor.utils.mirror.models.bt.logging')
    def test_malformed_pr_skipped_others_parsed(self, mock_logging, pull_request_dict):
        bad = {'pr_number': 999}  # missing required keys
        payload = {
            'github_id': '218712309',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T15:00:00Z',
            'pull_requests': [bad, pull_request_dict],
        }
        resp = MirrorPullRequestsResponse.from_dict(payload)
        assert len(resp.pull_requests) == 1
        assert resp.pull_requests[0].pr_number == 518
        mock_logging.warning.assert_called()


class TestMirrorIssuesResponse:
    def test_parses_response_envelope(self, issue_dict):
        payload = {
            'github_id': '170233626',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T15:00:00Z',
            'issues': [issue_dict],
        }
        resp = MirrorIssuesResponse.from_dict(payload)
        assert resp.github_id == '170233626'
        assert len(resp.issues) == 1
        assert resp.issues[0].issue_number == 487

    @patch('gittensor.utils.mirror.models.bt.logging')
    def test_malformed_issue_skipped_others_parsed(self, mock_logging, issue_dict):
        bad = {'issue_number': 1}  # missing required keys
        payload = {
            'github_id': '170233626',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T15:00:00Z',
            'issues': [bad, issue_dict],
        }
        resp = MirrorIssuesResponse.from_dict(payload)
        assert len(resp.issues) == 1
        assert resp.issues[0].issue_number == 487
        mock_logging.warning.assert_called()


class TestMirrorPullRequestFilesResponse:
    def test_parses_files_response(self, file_dict):
        payload = {
            'repo_full_name': 'entrius/gittensor-ui',
            'pr_number': 518,
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'scoring_data_stored': True,
            'files': [file_dict],
        }
        resp = MirrorPullRequestFilesResponse.from_dict(payload)
        assert resp.repo_full_name == 'entrius/gittensor-ui'
        assert resp.pr_number == 518
        assert resp.scoring_data_stored is True
        assert len(resp.files) == 1

    def test_empty_files_list(self):
        payload = {
            'repo_full_name': 'entrius/gittensor-ui',
            'pr_number': 518,
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'scoring_data_stored': False,
            'files': [],
        }
        resp = MirrorPullRequestFilesResponse.from_dict(payload)
        assert resp.files == []

    def test_repo_full_name_lowercased_at_parse(self, file_dict):
        payload = {
            'repo_full_name': 'Entrius/AllWays',
            'pr_number': 518,
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'scoring_data_stored': True,
            'files': [file_dict],
        }
        resp = MirrorPullRequestFilesResponse.from_dict(payload)
        assert resp.repo_full_name == 'entrius/allways'

    @patch('gittensor.utils.mirror.models.bt.logging')
    def test_malformed_file_skipped_others_parsed(self, mock_logging, file_dict):
        bad = {'previous_filename': None}  # missing required keys
        payload = {
            'repo_full_name': 'entrius/gittensor-ui',
            'pr_number': 518,
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'scoring_data_stored': True,
            'files': [bad, file_dict],
        }
        resp = MirrorPullRequestFilesResponse.from_dict(payload)
        assert len(resp.files) == 1
        assert resp.files[0].filename == 'src/components/MinerCard.tsx'
        mock_logging.warning.assert_called()


# ============================================================================
# Production decode path: msgspec.json.decode(bytes) -> from_dict
# ============================================================================


class TestEndToEndDecode:
    def test_bytes_round_trip_normalizes_nested_fields(self, pull_request_dict):
        """The MirrorClient path is bytes -> msgspec.json.decode -> from_dict.
        Confirms __post_init__ normalizations (repo lowercasing, int->str
        github_id) fire through the full nested decode, not just leaf-class tests.
        """
        pull_request_dict['repo_full_name'] = 'Entrius/AllWays'
        pull_request_dict['author_github_id'] = 218712309  # int from mirror
        pull_request_dict['linked_issues'][0]['author_github_id'] = 170233626
        payload = {
            'github_id': '218712309',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T15:00:00Z',
            'pull_requests': [pull_request_dict],
        }
        decoded = msgspec.json.decode(json.dumps(payload).encode())
        resp = MirrorPullRequestsResponse.from_dict(decoded)
        pr = resp.pull_requests[0]
        assert pr.repo_full_name == 'entrius/allways'
        assert pr.author_github_id == '218712309'
        assert pr.linked_issues[0].author_github_id == '170233626'

    @patch('gittensor.utils.mirror.models.bt.logging')
    def test_wrong_typed_pr_field_triggers_skip(self, mock_logging, pull_request_dict):
        """Schema drift (wrong type) for a required field is caught per-item
        and the rest of the response decodes - the 'fail loudly' guarantee
        the rewrite trades silent None-spreading for.
        """
        bad = {**pull_request_dict, 'pr_number': 'not-an-int'}
        payload = {
            'github_id': '218712309',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T15:00:00Z',
            'pull_requests': [bad, pull_request_dict],
        }
        resp = MirrorPullRequestsResponse.from_dict(payload)
        assert len(resp.pull_requests) == 1
        assert resp.pull_requests[0].pr_number == 518
        mock_logging.warning.assert_called()
