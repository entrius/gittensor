"""Unit tests for mirror per-PR scoring helpers.

Focuses on the scoring logic that's mirror-specific:
- Eligibility gate (_should_skip_merged_mirror_pr): self-merge w/o approval,
  base_ref / head_ref / default_branch checks. Note: edited_after_merge is
  NOT a PR-level gate (legacy parity) — it only invalidates the issue bonus,
  tested in TestLinkedIssueValidity below.
- Label resolution: highest-multiplier maintainer-set label, ignores backfilled
- Issue multiplier: anti-gaming gates (state_reason, is_transferred, self-issue,
  edited_after_merge)
- _convert_mirror_files: MirrorFile → FileChange + FileContentPair adapter

Token-scoring base_score is exercised indirectly via the existing legacy tests
(same calculate_token_score_from_file_changes infra).
"""

from __future__ import annotations

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
scored_pr_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scored_pr')
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')

_should_skip_merged_mirror_pr = scoring_module._should_skip_merged_mirror_pr
_convert_mirror_files = adapters_module.mirror_files_to_legacy
_calculate_pr_multipliers = scoring_module._calculate_pr_multipliers
_resolve_trusted_scoring_label = scoring_module._resolve_trusted_scoring_label
_calculate_issue_multiplier = scoring_module._calculate_issue_multiplier
_is_valid_linked_issue = scoring_module._is_valid_linked_issue
score_mirror_pr = scoring_module.score_mirror_pr

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
    base_ref: str = 'main',
    head_ref: str | None = 'feature/foo',
    head_repo_full_name: str | None = 'entrius/gittensor-ui',
    default_branch: str | None = 'main',
    approved_count: int = 1,
    labels: list | None = None,
    linked_issues: list | None = None,
) -> MirrorPullRequest:
    return MirrorPullRequest.from_dict(
        {
            'repo_full_name': 'entrius/gittensor-ui',
            'pr_number': 100,
            'title': 't',
            'body': 'b',
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
            'head_ref': head_ref,
            'head_repo_full_name': head_repo_full_name,
            'default_branch': default_branch,
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'additions': 1,
            'deletions': 0,
            'commits_count': 1,
            'scoring_data_stored': True,
            'review_summary': {
                'maintainer_changes_requested_count': 0,
                'changes_requested_count': 0,
                'approved_count': approved_count,
                'commented_count': 0,
            },
            'labels': labels or [],
            'linked_issues': linked_issues or [],
        }
    )


