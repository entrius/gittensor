"""Tests for resolve_scoring — per-repo override resolution against the
global default constants."""

from gittensor.constants import OPEN_PR_COLLATERAL_PERCENT, REVIEW_PENALTY_RATE, STANDARD_ISSUE_MULTIPLIER
from gittensor.validator.utils.load_weights import RepoScoringConfig, resolve_scoring


def test_none_resolves_entirely_to_global_defaults():
    resolved = resolve_scoring(None)
    assert resolved.open_pr_collateral_percent == OPEN_PR_COLLATERAL_PERCENT
    assert resolved.review_penalty_rate == REVIEW_PENALTY_RATE
    assert resolved.standard_issue_multiplier == STANDARD_ISSUE_MULTIPLIER


def test_empty_config_resolves_to_global_defaults():
    assert resolve_scoring(RepoScoringConfig()) == resolve_scoring(None)


def test_overrides_take_precedence_over_defaults():
    resolved = resolve_scoring(
        RepoScoringConfig(open_pr_collateral_percent=0.5, review_penalty_rate=0.3, standard_issue_multiplier=2.0)
    )
    assert resolved.open_pr_collateral_percent == 0.5
    assert resolved.review_penalty_rate == 0.3
    assert resolved.standard_issue_multiplier == 2.0


def test_zero_override_is_respected_not_treated_as_unset():
    """0 is a real value (a repo opting out of collateral), not 'use the default'."""
    resolved = resolve_scoring(RepoScoringConfig(open_pr_collateral_percent=0.0))
    assert resolved.open_pr_collateral_percent == 0.0
