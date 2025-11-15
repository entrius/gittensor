"""
Base repository class that provides common database operations and connection management.

This class implements a clean abstraction layer for database operations, eliminating
redundant cursor management and error handling code across repository classes.
"""

from typing import TypeVar
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
