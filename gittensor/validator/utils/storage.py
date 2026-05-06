from dataclasses import dataclass
from datetime import datetime, timezone
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
        self.db_connection = create_database_connection()
        self.repo = Repository(self.db_connection) if self.db_connection else None
        self.logger = bt.logging

    def is_enabled(self) -> bool:
        return self.db_connection is not None

    @staticmethod
    def _require_success(write_name: str, succeeded: bool) -> None:
        if not succeeded:
            raise RuntimeError(f'{write_name} write failed')

    @staticmethod
    def _require_bulk_success(write_name: str, item_count: int, stored_count: int) -> None:
        if item_count > 0 and not stored_count:
            raise RuntimeError(f'{write_name} bulk write failed for {item_count} item(s)')

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

            merged_pull_requests = miner_eval.merged_pull_requests + _adapt_mirror(miner_eval.mirror_merged_prs)
            open_pull_requests = miner_eval.open_pull_requests + _adapt_mirror(miner_eval.mirror_open_prs)
            closed_pull_requests = miner_eval.closed_pull_requests + _adapt_mirror(miner_eval.mirror_closed_prs)
            issues = miner_eval.get_all_issues()
            file_changes = miner_eval.get_all_file_changes()

            with self.db_connection.pipeline():
                miner_stored = self.repo.set_miner(miner, commit=False)
                result.stored_counts['miners'] = 1 if miner_stored else 0
                self._require_success('miner', miner_stored)

                result.stored_counts['merged_pull_requests'] = self.repo.store_pull_requests_bulk(
                    merged_pull_requests, commit=False
                )
                self._require_bulk_success(
                    'merged pull requests', len(merged_pull_requests), result.stored_counts['merged_pull_requests']
                )
                result.stored_counts['open_pull_requests'] = self.repo.store_pull_requests_bulk(
                    open_pull_requests, commit=False
                )
                self._require_bulk_success(
                    'open pull requests', len(open_pull_requests), result.stored_counts['open_pull_requests']
                )
                result.stored_counts['closed_pull_requests'] = self.repo.store_pull_requests_bulk(
                    closed_pull_requests, commit=False
                )
                self._require_bulk_success(
                    'closed pull requests', len(closed_pull_requests), result.stored_counts['closed_pull_requests']
                )
                result.stored_counts['stale_closed_pull_requests'] = self.repo.store_pull_requests_bulk(
                    miner_eval.stale_closed_pull_requests, commit=False
                )
                self._require_bulk_success(
                    'stale closed pull requests',
                    len(miner_eval.stale_closed_pull_requests),
                    result.stored_counts['stale_closed_pull_requests'],
                )
                result.stored_counts['issues'] = self.repo.store_issues_bulk(issues, commit=False)
                self._require_bulk_success('issues', len(issues), result.stored_counts['issues'])
                result.stored_counts['file_changes'] = self.repo.store_file_changes_bulk(file_changes, commit=False)
                self._require_bulk_success('file changes', len(file_changes), result.stored_counts['file_changes'])
                self._require_success(
                    'stale miner cleanup', self.repo.cleanup_stale_miner_data(miner_eval, commit=False)
                )

                evaluation_stored = self.repo.set_miner_evaluation(miner_eval, commit=False)
                result.stored_counts['evaluations'] = 1 if evaluation_stored else 0
                self._require_success('miner evaluation', evaluation_stored)

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
