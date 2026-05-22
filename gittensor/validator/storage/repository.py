"""
Repository class providing database operations for validator storage.

This module consolidates all database operations into a single Repository class,
providing clean methods for storing miners, pull requests, issues, file changes,
and miner evaluations.
"""

import logging
from contextlib import contextmanager
from typing import Dict, List

import numpy as np

from gittensor.classes import FileChange, Issue, Miner, MinerEvaluation, PullRequest, RepoEvaluation
from gittensor.validator.utils.load_weights import RepositoryConfig

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

    def execute_command(self, query: str, params: tuple = (), commit: bool = True) -> bool:
        """
        Execute an INSERT, UPDATE, or DELETE command.

        Args:
            query: SQL command string
            params: Query parameters tuple
            commit: Whether to commit after execution (default True)

        Returns:
            True if successful, False otherwise
        """
        try:
            with self.get_cursor() as cursor:
                cursor.execute(query, params)
                if commit:
                    self.db.commit()
                return True
        except Exception as e:
            if commit:
                self.db.rollback()
            self.logger.error(f'Error executing command: {e}')
            return False

    def set_entity(self, query: str, params: tuple, commit: bool = True) -> bool:
        """
        Insert or update an entity using the provided query.

        Args:
            query: SQL INSERT/UPDATE query with ON DUPLICATE KEY UPDATE
            params: Query parameters tuple
            commit: Whether to commit after execution (default True)

        Returns:
            True if successful, False otherwise
        """
        return self.execute_command(query, params, commit=commit)


