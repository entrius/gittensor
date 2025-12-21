#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
PR Scoring Simulation Test

This test allows you to test the PR scoring logic in isolation using mock PR and file change data.
Instead of querying live GitHub APIs, you provide mock PullRequest objects with mock file changes
and test the calculate_score_from_file_changes() function directly.

This is useful for:
- Testing edge cases with specific file configurations
- Debugging scoring logic without API calls
- Fast iteration on scoring algorithm changes
- Unit testing PR scoring behavior with different language weights

Usage:
    1. Define mock test cases in mock_prs.py
    2. Run this test to see how those PRs are scored
    3. Set breakpoints in calculate_score_from_file_changes() to debug scoring logic
"""

import os
import sys

import bittensor as bt
from gittensor.classes import PullRequest
from gittensor.validator.utils.load_weights import load_programming_language_weights

# Add the project root to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from gittensor.classes import MinerEvaluation
from gittensor.validator.test.simulation.mock_prs import get_mock_test_cases
from gittensor.validator.evaluation.scoring import calculate_issue_multiplier

def test_pr_scoring():
    """
    Test PR scoring with mock data.

    This function:
    1. Loads mock test cases from mock_prs.py
    2. For each PR in each test case:
       - Calculates base score from file changes
       - Applies issue bonus
       - Adds to MinerEvaluation
    3. Applies penalties and calculates totals
    4. Displays results and validates against expected values
    """

    programming_languages = load_programming_language_weights()

    bt.logging.info("=" * 70)
    bt.logging.info("PR SCORING SIMULATION TEST")
    bt.logging.info("=" * 70)

    # Load mock test cases
    test_cases = get_mock_test_cases()

    for test_case in test_cases:
        bt.logging.info("\n" + "-" * 70)
        bt.logging.info(f"Test Case: {test_case['name']}")
        bt.logging.info("-" * 70)

        # Create a fresh MinerEvaluation for this test case
        miner_eval = MinerEvaluation(
            uid=test_case.get('uid', 0), hotkey=test_case['hotkey']  # Default to 0 if not specified
        )
        miner_eval.github_id = test_case['github_id']

        bt.logging.info(f"GitHub ID: {miner_eval.github_id}")
        bt.logging.info(f"Number of PRs: {len(test_case['prs'])}")

        # Process each PR
        for pr_data in test_case['prs']:
            pr: PullRequest = pr_data['pr']
            file_changes = pr_data['file_changes']
            pr.set_file_changes(file_changes)

            bt.logging.info(f"\n  Processing PR #{pr.number} ({pr.repository_full_name}):")
            bt.logging.info(f"    File changes: {len(file_changes)}")

            base_score = pr.calculate_score_from_file_changes(programming_languages)
            bt.logging.info(f"Base score: {base_score:.2f}")

            # Set all multipliers before calculating final earned score
            pr.base_score = base_score
            pr.issue_multiplier = calculate_issue_multiplier(pr)
            pr.open_pr_spam_multiplier = 1.0  # No spam penalty in test
            pr.time_decay_multiplier = 1.0  # No time decay in test

            # Calculate final earned score using all multipliers
            pr.calculate_final_earned_score()
            bt.logging.info(f"Final score (after multipliers): {pr.earned_score:.2f}")

            # Set file changes and score on PR

            # Add to evaluation
            miner_eval.add_merged_pull_request(pr)

        # Calculate totals and apply penalties
        miner_eval.total_open_prs = test_case.get('total_open_prs', 0)

        # Display expected vs actual if provided
        if 'expected_score' in test_case and test_case['expected_score'] is not None:
            expected = test_case['expected_score']
            actual = miner_eval.total_score
            diff = abs(expected - actual)
            status = "✓ PASS" if diff < 0.01 else "✗ FAIL"
            bt.logging.info(f"\n  Expected Score: {expected:.2f}")
            bt.logging.info(f"  Actual Score:   {actual:.2f}")
            bt.logging.info(f"  Difference:     {diff:.2f}")
            bt.logging.info(f"  Status: {status}")

    bt.logging.info("\n" + "=" * 70)
    bt.logging.info("SIMULATION TEST COMPLETED")
    bt.logging.info("=" * 70)


if __name__ == "__main__":
    """
    Run the PR scoring simulation test.

    Debug workflow:
        1. Add/modify test cases in mock_prs.py
        2. Set breakpoints in score_pull_requests()
        3. Run this script to test PR scoring logic
    """

    try:
        test_pr_scoring()
    except KeyboardInterrupt:
        bt.logging.info("Test interrupted by user")
        sys.exit(0)
    except Exception as e:
        bt.logging.error(f"Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
