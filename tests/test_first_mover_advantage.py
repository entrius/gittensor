#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for first-mover advantage scoring mechanism.

Tests the apply_first_mover_advantage() function to ensure:
- First contributor to a repo gets 1.0x multiplier
- Subsequent contributors get 0.1x multiplier
- Tiebreaking works correctly (earliest timestamp, then lower UID)
- Multiple PRs by the same miner are handled correctly
"""

import unittest
from datetime import datetime, timezone
from typing import Dict

from gittensor.classes import FileChange, MinerEvaluation, PullRequest
from gittensor.constants import FIRST_MOVER_FOLLOWER_MULTIPLIER
from gittensor.validator.evaluation.scoring import apply_first_mover_advantage


class TestFirstMoverAdvantage(unittest.TestCase):
    """Test cases for first-mover advantage scoring"""

    def setUp(self):
        """Set up common test data"""
        self.base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)

    def create_pr(
        self,
        uid: int,
        repo: str,
        pr_number: int,
        merged_at: datetime,
        base_score: float = 10.0,
    ) -> PullRequest:
        """Helper to create a mock PullRequest"""
        pr = PullRequest(
            number=pr_number,
            repository_full_name=repo,
            uid=uid,
            hotkey=f"hotkey_{uid}",
            github_id=f"github_{uid}",
            title=f"PR {pr_number}",
            author_login=f"user_{uid}",
            merged_at=merged_at,
            created_at=merged_at,
            additions=100,
            deletions=50,
        )
        pr.set_earned_score(base_score)
        return pr

    def test_single_contributor_unchanged(self):
        """Test that a single contributor to a repo keeps their score unchanged"""
        miner_evals = {
            1: MinerEvaluation(uid=1, hotkey="hotkey_1", github_id="github_1"),
        }

        pr = self.create_pr(1, "owner/repo1", 100, self.base_time, base_score=10.0)
        miner_evals[1].add_pull_request(pr)

        apply_first_mover_advantage(miner_evals)

        # Score should remain unchanged (1.0x multiplier)
        self.assertEqual(pr.earned_score, 10.0)

    def test_first_mover_vs_follower(self):
        """Test that first mover gets 1.0x and follower gets 0.1x"""
        miner_evals = {
            1: MinerEvaluation(uid=1, hotkey="hotkey_1", github_id="github_1"),
            2: MinerEvaluation(uid=2, hotkey="hotkey_2", github_id="github_2"),
        }

        # UID 1 merges first
        pr1 = self.create_pr(1, "owner/repo1", 100, self.base_time, base_score=10.0)
        # UID 2 merges 5 days later
        pr2 = self.create_pr(
            2,
            "owner/repo1",
            101,
            datetime(2025, 1, 6, tzinfo=timezone.utc),
            base_score=10.0,
        )

        miner_evals[1].add_pull_request(pr1)
        miner_evals[2].add_pull_request(pr2)

        apply_first_mover_advantage(miner_evals)

        # First mover should keep full score
        self.assertEqual(pr1.earned_score, 10.0)
        # Follower should get reduced score
        self.assertAlmostEqual(pr2.earned_score, 10.0 * FIRST_MOVER_FOLLOWER_MULTIPLIER)

    def test_multiple_repos_independent(self):
        """Test that first-mover status is per-repository"""
        miner_evals = {
            1: MinerEvaluation(uid=1, hotkey="hotkey_1", github_id="github_1"),
            2: MinerEvaluation(uid=2, hotkey="hotkey_2", github_id="github_2"),
        }

        # UID 1 is first to repo1, UID 2 is first to repo2
        pr1_repo1 = self.create_pr(1, "owner/repo1", 100, self.base_time, base_score=10.0)
        pr2_repo2 = self.create_pr(2, "owner/repo2", 101, self.base_time, base_score=10.0)

        # UID 1 is follower to repo2, UID 2 is follower to repo1
        pr1_repo2 = self.create_pr(
            1,
            "owner/repo2",
            102,
            datetime(2025, 1, 6, tzinfo=timezone.utc),
            base_score=10.0,
        )
        pr2_repo1 = self.create_pr(
            2,
            "owner/repo1",
            103,
            datetime(2025, 1, 6, tzinfo=timezone.utc),
            base_score=10.0,
        )

        miner_evals[1].add_pull_request(pr1_repo1)
        miner_evals[1].add_pull_request(pr1_repo2)
        miner_evals[2].add_pull_request(pr2_repo2)
        miner_evals[2].add_pull_request(pr2_repo1)

        apply_first_mover_advantage(miner_evals)

        # UID 1: first to repo1, follower to repo2
        self.assertEqual(pr1_repo1.earned_score, 10.0)  # First mover
        self.assertAlmostEqual(pr1_repo2.earned_score, 1.0)  # Follower

        # UID 2: first to repo2, follower to repo1
        self.assertEqual(pr2_repo2.earned_score, 10.0)  # First mover
        self.assertAlmostEqual(pr2_repo1.earned_score, 1.0)  # Follower

    def test_tiebreaker_by_uid(self):
        """Test that when timestamps are identical, lower UID wins"""
        miner_evals = {
            5: MinerEvaluation(uid=5, hotkey="hotkey_5", github_id="github_5"),
            3: MinerEvaluation(uid=3, hotkey="hotkey_3", github_id="github_3"),
            7: MinerEvaluation(uid=7, hotkey="hotkey_7", github_id="github_7"),
        }

        # All merge at the exact same time
        same_time = self.base_time
        pr5 = self.create_pr(5, "owner/repo1", 100, same_time, base_score=10.0)
        pr3 = self.create_pr(3, "owner/repo1", 101, same_time, base_score=10.0)
        pr7 = self.create_pr(7, "owner/repo1", 102, same_time, base_score=10.0)

        miner_evals[5].add_pull_request(pr5)
        miner_evals[3].add_pull_request(pr3)
        miner_evals[7].add_pull_request(pr7)

        apply_first_mover_advantage(miner_evals)

        # UID 3 should be first mover (lowest UID)
        self.assertEqual(pr3.earned_score, 10.0)
        # Others should be followers
        self.assertAlmostEqual(pr5.earned_score, 1.0)
        self.assertAlmostEqual(pr7.earned_score, 1.0)

    def test_multiple_prs_same_miner_same_repo(self):
        """Test that first mover gets 1.0x on all their PRs to the same repo"""
        miner_evals = {
            1: MinerEvaluation(uid=1, hotkey="hotkey_1", github_id="github_1"),
            2: MinerEvaluation(uid=2, hotkey="hotkey_2", github_id="github_2"),
        }

        # UID 1 has 3 PRs to repo1, all at different times
        pr1_first = self.create_pr(1, "owner/repo1", 100, self.base_time, base_score=10.0)
        pr1_second = self.create_pr(
            1,
            "owner/repo1",
            101,
            datetime(2025, 1, 5, tzinfo=timezone.utc),
            base_score=15.0,
        )
        pr1_third = self.create_pr(
            1,
            "owner/repo1",
            102,
            datetime(2025, 1, 10, tzinfo=timezone.utc),
            base_score=20.0,
        )

        # UID 2 has 1 PR to repo1, after UID 1's first PR
        pr2 = self.create_pr(
            2,
            "owner/repo1",
            103,
            datetime(2025, 1, 7, tzinfo=timezone.utc),
            base_score=10.0,
        )

        miner_evals[1].add_pull_request(pr1_first)
        miner_evals[1].add_pull_request(pr1_second)
        miner_evals[1].add_pull_request(pr1_third)
        miner_evals[2].add_pull_request(pr2)

        apply_first_mover_advantage(miner_evals)

        # All of UID 1's PRs should keep full score (they were first)
        self.assertEqual(pr1_first.earned_score, 10.0)
        self.assertEqual(pr1_second.earned_score, 15.0)
        self.assertEqual(pr1_third.earned_score, 20.0)

        # UID 2's PR should be reduced
        self.assertAlmostEqual(pr2.earned_score, 1.0)

    def test_three_miners_cascade(self):
        """Test that only the first miner gets 1.0x, all others get 0.1x"""
        miner_evals = {
            1: MinerEvaluation(uid=1, hotkey="hotkey_1", github_id="github_1"),
            2: MinerEvaluation(uid=2, hotkey="hotkey_2", github_id="github_2"),
            3: MinerEvaluation(uid=3, hotkey="hotkey_3", github_id="github_3"),
        }

        # Three miners contribute to same repo at different times
        pr1 = self.create_pr(1, "owner/repo1", 100, self.base_time, base_score=10.0)
        pr2 = self.create_pr(
            2,
            "owner/repo1",
            101,
            datetime(2025, 1, 5, tzinfo=timezone.utc),
            base_score=10.0,
        )
        pr3 = self.create_pr(
            3,
            "owner/repo1",
            102,
            datetime(2025, 1, 10, tzinfo=timezone.utc),
            base_score=10.0,
        )

        miner_evals[1].add_pull_request(pr1)
        miner_evals[2].add_pull_request(pr2)
        miner_evals[3].add_pull_request(pr3)

        apply_first_mover_advantage(miner_evals)

        # Only first gets full score
        self.assertEqual(pr1.earned_score, 10.0)
        # Both followers get reduced score
        self.assertAlmostEqual(pr2.earned_score, 1.0)
        self.assertAlmostEqual(pr3.earned_score, 1.0)

    def test_empty_evaluations(self):
        """Test that function handles empty evaluations gracefully"""
        miner_evals: Dict[int, MinerEvaluation] = {}

        # Should not raise any errors
        apply_first_mover_advantage(miner_evals)

    def test_miner_with_no_prs(self):
        """Test that miners with no PRs are handled correctly"""
        miner_evals = {
            1: MinerEvaluation(uid=1, hotkey="hotkey_1", github_id="github_1"),
            2: MinerEvaluation(uid=2, hotkey="hotkey_2", github_id="github_2"),
        }

        # Only UID 2 has PRs
        pr2 = self.create_pr(2, "owner/repo1", 100, self.base_time, base_score=10.0)
        miner_evals[2].add_pull_request(pr2)

        # Should not raise errors
        apply_first_mover_advantage(miner_evals)

        # UID 2 should be first mover (only contributor)
        self.assertEqual(pr2.earned_score, 10.0)


if __name__ == "__main__":
    unittest.main()
