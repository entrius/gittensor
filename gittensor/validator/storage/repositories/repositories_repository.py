"""
Repository for handling database operations for Repository entities
"""

from typing import Any, Dict, List, Optional, Set

from gittensor.classes import Repository

from ..queries import BULK_UPSERT_REPOSITORIES, SET_REPOSITORY
from .base_repository import BaseRepository


class RepositoriesRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

    def _map_to_repository(self, row: Dict[str, Any]) -> Repository:
        """Map database row to Repository object"""
        return Repository(name=row['name'], owner=row['owner'], weight=row['weight'])

    def get_repository(self, repository_full_name: str) -> Optional[Repository]:
        """
        Get a repository by its full name

        Args:
            repository_full_name: Full repository name (owner/name)

        Returns:
            Repository object if found, None otherwise
        """
        return self.query_single((repository_full_name,), self._map_to_repository)

    def set_repository(self, repository: Repository) -> bool:
        """
        Insert or update a repository

        Args:
            repository: Repository object to store

        Returns:
            True if successful, False otherwise
        """
        params = (repository.full_name, repository.name, repository.owner, repository.weight)
        return self.set_entity(SET_REPOSITORY, params)

    def get_all_repositories(self) -> List[Repository]:
        """
        Get all repositories

        Returns:
            List of Repository objects
        """
        return self.query_multiple((), self._map_to_repository)

    def store_repositories_bulk(self, repository_full_names: Set[str]) -> int:
        """
        Bulk insert/update repositories given their full names as strings using efficient SQL.

        Args:
            repository_full_names: Set of strings like {"owner/repo", "other/owner"}

        Returns:
            Count of successfully stored repositories
        """
        if not repository_full_names:
            return 0

        # Prepare data for bulk insert
        values = []
        for full_name in repository_full_names:
            parts = full_name.split('/')
            if len(parts) == 2:
                values.append((full_name, parts[1], parts[0]))  # (full_name, name, owner)

        if not values:
            return 0

        try:
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values

                execute_values(
                    cursor,
                    BULK_UPSERT_REPOSITORIES.replace('VALUES %s', 'VALUES %s'),
                    values,
                    template=None,
                    page_size=100,
                )
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk repository storage: {e}")
            return 0

