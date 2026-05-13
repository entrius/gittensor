# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Click ParamTypes for issue-command CLI inputs (purely syntactic checks)."""

from __future__ import annotations

import click

from .helpers import MAX_ISSUE_ID, MAX_ISSUE_NUMBER, REPO_PATTERN, validate_ss58_address


class RepoNameType(click.ParamType):
    """Owner/repo string matching ``REPO_PATTERN``."""

    name = 'repo'

    def convert(self, value, param, ctx):
        trimmed = value.strip()
        if not REPO_PATTERN.match(trimmed):
            self.fail(f"'{value}' is not a valid owner/repo", param, ctx)
        return trimmed


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


class SS58AddressType(click.ParamType):
    """SS58 address validated via ``validate_ss58_address``."""

    name = 'ss58'

    def convert(self, value, param, ctx):
        name = (param.name if param else None) or 'address'
        try:
            return validate_ss58_address(value, name)
        except click.BadParameter as exc:
            self.fail(str(exc), param, ctx)


# Stateless singletons - one instance per type for the whole CLI
REPO = RepoNameType()
CONTRACT_ISSUE = ContractIssueType()
GITHUB_ISSUE = GitHubIssueType()
SS58 = SS58AddressType()