def _config(
    weight: float = 0.5,
    additional_branches: list | None = None,
    trusted_label_pipeline: bool = False,
) -> RepositoryConfig:
    return RepositoryConfig(
        weight=weight,
        mirror_enabled=True,
        additional_acceptable_branches=additional_branches,
        trusted_label_pipeline=trusted_label_pipeline,
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

    def test_edited_after_merge_does_not_block_pr_score(self):
        """Legacy parity: edited_after_merge gates only the issue bonus
        (see _is_valid_linked_issue), not the whole PR's base score. A miner
        editing a typo in their PR description after merge should still earn
        their base score + non-issue multipliers."""
        scored = ScoredMirrorPR(pr=_pr(edited_after_merge=True))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config())
        assert skip is False
        assert reason is None

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

    def test_default_branch_matches_without_additional(self):
        # With default_branch='main' and no additional, acceptable=['main'];
        # base_ref='main' passes.
        scored = ScoredMirrorPR(pr=_pr(base_ref='main', default_branch='main'))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config(additional_branches=None))
        assert skip is False

    def test_base_ref_mismatches_default_branch_blocks(self):
        # Closes the prior gap: with default_branch known and no additional,
        # a non-matching base_ref is now rejected (legacy parity).
        scored = ScoredMirrorPR(pr=_pr(base_ref='whatever', default_branch='main'))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config(additional_branches=None))
        assert skip is True
        assert "merged to 'whatever'" in reason

    def test_no_default_branch_no_additional_accepts_any_base_ref(self):
        # When BOTH default_branch and additional are missing (older mirror rows
        # predating default_branch exposure), there's no acceptable set to
        # check against — fall through rather than false-positive.
        scored = ScoredMirrorPR(pr=_pr(base_ref='whatever', default_branch=None))
        skip, reason = _should_skip_merged_mirror_pr(scored, _config(additional_branches=None))
        assert skip is False

    def test_head_ref_in_additional_blocks_same_repo(self):
        scored = ScoredMirrorPR(
            pr=_pr(
                base_ref='main',
                head_ref='test',
                head_repo_full_name='entrius/gittensor-ui',
            )
        )
        skip, reason = _should_skip_merged_mirror_pr(
            scored,
            _config(additional_branches=['test', 'staging']),
        )
        assert skip is True
        assert "source branch 'test'" in reason

    def test_head_ref_in_additional_passes_for_fork(self):
        # Fork PR whose head branch happens to collide with an acceptable
        # branch — fork branch names are arbitrary, legacy skips this case
        # and so do we.
        scored = ScoredMirrorPR(
            pr=_pr(
                base_ref='main',
                head_ref='test',
                head_repo_full_name='outsider/fork',
            )
        )
        skip, reason = _should_skip_merged_mirror_pr(
            scored,
            _config(additional_branches=['test']),
        )
        assert skip is False

    def test_null_head_ref_skips_check(self):
        scored = ScoredMirrorPR(
            pr=_pr(
                base_ref='main',
                head_ref=None,
                head_repo_full_name='entrius/gittensor-ui',
            )
        )
        skip, reason = _should_skip_merged_mirror_pr(
            scored,
            _config(additional_branches=['main']),
        )
        assert skip is False

    def test_null_head_repo_full_name_skips_check(self):
        # Pre-schema mirror rows may have NULL head_repo_full_name — we can't
        # distinguish same-repo from fork, so fall through conservatively.
        scored = ScoredMirrorPR(
            pr=_pr(
                base_ref='main',
                head_ref='test',
                head_repo_full_name=None,
            )
        )
        skip, reason = _should_skip_merged_mirror_pr(
            scored,
            _config(additional_branches=['test']),
        )
        assert skip is False

    def test_wildcard_head_ref_match_blocks(self):
        # Parity with legacy: wildcard patterns in additional_acceptable_branches
        # match against head_ref too.
        scored = ScoredMirrorPR(
            pr=_pr(
                base_ref='main',
                head_ref='3.0-dev',
                head_repo_full_name='entrius/gittensor-ui',
            )
        )
        skip, reason = _should_skip_merged_mirror_pr(
            scored,
            _config(additional_branches=['*-dev']),
        )
        assert skip is True
        assert "source branch '3.0-dev'" in reason

    def test_wildcard_base_ref_match_passes(self):
        scored = ScoredMirrorPR(pr=_pr(base_ref='3.0-dev'))
        skip, reason = _should_skip_merged_mirror_pr(
            scored,
            _config(additional_branches=['*-dev']),
        )
        assert skip is False

    def test_default_branch_as_head_blocks_cross_branch_merge(self):
        # staging <- main merge with additional=['staging'], default_branch='main':
        # acceptable=['main','staging'], head_ref='main' matches → block.
        # Closes the default-branch-not-in-acceptable gap.
        scored = ScoredMirrorPR(
            pr=_pr(
                base_ref='staging',
                head_ref='main',
                head_repo_full_name='entrius/gittensor-ui',
                default_branch='main',
            )
        )
        skip, reason = _should_skip_merged_mirror_pr(
            scored,
            _config(additional_branches=['staging']),
        )
        assert skip is True
        assert "source branch 'main'" in reason


# ============================================================================
# scoring_data_stored gate
# ============================================================================


class TestScoringDataStoredGate:
    def test_skips_fetch_when_flag_false(self):
        scored = ScoredMirrorPR(pr=_pr(state='CLOSED'))
        scored.pr.scoring_data_stored = False
        client = Mock()

        score_mirror_pr(
            scored,
            mirror_eval=Mock(),
            mirror_repos={scored.pr.repo_full_name: _config()},
            programming_languages={},
            token_config=Mock(),
            client=client,
        )

        client.get_pr_files.assert_not_called()
        assert scored.files is None
        assert scored.base_score == 0.0


# ============================================================================
# File adapter
# ============================================================================


