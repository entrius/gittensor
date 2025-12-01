# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for scoring module

Tests the core scoring logic including:
- Issue multiplier calculation
- Time decay calculation
- PR spam penalty calculation
- Repository contributor counting
- Issue validation logic
"""

import math
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from gittensor.classes import Issue, PullRequest, MinerEvaluation
from gittensor.validator.evaluation.scoring import (
    calculate_issue_multiplier,
    calculate_time_decay_multiplier,
    calculate_pr_spam_penalty_multiplier,
    count_repository_contributors,
    _is_valid_issue,
)
from gittensor.constants import (
    MAX_ISSUE_AGE_FOR_MAX_SCORE,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    TIME_DECAY_SIGMOID_MIDPOINT,
    EXCESSIVE_PR_PENALTY_THRESHOLD,
    EXCESSIVE_PR_PENALTY_SLOPE,
    EXCESSIVE_PR_MIN_WEIGHT,
)


class TestIssueMultiplierCalculation(unittest.TestCase):
    """Test suite for issue multiplier calculation"""

    def setUp(self):
        """Set up test fixtures"""
        self.now = datetime.now(timezone.utc)
        self.pr = Mock(spec=PullRequest)
        self.pr.number = 123
        self.pr.author_login = "test_author"
        self.pr.created_at = self.now - timedelta(days=10)
        self.pr.merged_at = self.now - timedelta(days=1)

    def test_no_issues_returns_base_multiplier(self):
        """Test that PRs with no issues get 1.0 multiplier"""
        self.pr.issues = []
        result = calculate_issue_multiplier(self.pr)
        self.assertEqual(result, 1.0)

    def test_none_issues_returns_base_multiplier(self):
        """Test that PRs with None issues get 1.0 multiplier"""
        self.pr.issues = None
        result = calculate_issue_multiplier(self.pr)
        self.assertEqual(result, 1.0)

    def test_single_new_issue_low_multiplier(self):
        """Test that newly created issues get low multiplier"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "other_author"
        # Issue created before PR, closed near PR merge
        issue.created_at = self.now - timedelta(days=15)
        issue.closed_at = self.pr.merged_at  # Closed same time as PR merged
        
        self.pr.issues = [issue]
        result = calculate_issue_multiplier(self.pr)
        
        # Should be > 1.0 but < 1.7 for a 14-day old issue (15 days created - 1 day merged)
        self.assertGreater(result, 1.0)
        self.assertLess(result, 1.7)

    def test_single_old_issue_high_multiplier(self):
        """Test that old issues (>45 days) get maximum multiplier"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "other_author"
        issue.created_at = self.now - timedelta(days=50)
        issue.closed_at = self.now
        
        self.pr.issues = [issue]
        result = calculate_issue_multiplier(self.pr)
        
        # Should be close to 1.9 (1.0 + 0.9 for max age issue)
        self.assertGreater(result, 1.8)
        self.assertLess(result, 2.0)

    def test_multiple_issues_additive_multiplier(self):
        """Test that multiple issues add their multipliers"""
        issues = []
        for i in range(3):
            issue = Mock(spec=Issue)
            issue.number = i + 1
            issue.state = 'CLOSED'
            issue.author_login = "other_author"
            issue.created_at = self.now - timedelta(days=30)
            issue.closed_at = self.now
            issues.append(issue)
        
        self.pr.issues = issues
        result = calculate_issue_multiplier(self.pr)
        
        # With 3 issues of 30 days each, should be > 2.0
        self.assertGreater(result, 2.0)

    def test_max_three_issues_counted(self):
        """Test that only first 3 issues are counted"""
        issues = []
        for i in range(5):
            issue = Mock(spec=Issue)
            issue.number = i + 1
            issue.state = 'CLOSED'
            issue.author_login = "other_author"
            issue.created_at = self.now - timedelta(days=30)
            issue.closed_at = self.now
            issues.append(issue)
        
        self.pr.issues = issues
        result = calculate_issue_multiplier(self.pr)
        
        # Should be same as 3 issues (4th and 5th ignored)
        self.pr.issues = issues[:3]
        result_three = calculate_issue_multiplier(self.pr)
        
        self.assertEqual(result, result_three)

    def test_issue_without_dates_gets_default_score(self):
        """Test that issues without dates get default 0.1 multiplier"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "other_author"
        issue.created_at = None
        issue.closed_at = None
        
        self.pr.issues = [issue]
        result = calculate_issue_multiplier(self.pr)
        
        # Should be 1.0 + 0.1 = 1.1
        self.assertAlmostEqual(result, 1.1, places=1)

    def test_issue_age_capped_at_max(self):
        """Test that issue age is capped at MAX_ISSUE_AGE_FOR_MAX_SCORE"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "other_author"
        issue.created_at = self.now - timedelta(days=100)  # Way over max
        issue.closed_at = self.now
        
        self.pr.issues = [issue]
        result_100 = calculate_issue_multiplier(self.pr)
        
        # Compare with exactly MAX_ISSUE_AGE_FOR_MAX_SCORE days
        issue.created_at = self.now - timedelta(days=MAX_ISSUE_AGE_FOR_MAX_SCORE)
        result_max = calculate_issue_multiplier(self.pr)
        
        # Should be equal (both capped)
        self.assertAlmostEqual(result_100, result_max, places=2)


class TestIssueValidation(unittest.TestCase):
    """Test suite for issue validation logic"""

    def setUp(self):
        """Set up test fixtures"""
        self.now = datetime.now(timezone.utc)
        self.pr = Mock(spec=PullRequest)
        self.pr.number = 123
        self.pr.author_login = "pr_author"
        self.pr.created_at = self.now - timedelta(days=10)
        self.pr.merged_at = self.now - timedelta(days=1)

    def test_valid_issue_returns_true(self):
        """Test that a valid issue passes validation"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "issue_author"
        issue.created_at = self.now - timedelta(days=20)
        issue.closed_at = self.now - timedelta(days=1)
        
        result = _is_valid_issue(issue, self.pr)
        self.assertTrue(result)

    def test_open_issue_invalid(self):
        """Test that open issues are invalid"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'OPEN'
        issue.author_login = "issue_author"
        issue.created_at = self.now - timedelta(days=20)
        issue.closed_at = None
        
        result = _is_valid_issue(issue, self.pr)
        self.assertFalse(result)

    def test_issue_without_author_invalid(self):
        """Test that issues without author are invalid"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = None
        issue.created_at = self.now - timedelta(days=20)
        issue.closed_at = self.now
        
        result = _is_valid_issue(issue, self.pr)
        self.assertFalse(result)

    def test_self_created_issue_invalid(self):
        """Test that self-created issues are invalid (gaming prevention)"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "pr_author"  # Same as PR author
        issue.created_at = self.now - timedelta(days=20)
        issue.closed_at = self.now
        
        result = _is_valid_issue(issue, self.pr)
        self.assertFalse(result)

    def test_issue_created_after_pr_invalid(self):
        """Test that issues created after PR are invalid"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "issue_author"
        issue.created_at = self.now  # After PR creation
        issue.closed_at = self.now + timedelta(days=1)
        
        result = _is_valid_issue(issue, self.pr)
        self.assertFalse(result)

    def test_issue_closed_too_far_from_pr_merge_invalid(self):
        """Test that issues closed too far from PR merge are invalid"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "issue_author"
        issue.created_at = self.now - timedelta(days=30)
        issue.closed_at = self.now - timedelta(days=MAX_ISSUE_CLOSE_WINDOW_DAYS + 2)
        
        result = _is_valid_issue(issue, self.pr)
        self.assertFalse(result)

    def test_issue_closed_within_window_valid(self):
        """Test that issues closed within window are valid"""
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "issue_author"
        issue.created_at = self.now - timedelta(days=30)
        issue.closed_at = self.pr.merged_at - timedelta(days=MAX_ISSUE_CLOSE_WINDOW_DAYS - 1)
        
        result = _is_valid_issue(issue, self.pr)
        self.assertTrue(result)


class TestTimeDecayMultiplier(unittest.TestCase):
    """Test suite for time decay multiplier calculation"""

    def test_recent_pr_no_decay(self):
        """Test that PRs within grace period have no decay"""
        pr = Mock(spec=PullRequest)
        pr.merged_at = datetime.now(timezone.utc) - timedelta(hours=2)
        
        result = calculate_time_decay_multiplier(pr)
        self.assertEqual(result, 1.0)

    def test_pr_within_grace_period_no_decay(self):
        """Test PR within grace period has no decay"""
        pr = Mock(spec=PullRequest)
        # Well within the grace period
        pr.merged_at = datetime.now(timezone.utc) - timedelta(hours=TIME_DECAY_GRACE_PERIOD_HOURS - 1)
        
        result = calculate_time_decay_multiplier(pr)
        self.assertEqual(result, 1.0)

    def test_pr_just_after_grace_period_has_decay(self):
        """Test that PR just after grace period starts decaying"""
        pr = Mock(spec=PullRequest)
        pr.merged_at = datetime.now(timezone.utc) - timedelta(hours=TIME_DECAY_GRACE_PERIOD_HOURS + 1)
        
        result = calculate_time_decay_multiplier(pr)
        self.assertLess(result, 1.0)
        self.assertGreater(result, 0.5)

    def test_pr_at_sigmoid_midpoint(self):
        """Test PR at sigmoid midpoint (50% decay)"""
        pr = Mock(spec=PullRequest)
        pr.merged_at = datetime.now(timezone.utc) - timedelta(days=TIME_DECAY_SIGMOID_MIDPOINT)
        
        result = calculate_time_decay_multiplier(pr)
        # At midpoint, sigmoid should be ~0.5
        self.assertGreater(result, 0.4)
        self.assertLess(result, 0.6)

    def test_very_old_pr_minimum_decay(self):
        """Test that very old PRs hit minimum multiplier"""
        pr = Mock(spec=PullRequest)
        pr.merged_at = datetime.now(timezone.utc) - timedelta(days=30)
        
        result = calculate_time_decay_multiplier(pr)
        # Should be at or near minimum
        self.assertLessEqual(result, 0.02)

    def test_decay_is_monotonic(self):
        """Test that decay increases monotonically with time"""
        pr = Mock(spec=PullRequest)
        now = datetime.now(timezone.utc)
        
        results = []
        for days in [5, 6, 7, 8, 9, 10]:
            pr.merged_at = now - timedelta(days=days)
            results.append(calculate_time_decay_multiplier(pr))
        
        # Each result should be less than or equal to previous
        for i in range(len(results) - 1):
            self.assertLessEqual(results[i + 1], results[i])


class TestPRSpamPenalty(unittest.TestCase):
    """Test suite for PR spam penalty calculation"""

    def test_no_penalty_below_threshold(self):
        """Test no penalty when below threshold"""
        result = calculate_pr_spam_penalty_multiplier(10)
        self.assertEqual(result, 1.0)

    def test_no_penalty_at_threshold(self):
        """Test no penalty at exact threshold"""
        result = calculate_pr_spam_penalty_multiplier(EXCESSIVE_PR_PENALTY_THRESHOLD)
        self.assertEqual(result, 1.0)

    def test_penalty_one_over_threshold(self):
        """Test penalty for one PR over threshold"""
        result = calculate_pr_spam_penalty_multiplier(EXCESSIVE_PR_PENALTY_THRESHOLD + 1)
        expected = 1.0 - EXCESSIVE_PR_PENALTY_SLOPE
        self.assertAlmostEqual(result, expected, places=4)

    def test_penalty_increases_linearly(self):
        """Test that penalty increases linearly"""
        result_13 = calculate_pr_spam_penalty_multiplier(13)
        result_14 = calculate_pr_spam_penalty_multiplier(14)
        
        diff = result_13 - result_14
        self.assertAlmostEqual(diff, EXCESSIVE_PR_PENALTY_SLOPE, places=4)

    def test_penalty_has_minimum_floor(self):
        """Test that penalty doesn't go below minimum"""
        result = calculate_pr_spam_penalty_multiplier(1000)
        self.assertEqual(result, EXCESSIVE_PR_MIN_WEIGHT)

    def test_penalty_reaches_minimum_at_expected_count(self):
        """Test penalty reaches minimum at calculated threshold"""
        # Calculate when penalty should hit minimum
        # 1.0 - (count - threshold) * slope = min_weight
        # count = threshold + (1.0 - min_weight) / slope
        expected_count = EXCESSIVE_PR_PENALTY_THRESHOLD + int((1.0 - EXCESSIVE_PR_MIN_WEIGHT) / EXCESSIVE_PR_PENALTY_SLOPE)
        
        result = calculate_pr_spam_penalty_multiplier(expected_count + 1)
        self.assertEqual(result, EXCESSIVE_PR_MIN_WEIGHT)