class Repository(BaseRepository):
    """
    Consolidated repository for all database operations.
    Methods are ordered to match their usage in the storage workflow.
    """

    def __init__(self, db_connection):
        super().__init__(db_connection)

    def set_miner(self, miner: Miner, commit: bool = True) -> bool:
        """
        Insert a miner (ignore conflicts)

        Args:
            miner: Miner object to store
            commit: Whether to commit after execution (default True)

        Returns:
            True if successful, False otherwise
        """
        params = (miner.uid, miner.hotkey, miner.github_id)
        return self.set_entity(SET_MINER, params, commit=commit)

    def cleanup_stale_miner_data(self, evaluation: MinerEvaluation, commit: bool = True) -> None:
        """
        Remove stale evaluation data when a miner re-registers on a new uid/hotkey.

        Deletes miner_evaluations and miners rows for the same
        github_id but under a different (uid, hotkey) pair, ensuring only one
        evaluation per real github user exists in the database.

        Args:
            evaluation: The current MinerEvaluation being stored
        """
        # Skip cleanup for penalized / pre-validation-failed evals — running it
        # for a penalized eval whose github_id is preserved would tug stale rows
        # between two duplicate-share UIDs, removing each other's records.
        if evaluation.failed_reason is not None:
            return
        if not evaluation.github_id or evaluation.github_id == '0':
            return

        params = (evaluation.github_id, evaluation.uid, evaluation.hotkey)
        eval_params = params + (evaluation.evaluation_timestamp,)

        # Clean up when same github_id re-registers on a new uid/hotkey
        self.execute_command(CLEANUP_STALE_MINER_EVALUATIONS, eval_params, commit=commit)
        self.execute_command(CLEANUP_STALE_MINERS, params, commit=commit)

        # Clean up when same (uid, hotkey) re-links to a new github_id
        reverse_params = (evaluation.uid, evaluation.hotkey, evaluation.github_id)
        reverse_eval_params = reverse_params + (evaluation.evaluation_timestamp,)
        self.execute_command(CLEANUP_STALE_MINER_EVALUATIONS_BY_HOTKEY, reverse_eval_params, commit=commit)
        self.execute_command(CLEANUP_STALE_MINERS_BY_HOTKEY, reverse_params, commit=commit)

    def store_pull_requests_bulk(self, pull_requests: List[PullRequest], commit: bool = True) -> int:
        """
        Bulk insert/update pull requests with efficient SQL conflict resolution

        Args:
            pull_requests: List of PullRequest objects to store
            commit: Whether to commit after execution (default True)

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
                    pr.base_score,
                    pr.issue_multiplier,
                    pr.open_pr_spam_multiplier,
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

        try:
            with self.get_cursor() as cursor:
                cursor.executemany(BULK_UPSERT_PULL_REQUESTS, values)
                if commit:
                    self.db.commit()
                return len(values)
        except Exception as e:
            if commit:
                self.db.rollback()
            self.logger.error(f'Error in bulk pull request storage: {e}')
            return 0

    def store_issues_bulk(self, issues: List[Issue], commit: bool = True) -> int:
        """
        Bulk insert/update issues with efficient SQL conflict resolution

        Args:
            issues: List of Issue objects to store
            commit: Whether to commit after execution (default True)

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
                    issue.discovery_time_decay_multiplier,
                    issue.discovery_credibility_multiplier,
                    issue.discovery_open_issue_spam_multiplier,
                )
            )

        try:
            with self.get_cursor() as cursor:
                cursor.executemany(BULK_UPSERT_ISSUES, values)
                if commit:
                    self.db.commit()
                return len(values)
        except Exception as e:
            if commit:
                self.db.rollback()
            self.logger.error(f'Error in bulk issue storage: {e}')
            return 0

    def store_file_changes_bulk(self, file_changes: List[FileChange], commit: bool = True) -> int:
        """
        Bulk insert/update file changes with efficient SQL conflict resolution

        Args:
            file_changes: List of FileChange objects to store (must include pr_number and repository_full_name)
            commit: Whether to commit after execution (default True)

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
                cursor.executemany(BULK_UPSERT_FILE_CHANGES, values)
                if commit:
                    self.db.commit()
                return len(values)
        except Exception as e:
            if commit:
                self.db.rollback()
            prs = {(fc.pr_number, fc.repository_full_name) for fc in file_changes}
            self.logger.error(f'Error in bulk file change storage: {e} | PRs: {prs}')
            return 0

    def set_miner_evaluation(
        self,
        evaluation: MinerEvaluation,
        master_repositories: Dict[str, RepositoryConfig],
        commit: bool = True,
    ) -> bool:
        """
        Insert or update a miner evaluation, one row per master-list repository.

        A row is written for every repo in ``master_repositories``; repos the
        miner never engaged get a zeroed RepoEvaluation.

        Args:
            evaluation: MinerEvaluation object to store
            master_repositories: The full master repo registry (one row each)
            commit: Whether to commit after execution (default True)

        Returns:
            True if successful, False otherwise
        """
        eval_values = []
        for repo_name in master_repositories:
            repo_eval = evaluation.repo_evaluations.get(repo_name) or RepoEvaluation(repository_full_name=repo_name)
            eval_values.append(
                (
                    evaluation.uid,
                    evaluation.hotkey,
                    evaluation.github_id,
                    repo_name,
                    evaluation.failed_reason,
                    repo_eval.base_total_score,
                    repo_eval.total_score,
                    repo_eval.total_collateral_score,
                    repo_eval.total_nodes_scored,
                    repo_eval.total_open_prs,
                    repo_eval.total_closed_prs,
                    repo_eval.total_merged_prs,
                    repo_eval.total_prs,
                    evaluation.unique_repos_count,
                    repo_eval.is_eligible,
                    repo_eval.credibility,
                    repo_eval.total_token_score,
                    repo_eval.total_structural_count,
                    repo_eval.total_structural_score,
                    repo_eval.total_leaf_count,
                    repo_eval.total_leaf_score,
                    repo_eval.issue_discovery_score,
                    repo_eval.issue_token_score,
                    repo_eval.issue_credibility,
                    repo_eval.is_issue_eligible,
                    repo_eval.total_solved_issues,
                    repo_eval.total_valid_solved_issues,
                    repo_eval.total_closed_issues,
                    repo_eval.total_open_issues,
                )
            )

        if not eval_values:
            return True

        try:
            with self.get_cursor() as cursor:
                cursor.executemany(BULK_UPSERT_MINER_EVALUATION, eval_values)
                if commit:
                    self.db.commit()
                return True
        except Exception as e:
            if commit:
                self.db.rollback()
            self.logger.error(f'Error in miner evaluation storage: {e}')
            return False
