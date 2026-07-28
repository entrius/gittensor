# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Click ParamTypes for issue-command CLI inputs (purely syntactic checks)."""

from __future__ import annotations

import click

from gittensor.constants import MAX_ISSUE_ID

from .helpers import MAX_ISSUE_NUMBER


class ContractIssueType(click.IntRange):
    """On-chain contract issue ID (1 .. MAX_ISSUE_ID - 1)."""

    name = 'contract_issue_id'

    def __init__(self) -> None:
        super().__init__(min=1, max=MAX_ISSUE_ID - 1)


class GitHubIssueType(click.IntRange):
    """GitHub issue number (u32-friendly, 1 .. 2**32 - 1)."""

    name = 'github_issue_number'

    def __init__(self) -> None:
        super().__init__(min=1, max=MAX_ISSUE_NUMBER)


# Stateless singletons - one instance per type for the whole CLI
CONTRACT_ISSUE = ContractIssueType()
GITHUB_ISSUE = GitHubIssueType()
