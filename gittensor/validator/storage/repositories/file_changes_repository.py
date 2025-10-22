"""
Repository for handling database operations for FileChange entities
"""
from typing import Optional, List, Dict, Any
from gittensor.classes import FileChange
from .base_repository import BaseRepository
from ..queries import (
    SET_FILE_CHANGES_FOR_PR,
    BULK_UPSERT_FILE_CHANGES
)


class FileChangesRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

    def _map_to_file_change(self, row: Dict[str, Any]) -> FileChange:
        """Map database row to FileChange object"""
        return FileChange(
            pr_number=row['pr_number'],
            repository_full_name=row['repository_full_name'],
            filename=row['filename'],
            changes=row['changes'],
            additions=row['additions'],
            deletions=row['deletions'],
            status=row['status'],
            patch=row['patch'],
            file_extension=row.get('file_extension'),
            id=row.get('id')
        )

    def get_file_change(self, file_change_id: int) -> Optional[FileChange]:
        """
        Get a file change by its ID

        Args:
            file_change_id: Primary key of the file change

        Returns:
            FileChange object if found, None otherwise
        """
        return self.query_single((file_change_id,), self._map_to_file_change)

    def get_file_changes_by_pr(self, pr_number: int, repository_full_name: str) -> List[FileChange]:
        """
        Get all file changes for a specific pull request

        Args:
            pr_number: Pull request number
            repository_full_name: Repository full name

        Returns:
            List of FileChange objects
        """
        return self.query_multiple((pr_number, repository_full_name), self._map_to_file_change)

    def set_file_changes_for_pr(self, pr_number: int, repository_full_name: str, file_changes: List[FileChange]) -> bool:
        """
        Set file changes for a specific pull request.

        This method efficiently stores multiple file changes for a PR in a single transaction.
        It will insert new file changes or update existing ones.

        Args:
            pr_number: Pull request number
            repository_full_name: Repository full name
            file_changes: List of FileChange objects to store

        Returns:
            True if all file changes were stored successfully, False otherwise
        """
        if not file_changes:
            return True

        query = SET_FILE_CHANGES_FOR_PR

        try:
            with self.get_cursor() as cursor:
                for file_change in file_changes:
                    params = (
                        pr_number,
                        repository_full_name,
                        file_change.filename,
                        file_change.changes,
                        file_change.additions,
                        file_change.deletions,
                        file_change.status,
                        file_change.patch,
                        file_change.file_extension or file_change._calculate_file_extension()
                    )
                    cursor.execute(query, params)

                self.db.commit()
                return True
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error storing file changes for PR {pr_number} in {repository_full_name}: {e}")
            return False

    def set_file_change(self, pr_number: int, repository_full_name: str, file_change: FileChange) -> bool:
        """
        Set a single file change for a pull request.

        Args:
            pr_number: Pull request number
            repository_full_name: Repository full name
            file_change: FileChange object to store

        Returns:
            True if successful, False otherwise
        """
        return self.set_file_changes_for_pr(pr_number, repository_full_name, [file_change])

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
            values.append((
                file_change.pr_number,
                file_change.repository_full_name,
                file_change.filename,
                file_change.changes,
                file_change.additions,
                file_change.deletions,
                file_change.status,
                file_change.patch,
                file_change.file_extension or file_change._calculate_file_extension()
            ))

        try:
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values
                execute_values(
                    cursor,
                    BULK_UPSERT_FILE_CHANGES.replace('VALUES %s', 'VALUES %s'),
                    values,
                    template=None,
                    page_size=100
                )
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk file change storage: {e}")
            return 0