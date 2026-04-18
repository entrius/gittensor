"""
Repository class providing database operations for validator storage.

This module consolidates all database operations into a single Repository class,
providing clean methods for storing miners, pull requests, issues, file changes,
and miner evaluations.
"""

import logging
import time
from contextlib import contextmanager
from typing import List

import numpy as np

from gittensor.classes import FileChange, Issue, Miner, MinerEvaluation, PullRequest

from .queries import (
    BULK_UPSERT_FILE_CHANGES,
    BULK_UPSERT_ISSUES,
    BULK_UPSERT_MINER_EVALUATION,
    BULK_UPSERT_PULL_REQUESTS,
    CLEANUP_STALE_MINER_EVALUATIONS,
    CLEANUP_STALE_MINER_EVALUATIONS_BY_HOTKEY,
    CLEANUP_STALE_MINERS,
    CLEANUP_STALE_MINERS_BY_HOTKEY,
    SET_MINER,
)

try:
    from psycopg2 import InterfaceError, OperationalError

    TRANSIENT_DB_ERRORS = (OperationalError, InterfaceError)
except Exception:  # pragma: no cover - fallback for environments without psycopg2
    TRANSIENT_DB_ERRORS = ()

TRANSIENT_DB_ERROR_NAMES = {'OperationalError', 'InterfaceError'}
MAX_DB_RETRIES = 3
DB_RETRY_BASE_DELAY_SECONDS = 0.5


class BaseRepository:
    """
    Base repository class that handles database connections and provides
    clean query execution methods.
    """

    def __init__(self, db_connection):
        self.db = db_connection
        self.logger = logging.getLogger(self.__class__.__name__)

    @contextmanager
    def get_cursor(self):
        """
        Context manager for database cursor operations.
        Automatically handles cursor cleanup.
        """
        cursor = self.db.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    def _is_transient_db_error(self, error: Exception) -> bool:
        if TRANSIENT_DB_ERRORS and isinstance(error, TRANSIENT_DB_ERRORS):
            return True
        return error.__class__.__name__ in TRANSIENT_DB_ERROR_NAMES

    def _run_with_retry(self, operation, error_context: str, failure_value):
        for attempt in range(1, MAX_DB_RETRIES + 1):
            try:
                result = operation()
                self.db.commit()
                return result
            except Exception as e:
                self.db.rollback()
                is_retryable = self._is_transient_db_error(e)
                has_retry_left = attempt < MAX_DB_RETRIES

                if is_retryable and has_retry_left:
                    delay = DB_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                    self.logger.warning(
                        f'{error_context}: transient database error on attempt {attempt}/{MAX_DB_RETRIES}: {e}. '
                        f'Retrying in {delay:.1f}s'
                    )
                    time.sleep(delay)
                    continue

                self.logger.error(f'{error_context}: {e}')
                return failure_value

    def execute_command(self, query: str, params: tuple = ()) -> bool:
        """
        Execute an INSERT, UPDATE, or DELETE command.

        Args:
            query: SQL command string
            params: Query parameters tuple

        Returns:
            True if successful, False otherwise
        """

        def operation():
            with self.get_cursor() as cursor:
                cursor.execute(query, params)
                return True

        return self._run_with_retry(operation, 'Error executing command', False)

    def set_entity(self, query: str, params: tuple) -> bool:
        """
        Insert or update an entity using the provided query.

        Args:
            query: SQL INSERT/UPDATE query with ON DUPLICATE KEY UPDATE
            params: Query parameters tuple

        Returns:
            True if successful, False otherwise
        """
        return self.execute_command(query, params)


