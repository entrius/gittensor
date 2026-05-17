"""Tests for check_issue_eligibility — the per-repository issue-discovery gate.

Credibility is solved / (solved + closed); the gate also requires a minimum
number of *valid* solved issues (solving PR meets the token threshold).
"""

import pytest

from gittensor.validator.issue_discovery.scoring import check_issue_eligibility
from gittensor.validator.utils.load_weights import RepoEligibilityConfig, resolve_eligibility

_CFG = resolve_eligibility(None)  # defaults: 3 valid solved issues, 0.70 issue credibility


@pytest.mark.parametrize(
    'solved_count,valid_solved_count,closed_count,expected_credibility,expected_eligible',
    [
        # credibility uses total solved/attempts; the gate uses valid_solved
        (10, 7, 2, pytest.approx(10 / 12, abs=1e-3), True),
        # valid below the minimum -> ineligible regardless of credibility
        (10, 2, 2, pytest.approx(10 / 12, abs=1e-3), False),
        # no solved issues -> credibility 0, ineligible
        (0, 0, 2, 0.0, False),
        # credibility below 0.70 -> ineligible even with enough valid solves
        (4, 4, 3, pytest.approx(4 / 7, abs=1e-3), False),
        # exactly at both gates
        (3, 3, 0, 1.0, True),
    ],
)
def test_check_issue_eligibility_uses_total_for_credibility_valid_for_gate(
    solved_count: int,
    valid_solved_count: int,
    closed_count: int,
    expected_credibility: float,
    expected_eligible: bool,
) -> None:
    is_eligible, credibility, _ = check_issue_eligibility(_CFG, solved_count, valid_solved_count, closed_count)
    assert credibility == expected_credibility
    assert is_eligible is expected_eligible


def test_per_repo_override_relaxes_gate() -> None:
    """A repo can lower its issue gate below the global default."""
    relaxed = resolve_eligibility(RepoEligibilityConfig(min_valid_solved_issues=1, min_issue_credibility=0.0))
    is_eligible, _, _ = check_issue_eligibility(relaxed, solved_count=1, valid_solved_count=1, closed_count=5)
    assert is_eligible is True
