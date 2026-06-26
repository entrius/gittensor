# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Pytest fixtures for validator tests.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytest

from gittensor.classes import PRState, PullRequest


@pytest.fixture(autouse=True)
def _isolate_repo_registry(monkeypatch, tmp_path):
    """Keep validator tests offline and deterministic:

    - the repos API fetch always fails, so loads exercise the fallback path
      (tests covering the API re-patch ``_fetch_registry_from_api`` themselves;
      their setattr runs after this fixture and wins);
    - the on-disk last-good cache points at an empty per-test tmp path, so the
      default load yields an empty registry. Tests that want a warm-cache
      fallback write their registry JSON to ``lw._get_repos_cache_path()``.
    """
    from gittensor.validator.utils import load_weights as lw

    def _api_disabled():
        raise lw.RepositoryRegistryError('repos API disabled in tests')

    monkeypatch.setattr(lw, '_fetch_registry_from_api', _api_disabled)
    monkeypatch.setattr(lw, '_get_repos_cache_path', lambda: tmp_path / 'repos_cache.json')


@dataclass
class PRBuilder:
    """Builder for creating mock PullRequests with sensible defaults.

    Tests instantiate ``PRBuilder()`` directly and call ``.create(...)``.
    """

    _counter: int = 0
    _repo_counter: int = 0

    def _next_number(self) -> int:
        self._counter += 1
        return self._counter

    def _next_repo(self) -> str:
        self._repo_counter += 1
        return f'test/repo-{self._repo_counter}'

    def create(
        self,
        state: PRState,
        number: Optional[int] = None,
        earned_score: float = 100.0,
        collateral_score: float = 20.0,
        repo: Optional[str] = None,
        unique_repo: bool = False,
        token_score: float = 10.0,
        uid: int = 0,
        merged_at: Optional[datetime] = None,
    ) -> PullRequest:
        """Create a mock PullRequest with the given parameters."""
        if number is None:
            number = self._next_number()

        if repo is None:
            repo = self._next_repo() if unique_repo else 'test/repo'

        if merged_at is None:
            merged_at = datetime.now(timezone.utc) if state == PRState.MERGED else None

        return PullRequest(
            number=number,
            repository_full_name=repo,
            uid=uid,
            hotkey=f'hotkey_{uid}',
            github_id=str(uid),
            title=f'Test PR #{number}',
            author_login=f'user_{uid}',
            merged_at=merged_at,
            created_at=datetime.now(timezone.utc),
            pr_state=state,
            earned_score=earned_score,
            collateral_score=collateral_score,
            token_score=token_score,
        )
