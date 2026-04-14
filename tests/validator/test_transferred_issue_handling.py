# The MIT License (MIT)
# Copyright © 2025 Entrius
# Copyright © 2025 Clawdia (AI Agent)

"""Tests for transferred issue handling in issue discovery scoring.

These tests verify the fix for issues #404 and #405:
- Bug: Transferred issues bypass anti-gaming credibility penalty
- Fix: is_transferred issues count as closed/failed in issue discovery scoring
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from gittensor.classes import Issue, MinerEvaluation, PullRequest
from gittensor.validator.issue_discovery.scoring import (
    calculate_issue_credibility,
    check_issue_eligibility,
    score_discovered_issues,
)


class TestTransferredIssueHandling:
    """Test that transferred issues are properly handled in issue discovery scoring."""

    def test_transferred_issue_counts_as_closed_in_pr_issues(self):
        """Transferred issues from merged PRs should count as closed, not solved."""
        evaluation = MinerEvaluation(
            uid=1,
            hotkey="test_hotkey",
            github_id="12345",
        )

        # Create a transferred issue
        transferred_issue = Issue(
            number=100,
            pr_number=50,
            repository_full_name="test/repo",
            title="Test issue",
            created_at=datetime.now(timezone.utc),
            state="CLOSED",
            author_github_id="12345",
            is_transferred=True,
            closed_at=datetime.now(timezone.utc),
        )

        # Create PR that "closed" the issue (via transfer, not merge)
        pr = PullRequest(
            number=50,
            repository_full_name="test/repo",
            uid=1,
            hotkey="test_hotkey",
            github_id="99999",  # Different from issue author
            title="Fix issue",
            author_login="solver",
            merged_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            pr_state="MERGED",
            additions=100,
            deletions=10,
            commits=1,
            token_score=10,
            base_score=25,
        )
        pr.issues = [transferred_issue]

        evaluation.merged_pull_requests = [pr]

        miner_evaluations = {1: evaluation}
        master_repos = {"test/repo": MagicMock(weight=1.0)}

        # Patch the logging to avoid noise in tests
        with patch('bittensor.logging.info'):
            score_discovered_issues(miner_evaluations, master_repos)

        # Transferred issue should NOT contribute to solved_count - counts as closed
        assert evaluation.total_solved_issues == 0, "Transferred issue should not count as solved"
        assert evaluation.total_closed_issues >= 1, "Transferred issue should count as closed"

    def test_transferred_issue_in_scan_issues_counts_as_closed(self):
        """Transferred issues from repo scan should count as closed."""
        evaluation = MinerEvaluation(
            uid=1,
            hotkey="test_hotkey",
            github_id="12345",
        )

        # Create a transferred issue from scan
        scan_issue = Issue(
            number=200,
            pr_number=0,
            repository_full_name="test/repo",
            title="Scanned issue",
            created_at=datetime.now(timezone.utc),
            state="CLOSED",
            author_github_id="12345",
            is_transferred=True,
            closed_at=datetime.now(timezone.utc),
        )

        miner_evaluations = {1: evaluation}
        master_repos = {"test/repo": MagicMock(weight=1.0)}
        scan_issues = {"12345": [scan_issue]}

        with patch('bittensor.logging.info'):
            score_discovered_issues(miner_evaluations, master_repos, scan_issues)

        # Transferred issue should count as closed
        assert evaluation.total_solved_issues == 0, "Transferred scan issue should not count as solved"
        assert evaluation.total_closed_issues >= 1, "Transferred scan issue should count as closed"

    def test_non_transferred_issue_with_merged_pr_counts_as_solved(self):
        """Non-transferred issues with merged PRs should count as solved."""
        evaluation = MinerEvaluation(
            uid=1,
            hotkey="test_hotkey",
            github_id="12345",
        )

        # Create a non-transferred issue with closed_at (merged PR closed it)
        normal_issue = Issue(
            number=300,
            pr_number=50,
            repository_full_name="test/repo",
            title="Normal issue",
            created_at=datetime.now(timezone.utc),
            state="CLOSED",
            author_github_id="12345",
            is_transferred=False,
            closed_at=datetime.now(timezone.utc),
        )

        pr = PullRequest(
            number=50,
            repository_full_name="test/repo",
            uid=1,
            hotkey="test_hotkey",
            github_id="99999",
            title="Fix issue",
            author_login="solver",
            merged_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            pr_state="MERGED",
            additions=100,
            deletions=10,
            commits=1,
            token_score=10,
            base_score=25,
        )
        pr.issues = [normal_issue]

        evaluation.merged_pull_requests = [pr]

        miner_evaluations = {1: evaluation}
        master_repos = {"test/repo": MagicMock(weight=1.0)}

        with patch('bittensor.logging.info'):
            score_discovered_issues(miner_evaluations, master_repos)

        # Issue should be counted as solved
        assert evaluation.total_solved_issues >= 1, "Non-transferred issue with merged PR should count as solved"


class TestCredibilityCalculation:
    """Test issue credibility calculation with transferred issues."""

    def test_credibility_calculation_basic(self):
        """Basic credibility calculation test."""
        # 3 solved, 1 closed
        # With mulligan=1: adjusted_closed = max(0, 1-1) = 0
        # total = 3 + 0 = 3, credibility = 3/3 = 1.0
        credibility = calculate_issue_credibility(solved_count=3, closed_count=1)
        assert credibility == 1.0

    def test_credibility_with_multiple_closed(self):
        """Credibility with multiple closed issues."""
        # 3 solved, 5 closed
        # adjusted_closed = max(0, 5-1) = 4
        # total = 3 + 4 = 7, credibility = 3/7 ≈ 0.43
        credibility = calculate_issue_credibility(solved_count=3, closed_count=5)
        assert abs(credibility - 3/7) < 0.01

    def test_credibility_all_closed(self):
        """All closed issues = 0 credibility."""
        credibility = calculate_issue_credibility(solved_count=0, closed_count=5)
        assert credibility == 0.0

    def test_eligibility_with_low_credibility(self):
        """Eligibility fails when credibility is below threshold."""
        # With many closed issues (transferred), credibility drops
        is_eligible, credibility, reason = check_issue_eligibility(
            solved_count=7,
            closed_count=10,
        )
        assert is_eligible is False
        assert credibility < 0.80  # Below MIN_ISSUE_CREDIBILITY


class TestIsTransferredCheck:
    """Unit tests for the is_transferred check logic."""

    def test_issue_is_transferred_attribute_exists(self):
        """Issue class should have is_transferred attribute."""
        issue = Issue(
            number=1,
            pr_number=0,
            repository_full_name="test/repo",
            title="Test",
            created_at=datetime.now(timezone.utc),
            is_transferred=True,
        )
        assert hasattr(issue, 'is_transferred')
        assert issue.is_transferred is True

    def test_issue_is_transferred_default_false(self):
        """Issue is_transferred should default to False."""
        issue = Issue(
            number=1,
            pr_number=0,
            repository_full_name="test/repo",
            title="Test",
            created_at=datetime.now(timezone.utc),
        )
        assert issue.is_transferred is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])