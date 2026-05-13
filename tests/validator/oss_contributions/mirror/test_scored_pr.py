"""Unit tests for ScoredPR.

Covers:
- Composition: raw response data accessed via .pr.<field>; scoring fields default neutrally
- is_pioneer_eligible respects merged + token_score gate
- calculate_final_earned_score multiplies base by every multiplier
"""

from __future__ import annotations

import pytest

scored_pr_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.scored_pr',
    reason='Requires gittensor mirror subpackage',
)
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')

ScoredPR = scored_pr_module.ScoredPR
MirrorPullRequest = mirror_models.MirrorPullRequest
MirrorReviewSummary = mirror_models.MirrorReviewSummary


def _make_pr(state: str = 'MERGED', merged_at_iso: str | None = '2026-04-18T10:00:00Z') -> MirrorPullRequest:
    return MirrorPullRequest.from_dict(
        {
            'repo_full_name': 'entrius/gittensor-ui',
            'pr_number': 100,
            'title': 't',
            'body': 'b',
            'state': state,
            'author_github_id': '1',
            'author_login': 'a',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-01T00:00:00Z',
            'closed_at': merged_at_iso,
            'merged_at': merged_at_iso,
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
            'linked_issues': [],
        }
    )


class TestComposition:
    def test_raw_data_accessed_through_pr(self):
        scored = ScoredPR(pr=_make_pr())
        assert scored.pr.pr_number == 100
        assert scored.pr.repo_full_name == 'entrius/gittensor-ui'
        assert scored.pr.author_github_id == '1'

    def test_scoring_fields_default_neutral(self):
        scored = ScoredPR(pr=_make_pr())
        for mult in [
            scored.repo_weight_multiplier,
            scored.issue_multiplier,
            scored.open_pr_spam_multiplier,
            scored.time_decay_multiplier,
            scored.credibility_multiplier,
            scored.review_quality_multiplier,
            scored.label_multiplier,
        ]:
            assert mult == 1.0
        assert scored.base_score == 0.0
        assert scored.earned_score == 0.0
        assert scored.token_score == 0.0
        assert scored.pioneer_rank == 0
        assert scored.files is None


class TestPioneerEligible:
    def test_unmerged_not_eligible(self):
        scored = ScoredPR(pr=_make_pr(state='OPEN', merged_at_iso=None))
        scored.token_score = 100.0
        assert scored.is_pioneer_eligible() is False

    def test_merged_below_token_threshold_not_eligible(self):
        scored = ScoredPR(pr=_make_pr())
        scored.token_score = 1.0  # below MIN_TOKEN_SCORE_FOR_BASE_SCORE (5)
        assert scored.is_pioneer_eligible() is False

    def test_merged_at_threshold_eligible(self):
        scored = ScoredPR(pr=_make_pr())
        scored.token_score = 5.0  # equals MIN_TOKEN_SCORE_FOR_BASE_SCORE
        assert scored.is_pioneer_eligible() is True

    def test_merged_above_threshold_eligible(self):
        scored = ScoredPR(pr=_make_pr())
        scored.token_score = 50.0
        assert scored.is_pioneer_eligible() is True


class TestCalculateFinalEarnedScore:
    def test_neutral_multipliers_returns_base(self):
        scored = ScoredPR(pr=_make_pr())
        scored.base_score = 25.0
        result = scored.calculate_final_earned_score()
        assert result == 25.0
        assert scored.earned_score == 25.0

    def test_multipliers_compose(self):
        scored = ScoredPR(pr=_make_pr())
        scored.base_score = 100.0
        scored.repo_weight_multiplier = 0.5
        scored.review_quality_multiplier = 0.5
        # Repo emission share is applied at aggregation, not per PR.
        assert scored.calculate_final_earned_score() == 50.0

    def test_zero_multiplier_zeros_score(self):
        scored = ScoredPR(pr=_make_pr())
        scored.base_score = 100.0
        scored.review_quality_multiplier = 0.0
        assert scored.calculate_final_earned_score() == 0.0
