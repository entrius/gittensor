from dataclasses import dataclass
from typing import Dict, List

import bittensor as bt

from gittensor.classes import Miner, MinerEvaluation
from gittensor.validator.storage.database import create_database_connection
from gittensor.validator.storage.migrator import DatabaseMigrator
from gittensor.validator.storage.repositories.file_changes_repository import FileChangesRepository
from gittensor.validator.storage.repositories.issues_repository import IssuesRepository
from gittensor.validator.storage.repositories.miner_evaluations_repository import MinerEvaluationsRepository
from gittensor.validator.storage.repositories.miners_repository import MinersRepository
from gittensor.validator.storage.repositories.pull_requests_repository import PullRequestsRepository


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

        # Initialize the database
        self.db_migrator = DatabaseMigrator(self.db_connection)
        self.db_migrator.create_tables()

        # Intitialize repositories
        self.miners_repo = MinersRepository(self.db_connection) if self.db_connection else None
        self.evaluations_repo = MinerEvaluationsRepository(self.db_connection) if self.db_connection else None
        self.pr_repo = PullRequestsRepository(self.db_connection) if self.db_connection else None
        self.issues_repo = IssuesRepository(self.db_connection) if self.db_connection else None
        self.file_changes_repo = FileChangesRepository(self.db_connection) if self.db_connection else None
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
            return StorageResult(success=False, errors=["Database storage not enabled"], stored_counts={})

        result = StorageResult(success=True, errors=[], stored_counts={})

        try:
            # Start transaction
            self.db_connection.autocommit = False

            # Store all entities using bulk methods
            miner = Miner(miner_eval.uid, miner_eval.hotkey, miner_eval.github_id)
            pull_requests = miner_eval.pull_requests
            all_issues = miner_eval.get_all_issues()
            all_file_changes = miner_eval.get_all_file_changes()

            result.stored_counts['miners'] = self.miners_repo.set_miner(miner)
            result.stored_counts['pull_requests'] = self.pr_repo.store_pull_requests_bulk(pull_requests)
            result.stored_counts['issues'] = self.issues_repo.store_issues_bulk(all_issues)
            result.stored_counts['file_changes'] = self.file_changes_repo.store_file_changes_bulk(all_file_changes)
            result.stored_counts['evaluations'] = 1 if self.evaluations_repo.set_miner_evaluation(miner_eval) else 0

            # Commit transaction
            self.db_connection.commit()
            self.db_connection.autocommit = True

            self.logger.success(f"Successfully stored evaluation data for UID {miner_eval.uid}")

        except Exception as ex:
            # Rollback transaction
            self.db_connection.rollback()
            self.db_connection.autocommit = True

            error_msg = f"Failed to store evaluation data for UID {miner_eval.uid}: {str(ex)}"
            result.success = False
            result.errors.append(error_msg)
            self.logger.error(error_msg)

        return result

    def _log_storage_summary(self, counts: Dict[str, int]):
        """Log a summary of what was stored"""
        self.logger.info("Storage Summary:")
        for entity_type, count in counts.items():
            if count > 0:
                self.logger.info(f"  - {entity_type}: {count}")

    def close(self):
        if self.db_connection:
            self.db_connection.close()
