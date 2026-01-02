"""
Database migration system for gittensor validator storage
"""

import logging
import os
from typing import List


class DatabaseMigrator:
    """Handle database schema migrations"""

    def __init__(self, db_connection):
        self.db = db_connection
        self.logger = logging.getLogger(__name__)

    def get_migration_files(self) -> List[str]:
        """Get list of SQL migration files in order"""
        migration_order = [
            'repositories.sql',
            'languages.sql',
            'miners.sql',
            'miner_evaluations.sql',
            'pull_requests.sql',
            'issues.sql',
            'file_changes.sql',
        ]
        return migration_order

    def read_migration_file(self, filename: str) -> str:
        """Read migration SQL from file system"""
        # Read directly from file system
        migration_path = os.path.join(os.path.dirname(__file__), 'sql', filename)
        with open(migration_path, 'r') as f:
            return f.read()

    def run_migration(self, filename: str) -> bool:
        """Run a single migration file"""
        try:
            sql_content = self.read_migration_file(filename)

            with self.db.cursor() as cursor:
                # Split on semicolons and execute each statement
                statements = [stmt.strip() for stmt in sql_content.split(';') if stmt.strip()]

                for statement in statements:
                    if statement:
                        cursor.execute(statement)

                self.db.commit()
                self.logger.info(f'Successfully ran migration: {filename}')
                return True

        except Exception as e:
            self.db.rollback()
            self.logger.error(f'Failed to run migration {filename}: {e}')
            return False

    def migrate(self) -> bool:
        """Run all migrations in order"""
        migration_files = self.get_migration_files()

        for filename in migration_files:
            if not self.run_migration(filename):
                self.logger.error(f'Migration failed at {filename}')
                return False

        self.logger.info('All migrations completed successfully')
        return True

    def create_tables(self) -> bool:
        """Create all tables (alias for migrate)"""
        return self.migrate()
