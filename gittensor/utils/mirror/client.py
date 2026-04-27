"""HTTP client for the das-github-mirror scoring API.

Three read endpoints (public, no auth, Cloudflare-rate-limited at 50 req / 10s
per IP) and one admin backfill endpoint (not used by the validator). This
client only covers the scoring-hot-path read endpoints.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Optional

import bittensor as bt
import requests

from gittensor.constants import (
    GITTENSOR_MIRROR_DEFAULT_URL,
    MIRROR_HTTP_TIMEOUT_SECONDS,
    MIRROR_MAX_ATTEMPTS,
    MIRROR_MIN_REQUEST_INTERVAL_SECONDS,
)
from gittensor.utils.mirror.models import (
    MirrorIssuesResponse,
    MirrorPullRequestFilesResponse,
    MirrorPullRequestsResponse,
)


class MirrorRequestError(RuntimeError):
    """Raised when a mirror request fails with a non-retryable status or
    exhausts all retries on transient failures."""


class MirrorClient:
    """Client for https://mirror.gittensor.io scoring endpoints."""

    def __init__(
        self,
        timeout: int = MIRROR_HTTP_TIMEOUT_SECONDS,
        max_attempts: int = MIRROR_MAX_ATTEMPTS,
        session: Optional[requests.Session] = None,
        min_request_interval: float = MIRROR_MIN_REQUEST_INTERVAL_SECONDS,
    ):
        self.base_url = GITTENSOR_MIRROR_DEFAULT_URL.rstrip('/')
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self._min_request_interval = min_request_interval
        self._last_request_at: float = 0.0
        self._throttle_lock = threading.Lock()

    def get_miner_pulls(
        self,
        github_id: str,
        since: Optional[datetime] = None,
    ) -> MirrorPullRequestsResponse:
        """Fetch every tracked PR authored by ``github_id`` since the given
        datetime. If ``since`` is omitted the mirror defaults to 35 days back.
        Response contains all mirror-tracked repos; caller must filter to the
        scoring config's mirror-enabled subset if it's narrower.
        """
        path = f'/api/v1/miners/{github_id}/pulls'
        params = {'since': since.astimezone(timezone.utc).isoformat()} if since else None
        data = self._get(path, params=params)
        return MirrorPullRequestsResponse.from_dict(data)

    def get_miner_issues(
        self,
        github_id: str,
        since: Optional[datetime] = None,
    ) -> MirrorIssuesResponse:
        """Fetch issues authored by ``github_id`` since the given datetime,
        each with an inline ``solving_pr`` when ``solved_by_pr`` is populated."""
        path = f'/api/v1/miners/{github_id}/issues'
        params = {'since': since.astimezone(timezone.utc).isoformat()} if since else None
        data = self._get(path, params=params)
        return MirrorIssuesResponse.from_dict(data)

    def get_pr_files(
        self,
        repo_full_name: str,
        pr_number: int,
    ) -> MirrorPullRequestFilesResponse:
        """Fetch per-file diff metadata and head/base content for a single PR.

        Called only after eligibility filtering — file contents are the
        heaviest payload and shouldn't be pulled speculatively.
        """
        path = f'/api/v1/pulls/{repo_full_name}/{pr_number}/files'
        data = self._get(path)
        return MirrorPullRequestFilesResponse.from_dict(data)

    def _throttle(self) -> None:
        """Enforce a minimum interval between outgoing requests.

        Uses a lock so concurrent callers don't both read a stale
        _last_request_at and skip the sleep.
        """
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_request_interval:
                time.sleep(self._min_request_interval - elapsed)
            self._last_request_at = time.monotonic()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f'{self.base_url}{path}'
        last_error: Optional[str] = None
        self._throttle()

        for attempt in range(self.max_attempts):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = f'request exception: {e}'
                if attempt < self.max_attempts - 1:
                    backoff = min(5 * (2**attempt), 30)
                    bt.logging.warning(
                        f'Mirror GET {path} raised {e} '
                        f'(attempt {attempt + 1}/{self.max_attempts}), retrying in {backoff}s...'
                    )
                    time.sleep(backoff)
                continue

            if 200 <= response.status_code < 300:
                return response.json()

            # 4xx except 429 are not retryable — fail fast so callers see the real error.
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise MirrorRequestError(f'Mirror GET {path} returned {response.status_code}: {response.text[:200]}')

            last_error = f'status {response.status_code}: {response.text[:200]}'
            if attempt < self.max_attempts - 1:
                backoff = min(5 * (2**attempt), 30)
                bt.logging.warning(
                    f'Mirror GET {path} failed ({last_error}) '
                    f'(attempt {attempt + 1}/{self.max_attempts}), retrying in {backoff}s...'
                )
                time.sleep(backoff)

        raise MirrorRequestError(f'Mirror GET {path} failed after {self.max_attempts} attempts: {last_error}')
