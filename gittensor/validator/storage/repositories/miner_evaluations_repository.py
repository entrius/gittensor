"""
Repository for handling database operations for MinerEvaluation entities
"""
from typing import Optional, List, Dict, Any
from datetime import datetime
from gittensor.classes import MinerEvaluation
from .base_repository import BaseRepository
from ..queries import (
    SET_MINER_EVALUATION
)

class MinerEvaluationsRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

    def _map_to_miner_evaluation(self, row: Dict[str, Any]) -> MinerEvaluation:
        """Map database row to MinerEvaluation object"""
        return MinerEvaluation(
            uid=row['uid'],
            hotkey=row['hotkey'],  # Required field
            id=row['id'],  # Required field
            total_score=float(row['total_score']) if row['total_score'] is not None else 0.0,
            total_lines_changed=row['total_lines_changed'] or 0,
            total_open_prs=row['total_open_prs'] or 0,
            unique_repos_count=row['unique_repos_count'] or 0,
            github_id=row['github_id'],  # Optional
            failed_reason=row['failed_reason'],  # Optional
            evaluation_timestamp=row['evaluation_timestamp'],  # Optional
            stored_total_prs=row['total_prs'] if 'total_prs' in row else None  # Map DB total_prs
        )

    def get_miner_evaluation(self, evaluation_id: int) -> Optional[MinerEvaluation]:
        """
        Get a miner evaluation by its ID

        Args:
            evaluation_id: Primary key of the evaluation

        Returns:
            MinerEvaluation object if found, None otherwise
        """
        return self.query_single((evaluation_id,), self._map_to_miner_evaluation)

    def get_latest_miner_evaluation(self, uid: int, hotkey: str) -> Optional[MinerEvaluation]:
        """
        Get the latest miner evaluation for a specific miner

        Args:
            uid: Miner UID
            hotkey: Miner hotkey

        Returns:
            Latest MinerEvaluation object if found, None otherwise
        """
        return self.query_single((uid, hotkey), self._map_to_miner_evaluation)

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

    def get_evaluations_by_timeframe(self, start_time: datetime, end_time: datetime) -> List[MinerEvaluation]:
        """
        Get all miner evaluations within a specific timeframe

        Args:
            start_time: Start of time range
            end_time: End of time range

        Returns:
            List of MinerEvaluation objects
        """
        return self.query_multiple((start_time, end_time), self._map_to_miner_evaluation)