class TestConvertMirrorFiles:
    def test_translates_basic_fields(self):
        files = [
            MirrorFile.from_dict(
                {
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
            ),
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
            MirrorFile.from_dict(
                {
                    'filename': 'new.py',
                    'previous_filename': None,
                    'status': 'added',
                    'additions': 10,
                    'deletions': 0,
                    'changes': 10,
                    'is_binary': False,
                    'byte_size': 100,
                    'head_content': 'new',
                    'base_content': None,
                }
            ),
        ]
        _, file_contents = _convert_mirror_files('o/r', 1, files)
        assert file_contents['new.py'].old_content is None
        assert file_contents['new.py'].new_content == 'new'

    def test_renamed_file_carries_previous_filename(self):
        files = [
            MirrorFile.from_dict(
                {
                    'filename': 'new_name.py',
                    'previous_filename': 'old_name.py',
                    'status': 'renamed',
                    'additions': 0,
                    'deletions': 0,
                    'changes': 0,
                    'is_binary': False,
                    'byte_size': 100,
                    'head_content': 'x',
                    'base_content': 'x',
                }
            ),
        ]
        file_changes, _ = _convert_mirror_files('o/r', 1, files)
        assert file_changes[0].previous_filename == 'old_name.py'


# ============================================================================
# Label resolution (maintainer-set + highest multiplier)
# ============================================================================


class TestLabelResolution:
    def test_no_labels_returns_none(self):
        scored = ScoredMirrorPR(pr=_pr(labels=[]))
        assert _resolve_trusted_scoring_label(scored.pr, _config()) is None

    def test_non_scoring_labels_ignored(self):
        # 'enhancement' is in LABEL_MULTIPLIERS, 'random' typically isn't
        labels = [
            {'name': 'random', 'actor_github_id': '1', 'actor_association': 'OWNER'},
        ]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        assert _resolve_trusted_scoring_label(scored.pr, _config()) is None

    @pytest.mark.parametrize(
        'actor_association,resolves',
        [
            ('OWNER', True),
            ('MEMBER', True),
            ('COLLABORATOR', True),
            ('CONTRIBUTOR', False),
            ('NONE', False),
            (None, False),
        ],
    )
    def test_untrusted_repo_requires_maintainer_actor(self, actor_association, resolves):
        labels = [{'name': 'feature', 'actor_github_id': '1', 'actor_association': actor_association}]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        expected = 'feature' if resolves else None
        assert _resolve_trusted_scoring_label(scored.pr, _config(trusted_label_pipeline=False)) == expected

    @pytest.mark.parametrize(
        'actor_association',
        ['OWNER', 'MEMBER', 'COLLABORATOR', 'CONTRIBUTOR', 'NONE', None],
    )
    def test_trusted_repo_accepts_any_scoring_label_actor(self, actor_association):
        labels = [{'name': 'feature', 'actor_github_id': '1', 'actor_association': actor_association}]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        assert _resolve_trusted_scoring_label(scored.pr, _config(trusted_label_pipeline=True)) == 'feature'

    def test_maintainer_set_scoring_label_returned(self):
        from gittensor.constants import LABEL_MULTIPLIERS

        scoring_label = next(iter(LABEL_MULTIPLIERS.keys()))
        labels = [
            {'name': scoring_label, 'actor_github_id': '1', 'actor_association': 'COLLABORATOR'},
        ]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))
        assert _resolve_trusted_scoring_label(scored.pr, _config()) == scoring_label.lower()

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
        chosen = _resolve_trusted_scoring_label(scored.pr, _config())
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
    closed_at: str | None = '2026-04-18T10:00:00Z',
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

    def test_issue_closed_before_pr_merged_blocks(self):
        """Legacy parity: issue.closed_at < pr.merged_at → negative days_diff → reject.
        If the issue was closed before the PR merged, the PR wasn't what solved it."""
        # _pr default merged_at = 2026-04-18. Issue closed 2026-04-17 (1 day earlier).
        scored = ScoredMirrorPR(pr=_pr())
        li = MirrorLinkedIssue.from_dict(_linked_issue(closed_at='2026-04-17T00:00:00Z'))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_closed_issue_not_planned_blocks_open_pr_too(self):
        """Legacy parity: CLOSED issue with state_reason != COMPLETED is rejected
        regardless of PR state. Matters for OPEN PR collateral scoring."""
        scored = ScoredMirrorPR(pr=_pr(state='OPEN'))  # OPEN PR
        li = MirrorLinkedIssue.from_dict(_linked_issue(state='CLOSED', state_reason='NOT_PLANNED'))
        assert _is_valid_linked_issue(li, scored.pr) is False

    def test_open_issue_on_open_pr_still_valid(self):
        """An OPEN issue linked to an OPEN PR should still be valid (state_reason only
        gates CLOSED issues)."""
        scored = ScoredMirrorPR(pr=_pr(state='OPEN'))
        li = MirrorLinkedIssue.from_dict(_linked_issue(state='OPEN', state_reason=None, closed_at=None))
        assert _is_valid_linked_issue(li, scored.pr) is True


