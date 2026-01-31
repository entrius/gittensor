# The MIT License (MIT)
# Copyright 2025 Entrius

"""Issue Bounties sub-mechanism for Gittensor validator (v0 - no competitions)."""

from .contract_client import (
    ContractIssue,
    IssueCompetitionContractClient,
    IssueStatus,
)
from .forward import (
    forward_issue_bounties,
)

__all__ = [
    # Forward pass
    'forward_issue_bounties',
    # Contract client
    'IssueCompetitionContractClient',
    'ContractIssue',
    'IssueStatus',
]
