from dataclasses import dataclass
from typing import Dict, List

import bittensor as bt

from gittensor.classes import Miner, MinerEvaluation
from gittensor.validator.storage.database import create_database_connection
from gittensor.validator.storage.repository import Repository


@dataclass
class StorageResult:
    """Result of a storage operation"""

    success: bool
    errors: List[str]
    stored_counts: Dict[str, int]


class DatabaseStorage:
    def __init__(self):
        # Instantiate the database connections
        self.db_connection = create_database_connection()
        # Initialize repository
        self.repo = Repository(self.db_connection) if self.db_connection else None
        self.logger = bt.logging

    def is_enabled(self) -> bool:
        return self.db_connection is not None

    def store_evaluation(self, miner_eval: MinerEvaluation) -> StorageResult:
        """
        Store all evaluation data in an optimized manner with proper error handling.

        Args:
            miner_eval: Complete miner evaluation with all related data

        Returns:
            StorageResult with success status, errors, and counts
        """
        if not self.is_enabled():
            return StorageResult(success=False, errors=['Database storage not enabled'], stored_counts={})

        result = StorageResult(success=True, errors=[], stored_counts={})

        try:
            # Start transaction
            self.db_connection.autocommit = False

            # Store all entities using bulk methods
            miner = Miner(miner_eval.uid, miner_eval.hotkey, miner_eval.github_id)

            result.stored_counts['miners'] = self.repo.set_miner(miner)
            result.stored_counts['merged_pull_requests'] = self.repo.store_pull_requests_bulk(
                miner_eval.merged_pull_requests
            )
            result.stored_counts['open_pull_requests'] = self.repo.store_pull_requests_bulk(
                miner_eval.open_pull_requests
            )
            result.stored_counts['closed_pull_requests'] = self.repo.store_pull_requests_bulk(
                miner_eval.closed_pull_requests
            )
            result.stored_counts['issues'] = self.repo.store_issues_bulk(miner_eval.get_all_issues())
            result.stored_counts['file_changes'] = self.repo.store_file_changes_bulk(miner_eval.get_all_file_changes())
            result.stored_counts['evaluations'] = 1 if self.repo.set_miner_evaluation(miner_eval) else 0

            # Commit transaction
            self.db_connection.commit()
            self.db_connection.autocommit = True

        except Exception as ex:
            # Rollback transaction
            self.db_connection.rollback()
            self.db_connection.autocommit = True

            error_msg = f'Failed to store evaluation data for UID {miner_eval.uid}: {str(ex)}'
            result.success = False
            result.errors.append(error_msg)
            self.logger.error(error_msg)

        return result

    def _log_storage_summary(self, counts: Dict[str, int]):
        """Log a summary of what was stored"""
        self.logger.info('Storage Summary:')
        for entity_type, count in counts.items():
            if count > 0:
                self.logger.info(f'  - {entity_type}: {count}')

    def close(self):
        if self.db_connection:
            self.db_connection.close()