class Repository(BaseRepository):
    """
    Consolidated repository for all database operations.
    Methods are ordered to match their usage in the storage workflow.
    """

    def __init__(self, db_connection):
        super().__init__(db_connection)

    def set_miner(self, miner: Miner) -> bool:
        """
        Insert a miner (ignore conflicts)

        Args:
            miner: Miner object to store

        Returns:
            True if successful, False otherwise
        """
        params = (miner.uid, miner.hotkey, miner.github_id)
        return self.set_entity(SET_MINER, params)

    def cleanup_stale_miner_data(self, evaluation: MinerEvaluation) -> None:
        """
        Remove stale evaluation data when a miner re-registers on a new uid/hotkey.

        Deletes miner_evaluations and miners rows for the same
        github_id but under a different (uid, hotkey) pair, ensuring only one
        evaluation per real github user exists in the database.

        Args:
            evaluation: The current MinerEvaluation being stored
        """
        if not evaluation.github_id or evaluation.github_id == '0':
            return

        params = (evaluation.github_id, evaluation.uid, evaluation.hotkey)
        eval_params = params + (evaluation.evaluation_timestamp,)

        # Clean up when same github_id re-registers on a new uid/hotkey
        self.execute_command(CLEANUP_STALE_MINER_EVALUATIONS, eval_params)
        self.execute_command(CLEANUP_STALE_MINERS, params)

        # Clean up when same (uid, hotkey) re-links to a new github_id
        reverse_params = (evaluation.uid, evaluation.hotkey, evaluation.github_id)
        reverse_eval_params = reverse_params + (evaluation.evaluation_timestamp,)
        self.execute_command(CLEANUP_STALE_MINER_EVALUATIONS_BY_HOTKEY, reverse_eval_params)
        self.execute_command(CLEANUP_STALE_MINERS_BY_HOTKEY, reverse_params)

    def store_pull_requests_bulk(self, pull_requests: List[PullRequest]) -> int:
        """
        Bulk insert/update pull requests with efficient SQL conflict resolution

        Args:
            pull_requests: List of PullRequest objects to store

        Returns:
            Count of successfully stored pull requests
        """
        if not pull_requests:
            return 0

        # Prepare data for bulk insert
        values = []
        for pr in pull_requests:
            # uid is causing issues bc it keeps remaining as an np.int64
            if isinstance(pr.uid, np.integer):
                pr.uid = pr.uid.item()

            values.append(
                (
                    pr.number,
                    pr.repository_full_name,
                    pr.uid,
                    pr.hotkey,
                    pr.github_id,
                    pr.title,
                    pr.author_login,
                    pr.merged_at,
                    pr.created_at,
                    pr.pr_state.value,  # Convert PRState enum to string
                    pr.repo_weight_multiplier,
                    pr.base_score,
                    pr.issue_multiplier,
                    pr.open_pr_spam_multiplier,
                    pr.pioneer_dividend,
                    pr.pioneer_rank,
                    pr.time_decay_multiplier,
                    pr.credibility_multiplier,
                    pr.review_quality_multiplier,
                    pr.label_multiplier,
                    pr.label,
                    pr.earned_score,
                    pr.collateral_score,
                    pr.additions,
                    pr.deletions,
                    pr.commits,
                    pr.total_nodes_scored,
                    pr.merged_by_login,
                    pr.description,
                    pr.last_edited_at,
                    pr.code_density,
                    pr.token_score,
                    pr.structural_count,
                    pr.structural_score,
                    pr.leaf_count,
                    pr.leaf_score,
                )
            )

        def operation():
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values

                execute_values(
                    cursor,
                    BULK_UPSERT_PULL_REQUESTS.replace('VALUES %s', 'VALUES %s'),
                    values,
                    template=None,
                    page_size=100,
                )
                return len(values)

        return self._run_with_retry(operation, 'Error in bulk pull request storage', 0)

    def store_issues_bulk(self, issues: List[Issue]) -> int:
        """
        Bulk insert/update issues with efficient SQL conflict resolution

        Args:
            issues: List of Issue objects to store

        Returns:
            Count of successfully stored issues
        """
        if not issues:
            return 0

        # Prepare data for bulk insert
        values = []
        for issue in issues:
            values.append(
                (
                    issue.number,
                    issue.pr_number,
                    issue.repository_full_name,
                    issue.title,
                    issue.created_at,
                    issue.closed_at,
                    issue.author_login,
                    issue.state,
                    issue.author_association,
                    issue.author_github_id,
                    issue.is_transferred,
                    issue.updated_at,
                    issue.discovery_base_score,
                    issue.discovery_earned_score,
                    issue.discovery_review_quality_multiplier,
                    issue.discovery_repo_weight_multiplier,
                    issue.discovery_time_decay_multiplier,
                    issue.discovery_credibility_multiplier,
                    issue.discovery_open_issue_spam_multiplier,
                )
            )

        def operation():
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values

                execute_values(
                    cursor, BULK_UPSERT_ISSUES.replace('VALUES %s', 'VALUES %s'), values, template=None, page_size=100
                )
                return len(values)

        return self._run_with_retry(operation, 'Error in bulk issue storage', 0)

    def store_file_changes_bulk(self, file_changes: List[FileChange]) -> int:
        """
        Bulk insert/update file changes with efficient SQL conflict resolution

        Args:
            file_changes: List of FileChange objects to store (must include pr_number and repository_full_name)

        Returns:
            Count of successfully stored file changes
        """
        if not file_changes:
            return 0

        # Prepare data for bulk insert
        values = []
        for file_change in file_changes:
            values.append(
                (
                    file_change.pr_number,
                    file_change.repository_full_name,
                    file_change.filename,
                    file_change.changes,
                    file_change.additions,
                    file_change.deletions,
                    file_change.status,
                    file_change.patch,
                    file_change.file_extension or file_change._calculate_file_extension(),
                )
            )

        prs = {(fc.pr_number, fc.repository_full_name) for fc in file_changes}

        def operation():
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values

                execute_values(
                    cursor,
                    BULK_UPSERT_FILE_CHANGES.replace('VALUES %s', 'VALUES %s'),
                    values,
                    template=None,
                    page_size=100,
                )
                return len(values)

        return self._run_with_retry(operation, f'Error in bulk file change storage | PRs: {prs}', 0)

    def set_miner_evaluation(self, evaluation: MinerEvaluation) -> bool:
        """
        Insert or update a miner evaluation.

        Args:
            evaluation: MinerEvaluation object to store

        Returns:
            True if successful, False otherwise
        """
        eval_values = [
            (
                evaluation.uid,
                evaluation.hotkey,
                evaluation.github_id,
                evaluation.failed_reason,
                evaluation.base_total_score,
                evaluation.total_score,
                evaluation.total_collateral_score,
                evaluation.total_nodes_scored,
                evaluation.total_open_prs,
                evaluation.total_closed_prs,
                evaluation.total_merged_prs,
                evaluation.total_prs,
                evaluation.unique_repos_count,
                evaluation.is_eligible,
                evaluation.credibility,
                evaluation.total_token_score,
                evaluation.total_structural_count,
                evaluation.total_structural_score,
                evaluation.total_leaf_count,
                evaluation.total_leaf_score,
                evaluation.issue_discovery_score,
                evaluation.issue_token_score,
                evaluation.issue_credibility,
                evaluation.is_issue_eligible,
                evaluation.total_solved_issues,
                evaluation.total_valid_solved_issues,
                evaluation.total_closed_issues,
                evaluation.total_open_issues,
            )
        ]

        def operation():
            with self.get_cursor() as cursor:
                from psycopg2.extras import execute_values

                execute_values(cursor, BULK_UPSERT_MINER_EVALUATION, eval_values)
                return True

        return self._run_with_retry(operation, 'Error in miner evaluation storage', False)
