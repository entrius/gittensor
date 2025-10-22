"""
Repository for handling database operations for Issue entities
"""
from typing import Optional, List, Dict, Any
from gittensor.classes import Issue
from .base_repository import BaseRepository
from ..queries import (
    SET_ISSUE,
    BULK_UPSERT_ISSUES
)


class IssuesRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

    def _map_to_issue(self, row: Dict[str, Any]) -> Issue:
        """Map database row to Issue object"""
        return Issue(
            number=row['number'],
            pr_number=row['pr_number'],
            repository_full_name=row['repository_full_name'],
            title=row['title'],
            created_at=row['created_at'],
            closed_at=row['closed_at']
        )

    def get_issue(self, number: int, repository_full_name: str) -> Optional[Issue]:
        """
        Get an issue by its number and repository

        Args:
            number: Issue number
            repository_full_name: Full repository name (owner/name)

        Returns:
            Issue object if found, None otherwise
        """
        return self.query_single((number, repository_full_name), self._map_to_issue)

    def get_issues_by_repository(self, repository_full_name: str) -> List[Issue]:
        """
        Get all issues for a given repository

        Args:
            repository_full_name: Full repository name (owner/name)

        Returns:
            List of Issue objects
        """
        return self.query_multiple((repository_full_name,), self._map_to_issue)

    def set_issue(self, issue: Issue) -> bool:
        """
        Insert or update an issue

        Args:
            issue: Issue object to store

        Returns:
            True if successful, False otherwise
        """
        params = (
            issue.number,
            issue.pr_number,
            issue.repository_full_name,
            issue.title,
            issue.created_at,
            issue.closed_at
        )
        return self.set_entity(SET_ISSUE, params)

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