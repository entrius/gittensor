#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Custom Mock MinerEvaluation Objects for Simulation Testing

Provides a template for custom MinerEvaluation objects to test alongside DB data.
Uncomment and modify the EXAMPLE_MINER template below.
"""

import os
import sys
from typing import Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from gittensor.classes import MinerEvaluation


def get_custom_evaluations() -> Dict[int, MinerEvaluation]:
    """
    Return custom MinerEvaluation objects for testing.
    """
    custom_evaluations = {}

    # =========================================================================
    # EXAMPLE TEMPLATE - Uncomment and modify as needed
    # Shows merged, open, and closed PRs with file changes and issues
    # =========================================================================

    # EXAMPLE_MINER = MinerEvaluation(uid=9001, hotkey="5CustomHotkey...", github_id="custom_user")
    # for tier in TIERS.keys():
    #     EXAMPLE_MINER.stats_by_tier[tier] = TierStats()
    #
    # # --- MERGED PR (earns score) ---
    # merged_pr = PullRequest(
    #     number=1001,
    #     repository_full_name="opentensor/bittensor",  # Must exist in master_repositories.json
    #     uid=9001,
    #     hotkey="5CustomHotkey...",
    #     github_id="custom_user",
    #     title="Add feature",
    #     author_login="custom_user",
    #     merged_at=datetime.now(timezone.utc) - timedelta(days=5),
    #     created_at=datetime.now(timezone.utc) - timedelta(days=7),
    #     pr_state=PRState.MERGED,
    #     additions=150,
    #     deletions=30,
    #     commits=3,
    #     gittensor_tagged=True,
    #     issues=[
    #         Issue(
    #             number=100,
    #             pr_number=1001,
    #             repository_full_name="opentensor/bittensor",
    #             title="Bug report",
    #             created_at=datetime.now(timezone.utc) - timedelta(days=60),
    #             closed_at=datetime.now(timezone.utc) - timedelta(days=5),
    #             author_login="other_user",  # Must differ from PR author
    #             state="CLOSED",
    #         ),
    #     ],
    # )
    # merged_pr.set_file_changes([
    #     FileChange(pr_number=1001, repository_full_name="opentensor/bittensor",
    #                filename="src/feature.py", changes=100, additions=80, deletions=20, status="modified"),
    #     FileChange(pr_number=1001, repository_full_name="opentensor/bittensor",
    #                filename="tests/test_feature.py", changes=80, additions=70, deletions=10, status="added"),
    # ])
    # EXAMPLE_MINER.merged_pull_requests.append(merged_pr)
    # EXAMPLE_MINER.unique_repos_contributed_to.add(merged_pr.repository_full_name)
    #
    # # --- OPEN PR (collateral deduction) ---
    # open_pr = PullRequest(
    #     number=1002,
    #     repository_full_name="opentensor/bittensor",
    #     uid=9001,
    #     hotkey="5CustomHotkey...",
    #     github_id="custom_user",
    #     title="WIP feature",
    #     author_login="custom_user",
    #     merged_at=None,
    #     created_at=datetime.now(timezone.utc) - timedelta(days=3),
    #     pr_state=PRState.OPEN,
    #     additions=50,
    #     deletions=10,
    #     gittensor_tagged=True,
    # )
    # open_pr.set_file_changes([
    #     FileChange(pr_number=1002, repository_full_name="opentensor/bittensor",
    #                filename="src/new.py", changes=60, additions=50, deletions=10, status="added"),
    # ])
    # EXAMPLE_MINER.open_pull_requests.append(open_pr)
    #
    # # --- CLOSED PR (affects credibility) ---
    # closed_pr = PullRequest(
    #     number=1003,
    #     repository_full_name="opentensor/bittensor",
    #     uid=9001,
    #     hotkey="5CustomHotkey...",
    #     github_id="custom_user",
    #     title="Rejected PR",
    #     author_login="custom_user",
    #     merged_at=None,
    #     created_at=datetime.now(timezone.utc) - timedelta(days=10),
    #     pr_state=PRState.CLOSED,
    #     additions=20,
    #     deletions=5,
    # )
    # closed_pr.set_file_changes([
    #     FileChange(pr_number=1003, repository_full_name="opentensor/bittensor",
    #                filename="rejected.py", changes=25, additions=20, deletions=5, status="added"),
    # ])
    # EXAMPLE_MINER.closed_pull_requests.append(closed_pr)
    #
    # custom_evaluations[9001] = EXAMPLE_MINER

    return custom_evaluations
