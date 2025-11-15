"""
Repository for handling database operations for Miner entities
"""
from gittensor.classes import Miner
from .base_repository import BaseRepository
from ..queries import (
    SET_MINER
)


class MinersRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

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
