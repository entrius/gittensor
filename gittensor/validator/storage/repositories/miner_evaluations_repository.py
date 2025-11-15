"""
Repository for handling database operations for MinerEvaluation entities
"""
from gittensor.classes import MinerEvaluation
from .base_repository import BaseRepository
from ..queries import (
    SET_MINER_EVALUATION
)

class MinerEvaluationsRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

    def set_miner_evaluation(self, evaluation: MinerEvaluation) -> bool:
        """
        Insert a new miner evaluation

        Args:
            evaluation: MinerEvaluation object to store

        Returns:
            True if successful, False otherwise
        """
        query = SET_MINER_EVALUATION
        params = (
            evaluation.uid,
            evaluation.hotkey,
            evaluation.github_id,
            evaluation.failed_reason,
            evaluation.total_score,
            evaluation.total_lines_changed,
            evaluation.total_open_prs,
            evaluation.total_prs,
            evaluation.unique_repos_count
        )
        return self.set_entity(query, params)