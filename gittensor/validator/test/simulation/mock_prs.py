#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Mock PR and File Change Data for Simulation Testing

This file provides test cases with mock PullRequest and FileChange objects.
You can modify these test cases or add new ones to test different scenarios.

Each test case should include:
- name: Description of the test case
- github_id: Mock GitHub username
- hotkey: Mock hotkey
- prs: List of dicts with 'pr' (PullRequest object) and 'file_changes' (List[FileChange])
- total_open_prs: Number of open PRs for spam penalty testing
- expected_score: (Optional) Expected total score for validation
"""

import sys
import os
from datetime import datetime

# Add the project root to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from gittensor.classes import PullRequest, FileChange, Issue


def get_mock_test_cases():
    """
    Returns a list of test cases with mock PR and file change data.

    Each test case is a dictionary with:
    - name: Test case description
    - github_id: Mock GitHub user
    - hotkey: Mock hotkey
    - prs: List of dicts containing 'pr' and 'file_changes'
    - total_open_prs: Number of open PRs (for spam penalty)
    - expected_score: Expected final score (optional)
    """

    test_cases = [
        # Test Case 1: Single Python PR
        {
            'name': 'Single Python File PR',
            'github_id': 'test_user_1',
            'hotkey': '5FakeHotkey1...',
            'prs': [
                {
                    'pr': PullRequest(
                        number=100,
                        repository_full_name='owner/repo',
                        uid=999,
                        hotkey='5FakeHotkey1...',
                        github_id='test_user_1',
                        title='Add feature X',
                        author_login='test_user_1',
                        merged_at=datetime(2025, 1, 1),
                        created_at=datetime(2024, 12, 25),
                        additions=100,
                        deletions=50,
                        commits=3,
                        issues=[]  # No issues
                    ),
                    'file_changes': [
                        FileChange(
                            pr_number=100,
                            repository_full_name='owner/repo',
                            filename='main.py',
                            changes=150,
                            additions=100,
                            deletions=50,
                            status='modified'
                        )
                    ]
                }
            ],
            'total_open_prs': 0,
            'expected_score': None  # Calculate and add if needed
        },

        # Test Case 2: Multiple files with different language weights
        {
            'name': 'Mixed Languages (Python, TypeScript, Markdown)',
            'github_id': 'test_user_2',
            'hotkey': '5FakeHotkey2...',
            'prs': [
                {
                    'pr': PullRequest(
                        number=200,
                        repository_full_name='owner/repo',
                        uid=998,
                        hotkey='5FakeHotkey2...',
                        github_id='test_user_2',
                        title='Full stack changes',
                        author_login='test_user_2',
                        merged_at=datetime(2025, 1, 2),
                        created_at=datetime(2024, 12, 28),
                        additions=300,
                        deletions=100,
                        commits=5,
                        issues=[]
                    ),
                    'file_changes': [
                        FileChange(
                            pr_number=200,
                            repository_full_name='owner/repo',
                            filename='backend/api.py',
                            changes=200,
                            additions=150,
                            deletions=50,
                            status='modified'
                        ),
                        FileChange(
                            pr_number=200,
                            repository_full_name='owner/repo',
                            filename='frontend/app.ts',
                            changes=150,
                            additions=100,
                            deletions=50,
                            status='modified'
                        ),
                        FileChange(
                            pr_number=200,
                            repository_full_name='owner/repo',
                            filename='README.md',
                            changes=50,
                            additions=50,
                            deletions=0,
                            status='modified'
                        )
                    ]
                }
            ],
            'total_open_prs': 0,
            'expected_score': None
        },

        # Test Case 3: PR with issue resolution (should get 1.5x bonus)
        {
            'name': 'PR Solving Issue (1.5x bonus)',
            'github_id': 'test_user_3',
            'hotkey': '5FakeHotkey3...',
            'prs': [
                {
                    'pr': PullRequest(
                        number=300,
                        repository_full_name='owner/repo',
                        uid=997,
                        hotkey='5FakeHotkey3...',
                        github_id='test_user_3',
                        title='Fix critical bug',
                        author_login='test_user_3',
                        merged_at=datetime(2025, 1, 5),
                        created_at=datetime(2025, 1, 4),
                        additions=75,
                        deletions=30,
                        commits=1,
                        issues=[
                            Issue(
                                number=50,
                                pr_number=300,
                                repository_full_name='owner/repo',
                                title='Critical bug',
                                created_at=datetime(2024, 12, 1),
                                closed_at=datetime(2025, 1, 5)
                            )
                        ]
                    ),
                    'file_changes': [
                        FileChange(
                            pr_number=300,
                            repository_full_name='owner/repo',
                            filename='buggy_module.py',
                            changes=105,
                            additions=75,
                            deletions=30,
                            status='modified'
                        )
                    ]
                }
            ],
            'total_open_prs': 0,
            'expected_score': None
        },

        # Test Case 4: Multiple PRs to test aggregation
        {
            'name': 'Multiple PRs - Total Score Aggregation',
            'github_id': 'test_user_4',
            'hotkey': '5FakeHotkey4...',
            'prs': [
                {
                    'pr': PullRequest(
                        number=400,
                        repository_full_name='owner/repo1',
                        uid=996,
                        hotkey='5FakeHotkey4...',
                        github_id='test_user_4',
                        title='First PR',
                        author_login='test_user_4',
                        merged_at=datetime(2025, 1, 10),
                        created_at=datetime(2025, 1, 9),
                        additions=100,
                        deletions=0,
                        commits=2,
                        issues=[]
                    ),
                    'file_changes': [
                        FileChange(
                            pr_number=400,
                            repository_full_name='owner/repo1',
                            filename='feature.py',
                            changes=100,
                            additions=100,
                            deletions=0,
                            status='added'
                        )
                    ]
                },
                {
                    'pr': PullRequest(
                        number=401,
                        repository_full_name='owner/repo2',
                        uid=996,
                        hotkey='5FakeHotkey4...',
                        github_id='test_user_4',
                        title='Second PR',
                        author_login='test_user_4',
                        merged_at=datetime(2025, 1, 11),
                        created_at=datetime(2025, 1, 10),
                        additions=50,
                        deletions=25,
                        commits=1,
                        issues=[]
                    ),
                    'file_changes': [
                        FileChange(
                            pr_number=401,
                            repository_full_name='owner/repo2',
                            filename='utils.js',
                            changes=75,
                            additions=50,
                            deletions=25,
                            status='modified'
                        )
                    ]
                }
            ],
            'total_open_prs': 0,
            'expected_score': None
        },

        # Test Case 5: Test spam penalty with excessive open PRs
        {
            'name': 'Spam Penalty Test (Many Open PRs)',
            'github_id': 'test_user_5',
            'hotkey': '5FakeHotkey5...',
            'prs': [
                {
                    'pr': PullRequest(
                        number=500,
                        repository_full_name='owner/repo',
                        uid=995,
                        hotkey='5FakeHotkey5...',
                        github_id='test_user_5',
                        title='Good PR',
                        author_login='test_user_5',
                        merged_at=datetime(2025, 1, 15),
                        created_at=datetime(2025, 1, 14),
                        additions=200,
                        deletions=100,
                        commits=3,
                        issues=[]
                    ),
                    'file_changes': [
                        FileChange(
                            pr_number=500,
                            repository_full_name='owner/repo',
                            filename='module.py',
                            changes=300,
                            additions=200,
                            deletions=100,
                            status='modified'
                        )
                    ]
                }
            ],
            'total_open_prs': 50,  # Should trigger spam penalty
            'expected_score': None  # Score will be penalized
        },

        # Test Case 6: Empty case
        {
            'name': 'No PRs',
            'github_id': 'test_user_6',
            'hotkey': '5FakeHotkey6...',
            'prs': [],
            'total_open_prs': 0,
            'expected_score': 0.0
        },
    ]

    return test_cases
