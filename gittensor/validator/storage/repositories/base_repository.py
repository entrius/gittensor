"""
Base repository class that provides common database operations and connection management.

This class implements a clean abstraction layer for database operations, eliminating
redundant cursor management and error handling code across repository classes.
"""

from typing import Optional, List, Dict, Any, TypeVar, Callable
from contextlib import contextmanager
import logging

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

    def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """
        Execute a SELECT query and return results.

        Args:
            query: SQL query string
            params: Query parameters tuple
            dictionary: Whether to return dictionary results

        Returns:
            List of result dictionaries
        """
        with self.get_cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()

    def execute_single_query(self, query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        """
        Execute a SELECT query and return single result.

        Args:
            query: SQL query string
            params: Query parameters tuple
            dictionary: Whether to return dictionary result

        Returns:
            Single result dictionary or None
        """
        with self.get_cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchone()

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

    def query_single(self, query: str, params: tuple, mapper: Callable[[Dict[str, Any]], T]) -> Optional[T]:
        """
        Execute query and map single result to domain object.

        Args:
            query: SQL query string
            params: Query parameters tuple
            mapper: Function to map result dict to domain object

        Returns:
            Mapped domain object or None
        """
        result = self.execute_single_query(query, params)
        if result:
            return mapper(result)
        return None

    def query_multiple(self, query: str, params: tuple, mapper: Callable[[Dict[str, Any]], T]) -> List[T]:
        """
        Execute query and map multiple results to domain objects.

        Args:
            query: SQL query string
            params: Query parameters tuple
            mapper: Function to map result dict to domain object

        Returns:
            List of mapped domain objects
        """
        results = self.execute_query(query, params)
        return [mapper(result) for result in results]

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

