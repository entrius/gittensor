"""
Repository for handling database operations for PullRequest entities
"""
from typing import List
from gittensor.classes import PullRequest
from .base_repository import BaseRepository
from ..queries import (
    BULK_UPSERT_PULL_REQUESTS
)

import numpy as np


class PullRequestsRepository(BaseRepository):
    def __init__(self, db_connection):
        super().__init__(db_connection)

    def store_pull_requests_bulk(self, pull_requests: List[PullRequest]) -> int:
        """
        Bulk insert/update pull requests with efficient SQL conflict resolution

        Args:
            pull_requests: List of PullRequest objects to store

        Returns:
            Count of successfully stored pull requests
        """
        if not pull_requests:
            return 0

        # Prepare data for bulk insert
        values = []
        for pr in pull_requests:

            # uid is causing issues bc it keeps remaining as an np.int64
            if isinstance(pr.uid, np.integer):
                pr.uid = pr.uid.item()
                
            values.append((
                pr.number,
                pr.repository_full_name,
                pr.uid,
                pr.hotkey,
                pr.github_id,
                pr.earned_score,
                pr.title,
                pr.merged_at,
                pr.created_at,
                pr.additions,
                pr.deletions,
                pr.commits,
                pr.author_login,
                pr.merged_by_login
            ))

        try:
            with self.get_cursor() as cursor:
                # Use psycopg2's execute_values for efficient bulk insert
                from psycopg2.extras import execute_values
                execute_values(
                    cursor,
                    BULK_UPSERT_PULL_REQUESTS.replace('VALUES %s', 'VALUES %s'),
                    values,
                    template=None,
                    page_size=100
                )
                self.db.commit()
                return len(values)
        except Exception as e:
            self.db.rollback()
            self.logger.error(f"Error in bulk pull request storage: {e}")
            return 0