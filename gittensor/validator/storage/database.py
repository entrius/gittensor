"""
Database connection utility for validator storage operations.
"""

import os
from typing import Optional

import bittensor as bt

try:
    import psycopg2

    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
    bt.logging.warning("psycopg2 not installed. Database storage features will be disabled.")


def create_database_connection() -> Optional[object]:
    """
    Create a PostgreSQL database connection using environment variables.

    Returns:
        Database connection if successful, None otherwise
    """
    if not POSTGRES_AVAILABLE:
        bt.logging.error("Cannot create database connection: psycopg2 not installed")
        return None

    try:
        db_config = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': int(os.getenv('DB_PORT', 5432)),
            'user': os.getenv('DB_USER', 'postgres'),
            'password': os.getenv('DB_PASSWORD', ''),
            'database': os.getenv('DB_NAME', 'gittensor_validator'),
        }

        connection = psycopg2.connect(**db_config)
        connection.autocommit = False
        bt.logging.success("Successfully connected to PostgreSQL database for validation result storage")
        return connection

    except psycopg2.Error as e:
        bt.logging.error(f"Failed to connect to database: {e}")
        return None
    except Exception as e:
        bt.logging.error(f"Unexpected error connecting to database: {e}")
        return None
