# The MIT License (MIT)
# Copyright 2025 Entrius

"""Issue Competitions sub-mechanism for Gittensor validator."""

from .contract_client import (
    CompetitionProposal,
    CompetitionStatus,
    ContractCompetition,
    ContractIssue,
    IssueCompetitionContractClient,
    IssueStatus,
)
from .elo import (
    CompetitionRecord,
    EloRating,
    calculate_elo_ema,
    get_elo_rankings,
    is_eligible,
)
from .forward import (
    get_elo_ratings_for_miners,
    get_rewards_for_issue_competitions,
)
from .pairing import (
    calculate_pairing_stats,
    find_best_pair_for_issue,
    find_pairs_for_issues,
)
from .solution_detection import (
    SolutionDetectionResult,
    check_external_solution,
    detect_issue_solution,
)
from .emission_harvester import (
    EmissionHarvester,
    HarvestConfig,
    create_harvester_for_validator,
    get_harvest_config,
)

__all__ = [
    # Forward pass
    'get_rewards_for_issue_competitions',
    'get_elo_ratings_for_miners',
    # ELO
    'calculate_elo_ema',
    'EloRating',
    'CompetitionRecord',
    'get_elo_rankings',
    'is_eligible',
    # Contract client
    'IssueCompetitionContractClient',
    'ContractIssue',
    'ContractCompetition',
    'IssueStatus',
    'CompetitionStatus',
    'CompetitionProposal',
    # Pairing
    'find_pairs_for_issues',
    'find_best_pair_for_issue',
    'calculate_pairing_stats',
    # Solution detection
    'detect_issue_solution',
    'check_external_solution',
    'SolutionDetectionResult',
    # Emission harvesting
    'EmissionHarvester',
    'HarvestConfig',
    'create_harvester_for_validator',
    'get_harvest_config',
]
