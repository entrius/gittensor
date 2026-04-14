# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import Dict

from gittensor.classes import MinerEvaluation
from gittensor.validator.oss_contributions.normalize import _normalize_scores


def normalize_issue_discovery_rewards(miner_evaluations: Dict[int, MinerEvaluation]) -> Dict[int, float]:
    """Normalize issue discovery scores to sum to 1.0."""
    return _normalize_scores(miner_evaluations, lambda e: e.issue_discovery_score, 'Issue discovery')