class TestRepositoryContributorCounting(unittest.TestCase):
    """Test suite for repository contributor counting"""

    def test_empty_evaluations_returns_empty_dict(self):
        """Test that empty evaluations return empty dict"""
        result = count_repository_contributors({})
        self.assertEqual(result, {})

    def test_single_miner_single_repo(self):
        """Test counting for single miner, single repo"""
        eval1 = Mock(spec=MinerEvaluation)
        eval1.unique_repos_contributed_to = {"owner/repo1"}
        
        evaluations = {1: eval1}
        result = count_repository_contributors(evaluations)
        
        self.assertEqual(result, {"owner/repo1": 1})

    def test_single_miner_multiple_repos(self):
        """Test counting for single miner, multiple repos"""
        eval1 = Mock(spec=MinerEvaluation)
        eval1.unique_repos_contributed_to = {"owner/repo1", "owner/repo2", "owner/repo3"}
        
        evaluations = {1: eval1}
        result = count_repository_contributors(evaluations)
        
        self.assertEqual(result, {
            "owner/repo1": 1,
            "owner/repo2": 1,
            "owner/repo3": 1,
        })

    def test_multiple_miners_same_repo(self):
        """Test counting for multiple miners contributing to same repo"""
        eval1 = Mock(spec=MinerEvaluation)
        eval1.unique_repos_contributed_to = {"owner/repo1"}
        
        eval2 = Mock(spec=MinerEvaluation)
        eval2.unique_repos_contributed_to = {"owner/repo1"}
        
        eval3 = Mock(spec=MinerEvaluation)
        eval3.unique_repos_contributed_to = {"owner/repo1"}
        
        evaluations = {1: eval1, 2: eval2, 3: eval3}
        result = count_repository_contributors(evaluations)
        
        self.assertEqual(result, {"owner/repo1": 3})

    def test_multiple_miners_different_repos(self):
        """Test counting for multiple miners, different repos"""
        eval1 = Mock(spec=MinerEvaluation)
        eval1.unique_repos_contributed_to = {"owner/repo1"}
        
        eval2 = Mock(spec=MinerEvaluation)
        eval2.unique_repos_contributed_to = {"owner/repo2"}
        
        eval3 = Mock(spec=MinerEvaluation)
        eval3.unique_repos_contributed_to = {"owner/repo3"}
        
        evaluations = {1: eval1, 2: eval2, 3: eval3}
        result = count_repository_contributors(evaluations)
        
        self.assertEqual(result, {
            "owner/repo1": 1,
            "owner/repo2": 1,
            "owner/repo3": 1,
        })

    def test_multiple_miners_overlapping_repos(self):
        """Test counting for multiple miners with overlapping repos"""
        eval1 = Mock(spec=MinerEvaluation)
        eval1.unique_repos_contributed_to = {"owner/repo1", "owner/repo2"}
        
        eval2 = Mock(spec=MinerEvaluation)
        eval2.unique_repos_contributed_to = {"owner/repo2", "owner/repo3"}
        
        eval3 = Mock(spec=MinerEvaluation)
        eval3.unique_repos_contributed_to = {"owner/repo1", "owner/repo3"}
        
        evaluations = {1: eval1, 2: eval2, 3: eval3}
        result = count_repository_contributors(evaluations)
        
        self.assertEqual(result, {
            "owner/repo1": 2,
            "owner/repo2": 2,
            "owner/repo3": 2,
        })

    def test_miner_with_no_repos_ignored(self):
        """Test that miners with no repos don't affect counts"""
        eval1 = Mock(spec=MinerEvaluation)
        eval1.unique_repos_contributed_to = {"owner/repo1"}
        
        eval2 = Mock(spec=MinerEvaluation)
        eval2.unique_repos_contributed_to = set()  # No repos
        
        evaluations = {1: eval1, 2: eval2}
        result = count_repository_contributors(evaluations)
        
        self.assertEqual(result, {"owner/repo1": 1})


