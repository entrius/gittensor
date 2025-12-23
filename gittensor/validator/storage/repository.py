"""
Repository class providing database operations for validator storage.

This module consolidates all database operations into a single Repository class,
providing clean methods for storing miners, pull requests, issues, file changes,
and miner evaluations.
"""

import logging
from contextlib import contextmanager
from typing import List, TypeVar

import numpy as np

from gittensor.classes import FileChange, Issue, Miner, MinerEvaluation, PullRequest

from .queries import (
    BULK_UPSERT_FILE_CHANGES,
    BULK_UPSERT_ISSUES,
    BULK_UPSERT_MINER_EVALUATION,
    BULK_UPSERT_PULL_REQUESTS,
    SET_MINER,
)

T = TypeVar('T')


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

    def execute_command(self, query: str, params: tuple = ()) -> bool:
        """
        Execute an INSERT, UPDATE, or DELETE command.

        Args:
            query: SQL command string
            params: Query parameters tuple

        Returns:
            True if successful, False otherwise
        """
        try:
            with self.get_cursor() as cursor:
                cursor.execute(query, params)
                self.db.commit()
                return True
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error executing command: {e}")
            return False

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
                    pr.repository_uniqueness_multiplier,
                    pr.time_decay_multiplier,
                    pr.gittensor_tag_multiplier,
                    pr.credibility_multiplier,
                    pr.earned_score,
                    pr.collateral_score,
                    pr.additions,
                    pr.deletions,
                    pr.commits,
                    pr.total_lines_scored,
                    pr.gittensor_tagged,
                    pr.merged_by_login,
                    pr.description,
                    pr.last_edited_at,
                )
            )

        try:
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
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk pull request storage: {e}")
            return 0

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
                )
            )

        try:
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values

                execute_values(
                    cursor, BULK_UPSERT_ISSUES.replace('VALUES %s', 'VALUES %s'), values, template=None, page_size=100
                )
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk issue storage: {e}")
            return 0

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

        try:
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
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk file change storage: {e}")
            return 0

    def set_miner_evaluation(self, evaluation: MinerEvaluation) -> bool:
        """
        Insert a new miner evaluation

        Args:
            evaluation: MinerEvaluation object to store

        Returns:
            True if successful, False otherwise
        """
        values = [
            (
                evaluation.uid,
                evaluation.hotkey,
                evaluation.github_id,
                evaluation.failed_reason,
                evaluation.base_total_score,
                evaluation.total_score,
                evaluation.total_collateral_score,
                evaluation.total_lines_changed,
                evaluation.total_open_prs,
                evaluation.total_closed_prs,
                evaluation.total_merged_prs,
                evaluation.total_prs,
                evaluation.unique_repos_count,
                evaluation.current_tier.value,
                evaluation.bronze_merged_prs,
                evaluation.bronze_total_prs,
                evaluation.bronze_collateral_score,
                evaluation.bronze_score,
                evaluation.silver_merged_prs,
                evaluation.silver_total_prs,
                evaluation.silver_collateral_score,
                evaluation.silver_score,
                evaluation.gold_merged_prs,
                evaluation.gold_total_prs,
                evaluation.gold_collateral_score,
                evaluation.gold_score,
            )
        ]

        try:
            with self.get_cursor() as cursor:
                from psycopg2.extras import execute_values

                execute_values(cursor, BULK_UPSERT_MINER_EVALUATION, values)
                self.db.commit()
                return True
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in miner evaluation storage: {e}")
            return False
