"""Tests for check_issue_eligibility — the shared helper used by mirror issue
discovery's per-miner gating logic.

Previously part of test_issue_discovery_scoring.py, which tested legacy
timeline-scraping internals that were removed along with scan_closed_issues
and score_discovered_issues.
"""

import pytest

from gittensor.validator.issue_discovery.scoring import check_issue_eligibility


@pytest.mark.parametrize(
    'solved_count,valid_solved_count,closed_count,expected_credibility,expected_eligible',
    [
        # Credibility uses total (10), eligibility gate uses valid (7)
        (10, 7, 2, pytest.approx(10 / 11, abs=1e-3), True),
        # Valid count below threshold → not eligible regardless of credibility
        (10, 6, 2, pytest.approx(10 / 11, abs=1e-3), False),
        # Zero solved → credibility 0, not eligible
        (0, 0, 2, 0.0, False),
        # All solved counts equal (no low-token solvers) → fix is no-op for this case
        (7, 7, 2, pytest.approx(7 / 8, abs=1e-3), True),
    ],
)
def test_check_issue_eligibility_uses_total_for_credibility_valid_for_gate(
    solved_count: int,
    valid_solved_count: int,
    closed_count: int,
    expected_credibility: float,
    expected_eligible: bool,
) -> None:
    is_eligible, credibility, _ = check_issue_eligibility(solved_count, valid_solved_count, closed_count)
    assert credibility == expected_credibility
    assert is_eligible is expected_eligible
