from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List

import bittensor as bt

from gittensor.classes import Miner, MinerEvaluation
from gittensor.validator.storage.database import create_database_connection
from gittensor.validator.storage.repository import Repository
from gittensor.validator.utils.load_weights import RepositoryConfig


@dataclass
class StorageResult:
    """Result of a storage operation"""

    success: bool
    errors: List[str]
    stored_counts: Dict[str, int]


class DatabaseStorage:
    def __init__(self):
        self.db_connection = create_database_connection()
        self.repo = Repository(self.db_connection) if self.db_connection else None
        self.logger = bt.logging

    def is_enabled(self) -> bool:
        return self.db_connection is not None

    def store_evaluation(
        self, miner_eval: MinerEvaluation, master_repositories: Dict[str, RepositoryConfig]
    ) -> StorageResult:
        """
        Store all evaluation data in an optimized manner with proper error handling.

        Args:
            miner_eval: Complete miner evaluation with all related data
            master_repositories: Master repo registry — one miner_evaluations row per repo

        Returns:
            StorageResult with success status, errors, and counts
        """
        if not self.is_enabled():
            return StorageResult(success=False, errors=['Database storage not enabled'], stored_counts={})

        result = StorageResult(success=True, errors=[], stored_counts={})

        try:
            assert self.db_connection is not None and self.repo is not None
            self.db_connection.autocommit = False

            miner_eval.evaluation_timestamp = datetime.now(timezone.utc)

            miner = Miner(miner_eval.uid, miner_eval.hotkey, miner_eval.github_id or '')

            from gittensor.validator.oss_contributions.mirror.adapters import (
                mirror_scored_pr_to_legacy_pull_request,
            )

            def _adapt_mirror(scored_list):
                return [
                    mirror_scored_pr_to_legacy_pull_request(s, miner_eval.uid, miner_eval.hotkey, miner_eval.github_id)
                    for s in scored_list
                ]

            with self.db_connection.pipeline():
                result.stored_counts['miners'] = self.repo.set_miner(miner, commit=False)
                result.stored_counts['merged_pull_requests'] = self.repo.store_pull_requests_bulk(
                    _adapt_mirror(miner_eval.merged_prs), commit=False
                )
                result.stored_counts['open_pull_requests'] = self.repo.store_pull_requests_bulk(
                    _adapt_mirror(miner_eval.open_prs), commit=False
                )
                result.stored_counts['closed_pull_requests'] = self.repo.store_pull_requests_bulk(
                    _adapt_mirror(miner_eval.closed_prs), commit=False
                )
                result.stored_counts['issues'] = self.repo.store_issues_bulk(miner_eval.get_all_issues(), commit=False)
                result.stored_counts['file_changes'] = self.repo.store_file_changes_bulk(
                    miner_eval.get_all_file_changes(), commit=False
                )
                self.repo.cleanup_stale_miner_data(miner_eval, commit=False)
                result.stored_counts['evaluations'] = (
                    1 if self.repo.set_miner_evaluation(miner_eval, master_repositories, commit=False) else 0
                )

            self.db_connection.commit()
            self.db_connection.autocommit = True

        except Exception as ex:
            if self.db_connection is not None:
                self.db_connection.rollback()
                self.db_connection.autocommit = True

            error_msg = f'Failed to store evaluation data for UID {miner_eval.uid}: {str(ex)}'
            result.success = False
            result.errors.append(error_msg)
            self.logger.error(error_msg)

        return result

    def close(self):
        if self.db_connection:
            self.db_connection.close()