class TestEdgeCases(unittest.TestCase):
    """Test suite for edge cases and error handling"""

    def test_issue_multiplier_with_invalid_dates(self):
        """Test issue multiplier handles missing date info gracefully"""
        pr = Mock(spec=PullRequest)
        pr.number = 123
        pr.author_login = "test_author"
        pr.created_at = datetime.now(timezone.utc)
        pr.merged_at = datetime.now(timezone.utc)
        
        issue = Mock(spec=Issue)
        issue.number = 1
        issue.state = 'CLOSED'
        issue.author_login = "other_author"
        issue.created_at = None  # Missing date
        issue.closed_at = None  # Missing date
        
        pr.issues = [issue]
        
        # Should not crash, should use default score
        result = calculate_issue_multiplier(pr)
        self.assertIsInstance(result, float)
        self.assertGreater(result, 1.0)

    def test_time_decay_with_future_merge_date(self):
        """Test time decay handles future dates gracefully"""
        pr = Mock(spec=PullRequest)
        pr.merged_at = datetime.now(timezone.utc) + timedelta(days=1)  # Future
        
        # Should not crash
        result = calculate_time_decay_multiplier(pr)
        self.assertIsInstance(result, float)

    def test_pr_spam_penalty_with_negative_count(self):
        """Test PR spam penalty handles negative counts"""
        result = calculate_pr_spam_penalty_multiplier(-5)
        # Should return 1.0 (no penalty for negative)
        self.assertEqual(result, 1.0)

    def test_pr_spam_penalty_with_zero_count(self):
        """Test PR spam penalty with zero open PRs"""
        result = calculate_pr_spam_penalty_multiplier(0)
        self.assertEqual(result, 1.0)


if __name__ == '__main__':
    unittest.main()
