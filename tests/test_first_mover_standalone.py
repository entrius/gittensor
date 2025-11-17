#!/usr/bin/env python3
"""
Standalone test for first-mover logic without bittensor dependency.
This creates minimal mock objects to test the core algorithm.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field


# Minimal mocks to avoid bittensor dependency
@dataclass
class MockPullRequest:
    number: int
    repository_full_name: str
    uid: int
    merged_at: datetime
    earned_score: float = 0.0

    def set_earned_score(self, score: float):
        self.earned_score = score


@dataclass
class MockMinerEvaluation:
    uid: int
    pull_requests: List[MockPullRequest] = field(default_factory=list)

    def add_pull_request(self, pr: MockPullRequest):
        self.pull_requests.append(pr)


def apply_first_mover_advantage_standalone(miner_evaluations: Dict[int, MockMinerEvaluation]):
    """Standalone version of first-mover logic for testing"""
    FIRST_MOVER_FOLLOWER_MULTIPLIER = 0.1
    
    # First pass: determine first mover for each repo
    repo_first_mover: Dict[str, tuple[int, datetime]] = {}
    
    for uid, evaluation in miner_evaluations.items():
        if not evaluation or not evaluation.pull_requests:
            continue
        
        for pr in evaluation.pull_requests:
            repo = pr.repository_full_name
            
            if repo not in repo_first_mover:
                repo_first_mover[repo] = (uid, pr.merged_at)
            else:
                current_first_uid, current_earliest_time = repo_first_mover[repo]
                
                if pr.merged_at < current_earliest_time:
                    repo_first_mover[repo] = (uid, pr.merged_at)
                elif pr.merged_at == current_earliest_time and uid < current_first_uid:
                    repo_first_mover[repo] = (uid, pr.merged_at)
    
    # Second pass: apply multipliers
    for uid, evaluation in miner_evaluations.items():
        if not evaluation or not evaluation.pull_requests:
            continue
        
        for pr in evaluation.pull_requests:
            repo = pr.repository_full_name
            first_mover_uid, _ = repo_first_mover.get(repo, (None, None))
            
            if first_mover_uid == uid:
                # First mover - keep 1.0x
                pass
            else:
                # Follower - apply 0.1x
                original_score = pr.earned_score
                pr.set_earned_score(original_score * FIRST_MOVER_FOLLOWER_MULTIPLIER)
    
    return repo_first_mover


def test_basic_first_vs_follower():
    """Test basic first-mover vs follower scenario"""
    print("\n=== Test 1: Basic First vs Follower ===")
    
    miner_evals = {
        1: MockMinerEvaluation(uid=1),
        2: MockMinerEvaluation(uid=2),
    }
    
    # UID 1 merges first
    pr1 = MockPullRequest(100, "owner/repo1", 1, datetime(2025, 1, 1, tzinfo=timezone.utc), 10.0)
    # UID 2 merges later
    pr2 = MockPullRequest(101, "owner/repo1", 2, datetime(2025, 1, 6, tzinfo=timezone.utc), 10.0)
    
    miner_evals[1].add_pull_request(pr1)
    miner_evals[2].add_pull_request(pr2)
    
    apply_first_mover_advantage_standalone(miner_evals)
    
    print(f"UID 1 (first mover): {pr1.earned_score} (expected: 10.0)")
    print(f"UID 2 (follower): {pr2.earned_score} (expected: 1.0)")
    
    assert pr1.earned_score == 10.0, f"Failed: UID 1 should keep 10.0, got {pr1.earned_score}"
    assert abs(pr2.earned_score - 1.0) < 0.01, f"Failed: UID 2 should have 1.0, got {pr2.earned_score}"
    print("[PASSED]")


def test_tiebreaker():
    """Test UID tiebreaker when timestamps match"""
    print("\n=== Test 2: UID Tiebreaker ===")
    
    miner_evals = {
        5: MockMinerEvaluation(uid=5),
        3: MockMinerEvaluation(uid=3),
        7: MockMinerEvaluation(uid=7),
    }
    
    same_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    pr5 = MockPullRequest(100, "owner/repo1", 5, same_time, 10.0)
    pr3 = MockPullRequest(101, "owner/repo1", 3, same_time, 10.0)
    pr7 = MockPullRequest(102, "owner/repo1", 7, same_time, 10.0)
    
    miner_evals[5].add_pull_request(pr5)
    miner_evals[3].add_pull_request(pr3)
    miner_evals[7].add_pull_request(pr7)
    
    repo_first_mover = apply_first_mover_advantage_standalone(miner_evals)
    
    print(f"UID 3 (lowest UID): {pr3.earned_score} (expected: 10.0)")
    print(f"UID 5: {pr5.earned_score} (expected: 1.0)")
    print(f"UID 7: {pr7.earned_score} (expected: 1.0)")
    print(f"First mover determined: UID {repo_first_mover['owner/repo1'][0]}")
    
    assert pr3.earned_score == 10.0, f"Failed: UID 3 should be first mover"
    assert abs(pr5.earned_score - 1.0) < 0.01, f"Failed: UID 5 should be follower"
    assert abs(pr7.earned_score - 1.0) < 0.01, f"Failed: UID 7 should be follower"
    assert repo_first_mover['owner/repo1'][0] == 3, f"Failed: UID 3 should be identified as first mover"
    print("[PASSED]")


def test_multiple_repos():
    """Test per-repository independence"""
    print("\n=== Test 3: Multiple Repositories ===")
    
    miner_evals = {
        1: MockMinerEvaluation(uid=1),
        2: MockMinerEvaluation(uid=2),
    }
    
    # UID 1 first to repo1, UID 2 first to repo2
    pr1_repo1 = MockPullRequest(100, "owner/repo1", 1, datetime(2025, 1, 1, tzinfo=timezone.utc), 10.0)
    pr2_repo2 = MockPullRequest(101, "owner/repo2", 2, datetime(2025, 1, 1, tzinfo=timezone.utc), 10.0)
    
    # UID 1 follower to repo2, UID 2 follower to repo1
    pr1_repo2 = MockPullRequest(102, "owner/repo2", 1, datetime(2025, 1, 6, tzinfo=timezone.utc), 10.0)
    pr2_repo1 = MockPullRequest(103, "owner/repo1", 2, datetime(2025, 1, 6, tzinfo=timezone.utc), 10.0)
    
    miner_evals[1].add_pull_request(pr1_repo1)
    miner_evals[1].add_pull_request(pr1_repo2)
    miner_evals[2].add_pull_request(pr2_repo2)
    miner_evals[2].add_pull_request(pr2_repo1)
    
    apply_first_mover_advantage_standalone(miner_evals)
    
    print(f"UID 1, repo1 (first): {pr1_repo1.earned_score} (expected: 10.0)")
    print(f"UID 1, repo2 (follower): {pr1_repo2.earned_score} (expected: 1.0)")
    print(f"UID 2, repo2 (first): {pr2_repo2.earned_score} (expected: 10.0)")
    print(f"UID 2, repo1 (follower): {pr2_repo1.earned_score} (expected: 1.0)")
    
    assert pr1_repo1.earned_score == 10.0
    assert abs(pr1_repo2.earned_score - 1.0) < 0.01
    assert pr2_repo2.earned_score == 10.0
    assert abs(pr2_repo1.earned_score - 1.0) < 0.01
    print("[PASSED]")


def test_multiple_prs_same_miner():
    """Test that first mover gets 1.0x on all their PRs"""
    print("\n=== Test 4: Multiple PRs by First Mover ===")
    
    miner_evals = {
        1: MockMinerEvaluation(uid=1),
        2: MockMinerEvaluation(uid=2),
    }
    
    # UID 1 has 3 PRs, all to same repo
    pr1_a = MockPullRequest(100, "owner/repo1", 1, datetime(2025, 1, 1, tzinfo=timezone.utc), 10.0)
    pr1_b = MockPullRequest(101, "owner/repo1", 1, datetime(2025, 1, 5, tzinfo=timezone.utc), 15.0)
    pr1_c = MockPullRequest(102, "owner/repo1", 1, datetime(2025, 1, 10, tzinfo=timezone.utc), 20.0)
    
    # UID 2 has 1 PR, after UID 1
    pr2 = MockPullRequest(103, "owner/repo1", 2, datetime(2025, 1, 7, tzinfo=timezone.utc), 10.0)
    
    miner_evals[1].add_pull_request(pr1_a)
    miner_evals[1].add_pull_request(pr1_b)
    miner_evals[1].add_pull_request(pr1_c)
    miner_evals[2].add_pull_request(pr2)
    
    apply_first_mover_advantage_standalone(miner_evals)
    
    print(f"UID 1, PR #100: {pr1_a.earned_score} (expected: 10.0)")
    print(f"UID 1, PR #101: {pr1_b.earned_score} (expected: 15.0)")
    print(f"UID 1, PR #102: {pr1_c.earned_score} (expected: 20.0)")
    print(f"UID 2, PR #103: {pr2.earned_score} (expected: 1.0)")
    
    assert pr1_a.earned_score == 10.0
    assert pr1_b.earned_score == 15.0
    assert pr1_c.earned_score == 20.0
    assert abs(pr2.earned_score - 1.0) < 0.01
    print("[PASSED]")


if __name__ == "__main__":
    print("=" * 70)
    print("STANDALONE FIRST-MOVER ADVANTAGE TESTS")
    print("=" * 70)
    
    try:
        test_basic_first_vs_follower()
        test_tiebreaker()
        test_multiple_repos()
        test_multiple_prs_same_miner()
        
        print("\n" + "=" * 70)
        print("*** ALL TESTS PASSED! ***")
        print("=" * 70)
    except AssertionError as e:
        print(f"\n[FAIL] TEST FAILED: {e}")
        exit(1)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        exit(1)
