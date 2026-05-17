"""Tests for resolve_scoring — per-repo override resolution against the
global default constants."""

from gittensor.constants import OPEN_PR_COLLATERAL_PERCENT
from gittensor.validator.utils.load_weights import RepoScoringConfig, resolve_scoring


def test_none_resolves_entirely_to_global_defaults():
    resolved = resolve_scoring(None)
    assert resolved.open_pr_collateral_percent == OPEN_PR_COLLATERAL_PERCENT


def test_empty_config_resolves_to_global_defaults():
    assert resolve_scoring(RepoScoringConfig()) == resolve_scoring(None)


def test_overrides_take_precedence_over_defaults():
    resolved = resolve_scoring(RepoScoringConfig(open_pr_collateral_percent=0.5))
    assert resolved.open_pr_collateral_percent == 0.5


def test_zero_override_is_respected_not_treated_as_unset():
    """0 is a real value (a repo opting out of collateral), not 'use the default'."""
    resolved = resolve_scoring(RepoScoringConfig(open_pr_collateral_percent=0.0))
    assert resolved.open_pr_collateral_percent == 0.0
