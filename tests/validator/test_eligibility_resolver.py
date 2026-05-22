"""Tests for resolve_eligibility — per-repo override resolution against the
global default constants."""

from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAX_OPEN_ISSUE_THRESHOLD,
    MAX_OPEN_PR_THRESHOLD,
    MIN_CREDIBILITY,
    MIN_ISSUE_CREDIBILITY,
    MIN_TOKEN_SCORE_FOR_BASE_SCORE,
    MIN_TOKEN_SCORE_FOR_VALID_ISSUE,
    MIN_VALID_MERGED_PRS,
    MIN_VALID_SOLVED_ISSUES,
    OPEN_ISSUE_SPAM_BASE_THRESHOLD,
    OPEN_ISSUE_SPAM_TOKEN_SCORE_PER_SLOT,
    OPEN_PR_THRESHOLD_TOKEN_SCORE,
)
from gittensor.validator.utils.load_weights import RepoEligibilityConfig, resolve_eligibility


def test_none_resolves_entirely_to_global_defaults():
    resolved = resolve_eligibility(None)
    assert resolved.min_valid_merged_prs == MIN_VALID_MERGED_PRS
    assert resolved.min_credibility == MIN_CREDIBILITY
    assert resolved.min_token_score_for_base_score == MIN_TOKEN_SCORE_FOR_BASE_SCORE
    assert resolved.excessive_pr_penalty_base_threshold == EXCESSIVE_PR_PENALTY_BASE_THRESHOLD
    assert resolved.open_pr_threshold_token_score == OPEN_PR_THRESHOLD_TOKEN_SCORE
    assert resolved.max_open_pr_threshold == MAX_OPEN_PR_THRESHOLD
    assert resolved.min_valid_solved_issues == MIN_VALID_SOLVED_ISSUES
    assert resolved.min_issue_credibility == MIN_ISSUE_CREDIBILITY
    assert resolved.min_token_score_for_valid_issue == MIN_TOKEN_SCORE_FOR_VALID_ISSUE
    assert resolved.open_issue_spam_base_threshold == OPEN_ISSUE_SPAM_BASE_THRESHOLD
    assert resolved.open_issue_spam_token_score_per_slot == OPEN_ISSUE_SPAM_TOKEN_SCORE_PER_SLOT
    assert resolved.max_open_issue_threshold == MAX_OPEN_ISSUE_THRESHOLD


def test_empty_config_resolves_to_global_defaults():
    assert resolve_eligibility(RepoEligibilityConfig()) == resolve_eligibility(None)


def test_overrides_take_precedence_over_defaults():
    cfg = RepoEligibilityConfig(min_valid_merged_prs=1, min_credibility=0.5, max_open_pr_threshold=99)
    resolved = resolve_eligibility(cfg)
    assert resolved.min_valid_merged_prs == 1
    assert resolved.min_credibility == 0.5
    assert resolved.max_open_pr_threshold == 99
    # unset fields still fall back to the global default
    assert resolved.min_token_score_for_base_score == MIN_TOKEN_SCORE_FOR_BASE_SCORE


def test_zero_override_is_respected_not_treated_as_unset():
    """0 is a real value (a repo opting out of a gate), not 'use the default'."""
    resolved = resolve_eligibility(RepoEligibilityConfig(min_valid_merged_prs=0, min_credibility=0.0))
    assert resolved.min_valid_merged_prs == 0
    assert resolved.min_credibility == 0.0
