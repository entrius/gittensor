# The MIT License (MIT)
# Copyright 2025 Entrius

"""Issue Bounties sub-mechanism for Gittensor validator"""

from .constants import (
    ISSUE_BOUNTIES_ENABLED,
    ISSUES_CONTRACT_UID,
    get_contract_address,
    get_ws_endpoint,
)
from .contract_client import (
    ContractIssue,
    IssueCompetitionContractClient,
    IssueStatus,
)
from .forward import (
    forward_issue_bounties,
)

__all__ = [
    # Constants
    'ISSUE_BOUNTIES_ENABLED',
    'ISSUES_CONTRACT_UID',
    'get_contract_address',
    'get_ws_endpoint',
    # Forward pass
    'forward_issue_bounties',
    # Contract client
    'IssueCompetitionContractClient',
    'ContractIssue',
    'IssueStatus',
]
