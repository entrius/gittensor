"""Unit tests for mirror per-PR scoring helpers.

Focuses on the scoring logic that's mirror-specific:
- Eligibility gate (_should_skip_merged_mirror_pr): edited_after_merge gate,
  self-merge w/o approval, base_ref check
- Label resolution: highest-multiplier maintainer-set label, ignores backfilled
- Issue multiplier: anti-gaming gates (state_reason, is_transferred, self-issue)
- _convert_mirror_files: MirrorFile → FileChange + FileContentPair adapter

Token-scoring base_score is exercised indirectly via the existing legacy tests
(same calculate_token_score_from_file_changes infra).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

scoring_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.scoring',
    reason='Requires gittensor mirror subpackage',
)
adapters_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.adapters',
)
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
scored_pr_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.scored_pr'
)
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')

_should_skip_merged_mirror_pr = scoring_module._should_skip_merged_mirror_pr
_convert_mirror_files = adapters_module.mirror_files_to_legacy
_calculate_pr_multipliers = scoring_module._calculate_pr_multipliers
_resolve_maintainer_set_label = scoring_module._resolve_maintainer_set_label
_calculate_issue_multiplier = scoring_module._calculate_issue_multiplier
_is_valid_linked_issue = scoring_module._is_valid_linked_issue

ScoredMirrorPR = scored_pr_module.ScoredMirrorPR
MirrorPullRequest = mirror_models.MirrorPullRequest
MirrorLinkedIssue = mirror_models.MirrorLinkedIssue
MirrorFile = mirror_models.MirrorFile
RepositoryConfig = load_weights.RepositoryConfig


def _pr(
    state: str = 'MERGED',
    edited_after_merge: bool = False,
    author_login: str = 'bittoby',
    merged_by_login: str | None = 'anderdc',
    author_association: str = 'CONTRIBUTOR',
    base_ref: str = 'test',
    approved_count: int = 1,
    labels: list | None = None,
    linked_issues: list | None = None,
) -> MirrorPullRequest:
    return MirrorPullRequest.from_dict({
        'repo_full_name': 'entrius/gittensor-ui',
        'pr_number': 100,
        'title': 't', 'body': 'b',
        'state': state,
        'author_github_id': '218712309',
        'author_login': author_login,
        'author_association': author_association,
        'created_at': '2026-04-15T00:00:00Z',
        'closed_at': '2026-04-18T10:00:00Z' if state in ('CLOSED', 'MERGED') else None,
        'merged_at': '2026-04-18T10:00:00Z' if state == 'MERGED' else None,
        'last_edited_at': None,
        'edited_after_merge': edited_after_merge,
        'hours_since_merge': 1.0 if state == 'MERGED' else None,
        'merged_by_login': merged_by_login if state == 'MERGED' else None,
        'base_ref': base_ref,
        'head_sha': 'h', 'base_sha': 'b', 'merge_base_sha': 'mb',
        'additions': 1, 'deletions': 0, 'commits_count': 1,
        'scoring_data_stored': True,
        'review_summary': {
            'maintainer_changes_requested_count': 0,
            'changes_requested_count': 0,
            'approved_count': approved_count,
            'commented_count': 0,
        },
        'labels': labels or [],
        'linked_issues': linked_issues or [],
    })


def _config(
    weight: float = 0.5, additional_branches: list | None = None
) -> RepositoryConfig:
    return RepositoryConfig(
        weight=weight,
        mirror_enabled=True,
        additional_acceptable_branches=additional_branches,
    )


# ============================================================================
# Eligibility gate
# ============================================================================


class TestEligibilityGate:
    def test_passes_when_clean(self):
        scored = ScoredMirrorPR(pr=_pr())
        skip, reason = _should_skip_merged_mirror_pr(scored, _config())
        assert skip is False
        assert reason is None

    def test_edited_after_merge_blocks(self):
        scored = ScoredMirrorPR(pr=_pr(edited_after_merge=True))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config())
        assert skip is True
        assert 'edited after merge' in reason

    def test_maintainer_author_blocks(self, monkeypatch):
        monkeypatch.delenv('DEV_MODE', raising=False)
        scored = ScoredMirrorPR(pr=_pr(author_association='OWNER'))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config())
        assert skip is True
        assert 'OWNER' in reason

    def test_dev_mode_bypasses_maintainer_block(self, monkeypatch):
        monkeypatch.setenv('DEV_MODE', '1')
        scored = ScoredMirrorPR(pr=_pr(author_association='OWNER'))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config())
        assert skip is False

    def test_self_merge_without_approval_blocks(self):
        scored = ScoredMirrorPR(pr=_pr(author_login='alice', merged_by_login='alice', approved_count=0))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config())
        assert skip is True
        assert 'self-merged' in reason

    def test_self_merge_with_approval_passes(self):
        # GitHub forbids self-approval; any approval implies external reviewer.
        scored = ScoredMirrorPR(pr=_pr(author_login='alice', merged_by_login='alice', approved_count=1))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config())
        assert skip is False

    def test_base_ref_in_additional_passes(self):
        scored = ScoredMirrorPR(pr=_pr(base_ref='test'))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config(additional_branches=['test', 'staging']))
        assert skip is False

    def test_base_ref_not_in_additional_blocks(self):
        scored = ScoredMirrorPR(pr=_pr(base_ref='feature/foo'))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config(additional_branches=['test', 'staging']))
        assert skip is True
        assert "merged to 'feature/foo'" in reason

    def test_no_additional_branches_accepts_any_base_ref(self):
        # Without an explicit list and without default-branch info from mirror,
        # we accept any base_ref (documented as a known gap).
        scored = ScoredMirrorPR(pr=_pr(base_ref='whatever'))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config(additional_branches=None))
        assert skip is False


# ============================================================================
# File adapter
# ============================================================================


class TestConvertMirrorFiles:
    def test_translates_basic_fields(self):
        files = [
            MirrorFile.from_dict({
                'filename': 'src/foo.py', 'previous_filename': None,
                'status': 'modified',
                'additions': 5, 'deletions': 2, 'changes': 7,
                'is_binary': False, 'byte_size': 100,
                'head_content': 'new', 'base_content': 'old',
            }),
        ]
        file_changes, file_contents = _convert_mirror_files('owner/repo', 42, files)

        assert len(file_changes) == 1
        fc = file_changes[0]
        assert fc.filename == 'src/foo.py'
        assert fc.pr_number == 42
        assert fc.repository_full_name == 'owner/repo'
        assert (fc.additions, fc.deletions, fc.changes) == (5, 2, 7)
        assert fc.status == 'modified'

        pair = file_contents['src/foo.py']
        assert pair.old_content == 'old'
        assert pair.new_content == 'new'

    def test_added_file_has_null_old(self):
        files = [
            MirrorFile.from_dict({
                'filename': 'new.py', 'previous_filename': None,
                'status': 'added',
                'additions': 10, 'deletions': 0, 'changes': 10,
                'is_binary': False, 'byte_size': 100,
                'head_content': 'new', 'base_content': None,
            }),
        ]
        _, file_contents = _convert_mirror_files('o/r', 1, files)
        assert file_contents['new.py'].old_content is None
        assert file_contents['new.py'].new_content == 'new'

    def test_renamed_file_carries_previous_filename(self):
        files = [
            MirrorFile.from_dict({
                'filename': 'new_name.py', 'previous_filename': 'old_name.py',
                'status': 'renamed',
                'additions': 0, 'deletions': 0, 'changes': 0,
                'is_binary': False, 'byte_size': 100,
                'head_content': 'x', 'base_content': 'x',
            }),
        ]
        file_changes, _ = _convert_mirror_files('o/r', 1, files)
        assert file_changes[0].previous_filename == 'old_name.py'


# ============================================================================
# Label resolution (maintainer-set + highest multiplier)
# ============================================================================


class TestLabelResolution:
    def test_no_labels_returns_none(self):
        scored = ScoredMirrorPR(pr=_pr(labels=[]))
        assert _resolve_maintainer_set_label(scored.pr) is None

    def test_non_scoring_labels_ignored(self):
        # 'enhancement' is in LABEL_MULTIPLIERS, 'random' typically isn't
        labels = [
            {'name': 'random', 'actor_github_id': '1', 'actor_association': 'OWNER'},
        ]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        assert _resolve_maintainer_set_label(scored.pr) is None

    def test_non_maintainer_label_ignored(self):
        from gittensor.constants import LABEL_MULTIPLIERS
        scoring_label = next(iter(LABEL_MULTIPLIERS.keys()))
        labels = [
            {'name': scoring_label, 'actor_github_id': '1', 'actor_association': 'CONTRIBUTOR'},
        ]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        assert _resolve_maintainer_set_label(scored.pr) is None

    def test_null_actor_association_ignored(self):
        from gittensor.constants import LABEL_MULTIPLIERS
        scoring_label = next(iter(LABEL_MULTIPLIERS.keys()))
        labels = [
            {'name': scoring_label, 'actor_github_id': None, 'actor_association': None},
        ]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        assert _resolve_maintainer_set_label(scored.pr) is None

    def test_maintainer_set_scoring_label_returned(self):
        from gittensor.constants import LABEL_MULTIPLIERS
        scoring_label = next(iter(LABEL_MULTIPLIERS.keys()))
        labels = [
            {'name': scoring_label, 'actor_github_id': '1', 'actor_association': 'COLLABORATOR'},
        ]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        assert _resolve_maintainer_set_label(scored.pr) == scoring_label.lower()

    def test_highest_multiplier_wins(self):
        from gittensor.constants import LABEL_MULTIPLIERS
        scoring_labels = list(LABEL_MULTIPLIERS.keys())
        if len(scoring_labels) < 2:
            pytest.skip('Need at least 2 scoring labels for this test')

        # Pick two different scoring labels
        a, b = scoring_labels[0], scoring_labels[1]
        labels = [
            {'name': a, 'actor_github_id': '1', 'actor_association': 'OWNER'},
            {'name': b, 'actor_github_id': '1', 'actor_association': 'OWNER'},
        ]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        chosen = _resolve_maintainer_set_label(scored.pr)
        # Whichever label has the higher LABEL_MULTIPLIERS value should win
        expected = max([a, b], key=lambda n: (LABEL_MULTIPLIERS[n], n)).lower()
        assert chosen == expected


# ============================================================================
# Issue multiplier
# ============================================================================


def _linked_issue(
    state: str = 'CLOSED',
    state_reason: str | None = 'COMPLETED',
    is_transferred: bool = False,
    author_github_id: str | None = '999',
    created_at: str = '2026-04-10T00:00:00Z',
    closed_at: str = '2026-04-18T10:00:00Z',
    author_association: str | None = 'CONTRIBUTOR',
    number: int = 50,
):
    return {
        'number': number,
        'title': 't',
        'state': state,
        'state_reason': state_reason,
        'author_github_id': author_github_id,
        'author_association': author_association,
        'created_at': created_at,
        'closed_at': closed_at,
        'updated_at': closed_at,
        'is_transferred': is_transferred,
        'solved_by_pr': 100,
        'labels': [],
    }


class TestIssueMultiplier:
    def test_no_linked_issues_returns_neutral(self):
        scored = ScoredMirrorPR(pr=_pr(linked_issues=[]))
        assert _calculate_issue_multiplier(scored) == 1.0

    def test_valid_standard_issue(self):
        from gittensor.constants import STANDARD_ISSUE_MULTIPLIER
        scored = ScoredMirrorPR(pr=_pr(linked_issues=[_linked_issue()]))
        assert _calculate_issue_multiplier(scored) == STANDARD_ISSUE_MULTIPLIER

    def test_maintainer_authored_issue_gets_maintainer_multiplier(self):
        from gittensor.constants import MAINTAINER_ISSUE_MULTIPLIER
        scored = ScoredMirrorPR(pr=_pr(linked_issues=[_linked_issue(author_association='OWNER')]))
        assert _calculate_issue_multiplier(scored) == MAINTAINER_ISSUE_MULTIPLIER

    def test_first_valid_issue_chosen(self):
        # Even if the first issue is invalid, valid second one should be chosen
        invalid = _linked_issue(is_transferred=True)
        valid = _linked_issue(number=51, author_github_id='888')
        scored = ScoredMirrorPR(pr=_pr(linked_issues=[invalid, valid]))
        from gittensor.constants import STANDARD_ISSUE_MULTIPLIER
        assert _calculate_issue_multiplier(scored) == STANDARD_ISSUE_MULTIPLIER


class TestLinkedIssueValidity:
    def test_clean_valid(self):
        scored = ScoredMirrorPR(pr=_pr())
        li_data = _linked_issue()
        li = MirrorLinkedIssue.from_dict(li_data)
        assert _is_valid_linked_issue(li, scored.pr) is True

    def test_transferred_blocks(self):
        scored = ScoredMirrorPR(pr=_pr())
        li = MirrorLinkedIssue.from_dict(_linked_issue(is_transferred=True))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_missing_author_blocks(self):
        scored = ScoredMirrorPR(pr=_pr())
        li = MirrorLinkedIssue.from_dict(_linked_issue(author_github_id=None))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_self_authored_blocks(self):
        # PR author_github_id is 218712309 (from _pr); make linked issue have same
        scored = ScoredMirrorPR(pr=_pr())
        li = MirrorLinkedIssue.from_dict(_linked_issue(author_github_id='218712309'))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_issue_created_after_pr_blocks(self):
        # _pr created_at is 2026-04-15
        scored = ScoredMirrorPR(pr=_pr())
        li = MirrorLinkedIssue.from_dict(_linked_issue(created_at='2026-04-20T00:00:00Z'))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_pr_edited_after_merge_blocks(self):
        scored = ScoredMirrorPR(pr=_pr(edited_after_merge=True))
        li = MirrorLinkedIssue.from_dict(_linked_issue())
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_open_issue_blocks_for_merged_pr(self):
        scored = ScoredMirrorPR(pr=_pr())
        li = MirrorLinkedIssue.from_dict(_linked_issue(state='OPEN', state_reason=None, closed_at=None))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_state_reason_not_completed_blocks(self):
        scored = ScoredMirrorPR(pr=_pr())
        li = MirrorLinkedIssue.from_dict(_linked_issue(state_reason='NOT_PLANNED'))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_closed_too_far_from_merge_blocks(self):
        # MAX_ISSUE_CLOSE_WINDOW_DAYS = 1; close 5 days from merge
        scored = ScoredMirrorPR(pr=_pr())
        li = MirrorLinkedIssue.from_dict(_linked_issue(closed_at='2026-04-12T00:00:00Z'))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_open_pr_skips_merge_only_gates(self):
        # OPEN PR shouldn't apply the merge-only gates
        scored = ScoredMirrorPR(pr=_pr(state='OPEN'))
        li = MirrorLinkedIssue.from_dict(
            _linked_issue(state='OPEN', state_reason=None, closed_at=None)
        )
        # Author check still applies — set different authors
        # The issue with state=OPEN/state_reason=None should still pass for an OPEN PR
        assert _is_valid_linked_issue(li, scored.pr) is True


# ============================================================================
# Multiplier composition (smoke test that all multipliers populate)
# ============================================================================


class TestPrMultipliers:
    def test_merged_pr_populates_all_multipliers(self):
        from gittensor.constants import LABEL_MULTIPLIERS
        scoring_label = next(iter(LABEL_MULTIPLIERS.keys()))
        labels = [{'name': scoring_label, 'actor_github_id': '1', 'actor_association': 'OWNER'}]

        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        scored.token_score = 100.0  # for completeness
        _calculate_pr_multipliers(scored, _config(weight=0.7, additional_branches=['test']))

        assert scored.repo_weight_multiplier == 0.7
        assert scored.label == scoring_label.lower()
        assert scored.label_multiplier == LABEL_MULTIPLIERS[scoring_label.lower()]
        assert 0.0 <= scored.time_decay_multiplier <= 1.0
        assert scored.review_quality_multiplier == 1.0  # 0 maintainer changes_requested
        assert scored.issue_multiplier == 1.0  # no linked_issues
        assert scored.open_pr_spam_multiplier == 1.0  # set in finalize, neutral here

    def test_open_pr_only_neutral_multipliers(self):
        scored = ScoredMirrorPR(pr=_pr(state='OPEN'))
        _calculate_pr_multipliers(scored, _config(weight=0.5))

        assert scored.repo_weight_multiplier == 0.5
        # Time decay / review quality / credibility are merge-only — kept neutral here
        assert scored.time_decay_multiplier == 1.0
        assert scored.credibility_multiplier == 1.0
        assert scored.review_quality_multiplier == 1.0