class TestIssueMultiplierPreference:
    def test_prefer_maintainer_authored_when_multiple_valid(self):
        """Legacy parity (PR #673): the issue multiplier should pick a
        maintainer-authored valid issue regardless of response ordering."""
        from gittensor.constants import MAINTAINER_ISSUE_MULTIPLIER

        # Non-maintainer issue listed first, maintainer-authored issue second
        non_maint = _linked_issue(number=1, author_association='CONTRIBUTOR', author_github_id='111')
        maint = _linked_issue(number=2, author_association='OWNER', author_github_id='222')
        scored = ScoredMirrorPR(pr=_pr(linked_issues=[non_maint, maint]))
        assert _calculate_issue_multiplier(scored) == MAINTAINER_ISSUE_MULTIPLIER

    def test_falls_back_to_first_when_no_maintainer_authored(self):
        from gittensor.constants import STANDARD_ISSUE_MULTIPLIER

        issue_a = _linked_issue(number=1, author_association='CONTRIBUTOR', author_github_id='111')
        issue_b = _linked_issue(number=2, author_association='CONTRIBUTOR', author_github_id='222')
        scored = ScoredMirrorPR(pr=_pr(linked_issues=[issue_a, issue_b]))
        assert _calculate_issue_multiplier(scored) == STANDARD_ISSUE_MULTIPLIER


class TestCollateralScoreAcceptsScoredMirrorPR:
    """Regression test for the crash where calculate_open_pr_collateral_score accessed
    pr.number which doesn't exist on ScoredMirrorPR. The .number property now proxies
    to pr.pr_number so duck-typing works."""

    def test_collateral_computed_without_crash(self):
        from gittensor.validator.oss_contributions.scoring import calculate_open_pr_collateral_score

        scored = ScoredMirrorPR(pr=_pr(state='OPEN'))
        scored.base_score = 25.0
        scored.repo_weight_multiplier = 0.5
        scored.issue_multiplier = 1.0
        scored.label_multiplier = 1.0

        # Must not raise AttributeError on .number
        result = calculate_open_pr_collateral_score(scored)
        assert result >= 0.0

    def test_number_property_proxies_to_pr_pr_number(self):
        scored = ScoredMirrorPR(pr=_pr())
        assert scored.number == scored.pr.pr_number == 100

    def test_repository_full_name_property_proxies(self):
        scored = ScoredMirrorPR(pr=_pr())
        assert scored.repository_full_name == scored.pr.repo_full_name == 'entrius/gittensor-ui'

    def test_open_pr_skips_merge_only_gates(self):
        # OPEN PR shouldn't apply the merge-only gates
        scored = ScoredMirrorPR(pr=_pr(state='OPEN'))
        li = MirrorLinkedIssue.from_dict(_linked_issue(state='OPEN', state_reason=None, closed_at=None))
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

    def test_bot_applied_refactor_label_scores_on_trusted_repo(self):
        labels = [{'name': 'refactor', 'actor_github_id': '1', 'actor_association': None}]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))

        _calculate_pr_multipliers(scored, _config(trusted_label_pipeline=True))

        assert scored.label == 'refactor'
        assert scored.label_multiplier == 0.5

    def test_bot_applied_label_stays_neutral_on_untrusted_repo(self):
        labels = [{'name': 'feature', 'actor_github_id': '1', 'actor_association': None}]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))

        _calculate_pr_multipliers(scored, _config(trusted_label_pipeline=False))

        assert scored.label is None
        assert scored.label_multiplier == 1.0

    @pytest.mark.parametrize('trusted_label_pipeline', [False, True])
    def test_maintainer_applied_label_scores_on_all_repos(self, trusted_label_pipeline):
        labels = [{'name': 'feature', 'actor_github_id': '1', 'actor_association': 'OWNER'}]
        scored = ScoredMirrorPR(pr=_pr(labels=labels))

        _calculate_pr_multipliers(scored, _config(trusted_label_pipeline=trusted_label_pipeline))

        assert scored.label == 'feature'
        assert scored.label_multiplier == 1.5
