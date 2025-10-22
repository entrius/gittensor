"""
Repository for handling database operations for PullRequest entities
"""
from typing import Optional, List, Dict, Any
from gittensor.classes import PullRequest, FileChange
from .base_repository import BaseRepository
from ..queries import (
    SET_PULL_REQUEST,
    BULK_UPSERT_PULL_REQUESTS
)

import numpy as np


class PullRequestsRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

    def _map_to_pull_request(self, row: Dict[str, Any]) -> PullRequest:
        """Map database row to PullRequest object"""
        return PullRequest(
            number=row['number'],
            repository_full_name=row['repository_full_name'],
            uid=row['uid'],
            hotkey=row['hotkey'],
            github_id=row['github_id'],
            title=row['title'],
            author_login=row['author_login'],
            merged_at=row['merged_at'],
            created_at=row['pr_created_at'],  # DB column pr_created_at maps to model created_at
            earned_score=float(row.get('earned_score', 0.0)),
            additions=row['additions'] or 0,
            deletions=row['deletions'] or 0,
            commits=row.get('commits', 0),
            merged_by_login=row['merged_by_login']  # Optional
        )

    def _map_to_pull_request_with_file_changes(self, rows: List[Dict[str, Any]]) -> Optional[PullRequest]:
        """Map database rows to PullRequest with nested FileChanges"""
        if not rows:
            return None

        first_row = rows[0]

        pull_request = PullRequest(
            number=first_row['number'],
            repository_full_name=first_row['repository_full_name'],
            uid=first_row['uid'],
            hotkey=first_row['hotkey'],
            github_id=first_row['github_id'],
            title=first_row['title'],
            author_login=first_row['author_login'],
            merged_at=first_row['merged_at'],
            created_at=first_row['pr_created_at'],  # DB column pr_created_at maps to model created_at
            earned_score=float(first_row.get('earned_score', 0.0)),
            additions=first_row['additions'] or 0,
            deletions=first_row['deletions'] or 0,
            commits=first_row.get('commits', 0),
            merged_by_login=first_row['merged_by_login']  # Optional
        )

        # Create nested FileChanges if they exist
        if first_row.get('filename'):  # Check if file changes exist
            file_changes = []
            for row in rows:
                if row['filename']:  # Skip rows without file changes
                    file_change = FileChange(
                        pr_number=row['number'],
                        repository_full_name=row['repository_full_name'],
                        filename=row['filename'],
                        changes=row['changes'],
                        additions=row['file_additions'],
                        deletions=row['file_deletions'],
                        status=row['status'],
                        patch=row['patch'],
                        file_extension=row['file_extension']
                    )
                    file_changes.append(file_change)

            # Attach file_changes as a list attribute to the pull_request
            pull_request.file_changes = file_changes

        return pull_request

    def get_pull_request(self, pr_number: int, repository_full_name: str) -> Optional[PullRequest]:
        """
        Get a pull request by its number and repository

        Args:
            pr_number: PR number
            repository_full_name: Full repository name

        Returns:
            PullRequest object if found, None otherwise
        """
        return self.query_single((pr_number, repository_full_name), self._map_to_pull_request)

    def set_pull_request(self, pull_request: PullRequest) -> bool:
        """
        Insert or update a pull request

        Args:
            pull_request: PullRequest object to store

        Returns:
            True if successful, False otherwise
        """

        # uid is causing issues bc it keeps remaining as an np.int64
        if isinstance(pull_request.uid, np.integer):
            pull_request.uid = pull_request.uid.item()  # Converts numpy int to Python int
            
        query = SET_PULL_REQUEST
        params = (
            pull_request.number,
            pull_request.repository_full_name,
            pull_request.uid,
            pull_request.hotkey,
            pull_request.github_id,
            pull_request.earned_score,
            pull_request.title,
            pull_request.merged_at,
            pull_request.created_at,
            pull_request.additions,
            pull_request.deletions,
            pull_request.commits,
            pull_request.author_login,
            pull_request.merged_by_login
        )
        return self.set_entity(query, params)

    def get_pull_requests_by_repository(self, repository_full_name: str) -> List[PullRequest]:
        """
        Get all pull requests for a specific repository

        Args:
            repository_full_name: Full repository name

        Returns:
            List of PullRequest objects
        """
        return self.query_multiple((repository_full_name,), self._map_to_pull_request)

    def get_pull_requests_by_miner(self, uid: int, hotkey: str, github_id: str) -> List[PullRequest]:
        """
        Get all pull requests for a specific miner

        Args:
            uid: Miner UID
            hotkey: Miner hotkey
            github_id: Miner GitHub ID

        Returns:
            List of PullRequest objects
        """
        return self.query_multiple((uid, hotkey, github_id), self._map_to_pull_request)

    def get_pull_request_with_file_changes(self, pr_number: int, repository_full_name: str) -> Optional[PullRequest]:
        """
        Get a pull request with its associated file changes.

        This method efficiently loads nested objects in a single query using JOINs,
        which is a common practice for eager loading of related data.

        Args:
            pr_number: PR number
            repository_full_name: Full repository name

        Returns:
            PullRequest object with nested FileChanges, or None if not found
        """
        results = self.execute_query((pr_number, repository_full_name))
        return self._map_to_pull_request_with_file_changes(results)

    def get_pull_requests_by_repository_with_file_changes(self, repository_full_name: str) -> List[PullRequest]:
        """
        Get all pull requests for a repository with their associated file changes.

        Note: This can be memory intensive for repositories with many PRs and large file changes.
        Consider pagination for production use.

        Args:
            repository_full_name: Full repository name

        Returns:
            List of PullRequest objects with nested file changes
        """
        # Modify query to get all PRs for repository
        query = GET_PULL_REQUEST_WITH_FILE_CHANGES.replace(
            "WHERE pr.number = %s AND pr.repository_full_name = %s",
            "WHERE pr.repository_full_name = %s ORDER BY pr.merged_at DESC"
        )

        results = self.execute_query(query, (repository_full_name,))

        # Group results by PR number to handle multiple file changes per PR
        pr_groups = {}
        for result in results:
            pr_key = result['number']
            if pr_key not in pr_groups:
                pr_groups[pr_key] = []
            pr_groups[pr_key].append(result)

        # Map each group to a PullRequest with nested data
        pull_requests = []
        for pr_results in pr_groups.values():
            pr = self._map_to_pull_request_with_file_changes(pr_results)
            if pr:
                pull_requests.append(pr)

        return pull_requests

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
                pr.uid = pr.uid.item()  # Converts numpy int to Python int
                
            values.append((
                pr.number,
                pr.repository_full_name,
                pr.uid,
                pr.hotkey,
                pr.github_id,
                pr.earned_score,
                pr.title,
                pr.merged_at,
                pr.created_at,
                pr.additions,
                pr.deletions,
                pr.commits,
                pr.author_login,
                pr.merged_by_login
            ))

        try:
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values
                execute_values(
                    cursor,
                    BULK_UPSERT_PULL_REQUESTS.replace('VALUES %s', 'VALUES %s'),
                    values,
                    template=None,
                    page_size=100
                )
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk pull request storage: {e}")
            return 0