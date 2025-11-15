"""
Repository for handling database operations for Issue entities
"""
from typing import List
from gittensor.classes import Issue
from .base_repository import BaseRepository
from ..queries import (
    BULK_UPSERT_ISSUES
)


class IssuesRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

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
            values.append((
                issue.number,
                issue.pr_number,
                issue.repository_full_name,
                issue.title,
                issue.created_at,
                issue.closed_at
            ))

        try:
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values
                execute_values(
                    cursor,
                    BULK_UPSERT_ISSUES.replace('VALUES %s', 'VALUES %s'),
                    values,
                    template=None,
                    page_size=100
                )
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk issue storage: {e}")
            return 0