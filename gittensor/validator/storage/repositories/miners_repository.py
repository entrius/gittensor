"""
Repository for handling database operations for Miner entities
"""
from typing import Optional, List, Dict, Any
from gittensor.classes import Miner
from .base_repository import BaseRepository
from ..queries import (
    SET_MINER,
    UPSERT_MINER,
    BULK_UPSERT_MINERS
)


class MinersRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

    def _map_to_miner(self, row: Dict[str, Any]) -> Miner:
        """Map database row to Miner object"""
        return Miner(
            uid=row['uid'],
            hotkey=row['hotkey'],
            github_id=row['github_id']
        )

    def get_miner(self, uid: int, hotkey: str, github_id: str) -> Optional[Miner]:
        """
        Get a miner by their composite primary key

        Args:
            uid: Miner UID
            hotkey: Miner hotkey
            github_id: Miner GitHub ID

        Returns:
            Miner object if found, None otherwise
        """
        return self.query_single((uid, hotkey, github_id), self._map_to_miner)

    def get_miner_by_uid(self, uid: int) -> Optional[Miner]:
        """
        Get a miner by UID

        Args:
            uid: Miner UID

        Returns:
            Miner object if found, None otherwise
        """
        return self.query_single((uid,), self._map_to_miner)

    def get_miner_by_hotkey(self, hotkey: str) -> Optional[Miner]:
        """
        Get a miner by hotkey

        Args:
            hotkey: Miner hotkey

        Returns:
            Miner object if found, None otherwise
        """
        return self.query_single((hotkey,), self._map_to_miner)

    def get_miner_by_github_id(self, github_id: str) -> Optional[Miner]:
        """
        Get a miner by GitHub ID

        Args:
            github_id: Miner GitHub ID

        Returns:
            Miner object if found, None otherwise
        """
        return self.query_single((github_id,), self._map_to_miner)

    def get_miner_by_hotkey_and_github_id(self, hotkey: str, github_id: str) -> Optional[Miner]:
        """
        Get a miner by hotkey and GitHub ID

        Args:
            hotkey: Miner hotkey
            github_id: Miner GitHub ID

        Returns:
            Miner object if found, None otherwise
        """
        return self.query_single((hotkey, github_id), self._map_to_miner)

    def set_miner(self, miner: Miner) -> bool:
        """
        Insert a miner (ignore conflicts)

        Args:
            miner: Miner object to store

        Returns:
            True if successful, False otherwise
        """
        params = (
            miner.uid,
            miner.hotkey,
            miner.github_id
        )
        return self.set_entity(SET_MINER, params)

    def upsert_miner(self, miner: Miner) -> bool:
        """
        Insert or update a miner (updates timestamp on conflict)

        Args:
            miner: Miner object to store

        Returns:
            True if successful, False otherwise
        """
        params = (
            miner.uid,
            miner.hotkey,
            miner.github_id
        )
        return self.set_entity(UPSERT_MINER, params)

    def get_all_miners(self) -> List[Miner]:
        """
        Get all miners

        Returns:
            List of Miner objects
        """
        return self.query_multiple((), self._map_to_miner)

    def store_miners_bulk(self, miners: List[Miner]) -> int:
        """
        Bulk upsert miners using efficient SQL

        Args:
            miners: List of Miner objects to store

        Returns:
            Count of successfully stored miners
        """
        if not miners:
            return 0

        # Prepare data for bulk insert
        values = [(miner.uid, miner.hotkey, miner.github_id) for miner in miners]

        try:
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values
                execute_values(
                    cursor,
                    BULK_UPSERT_MINERS.replace('VALUES %s', 'VALUES %s'),
                    values,
                    template=None,
                    page_size=100
                )
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk miner storage: {e}")
            return 0