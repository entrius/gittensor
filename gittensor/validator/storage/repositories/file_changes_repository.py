"""
Repository for handling database operations for FileChange entities
"""
from typing import List
from gittensor.classes import FileChange
from .base_repository import BaseRepository
from ..queries import (
    BULK_UPSERT_FILE_CHANGES
)


class FileChangesRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

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