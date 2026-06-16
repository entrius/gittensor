"""HTTP client for the das-github-mirror scoring API.

Three read endpoints (public, no auth, Cloudflare-rate-limited at 50 req / 10s
per IP) and one admin backfill endpoint (not used by the validator). This
client only covers the scoring-hot-path read endpoints.
"""

import time
from datetime import datetime, timezone
from typing import Dict, Optional

import bittensor as bt
import requests

from gittensor.constants import (
    GITTENSOR_MIRROR_DEFAULT_URL,
    MIRROR_HTTP_TIMEOUT_SECONDS,
    MIRROR_MAX_ATTEMPTS,
    MIRROR_MAX_PAGES,
    MIRROR_PAGE_LIMIT,
)
from gittensor.utils.mirror.models import (
    MirrorIssuesResponse,
    MirrorPullRequestFilesResponse,
    MirrorPullRequestsResponse,
    MirrorRepoMaintainersResponse,
)
from gittensor.utils.utils import backoff_seconds


class MirrorRequestError(RuntimeError):
    """Raised when a mirror request fails with a non-retryable status or
    exhausts all retries on transient failures."""


def _body_preview(response: requests.Response) -> str:
    text = getattr(response, 'text', '')
    if not isinstance(text, str):
        text = str(text)
    return text[:200]


class MirrorClient:
    """Client for https://mirror.gittensor.io scoring endpoints."""

    def __init__(
        self,
        timeout: int = MIRROR_HTTP_TIMEOUT_SECONDS,
        max_attempts: int = MIRROR_MAX_ATTEMPTS,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = GITTENSOR_MIRROR_DEFAULT_URL.rstrip('/')
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = session or requests.Session()

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> 'MirrorClient':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get_miner_pulls(
        self,
        github_id: str,
        since_by_repo: Optional[Dict[str, datetime]] = None,
    ) -> MirrorPullRequestsResponse:
        """Fetch tracked PRs authored by ``github_id``.

        With ``since_by_repo`` (repo full name -> cutoff datetime), POSTs the
        per-repo window map; the response is restricted to those repos, each
        windowed to its own cutoff. Without it, GETs the mirror's default
        window across all tracked repos.
        """
        path = f'/api/v1/miners/{github_id}/pulls'
        data = self._fetch_windowed(path, 'pull_requests', since_by_repo)
        try:
            return MirrorPullRequestsResponse.from_dict(data)
        except Exception as e:
            raise MirrorRequestError(f'Mirror response from {path} was invalid: {e}') from e

    def get_miner_issues(
        self,
        github_id: str,
        since_by_repo: Optional[Dict[str, datetime]] = None,
    ) -> MirrorIssuesResponse:
        """Fetch issues authored by ``github_id``, each with an inline
        ``solving_pr`` when ``solved_by_pr`` is populated.

        With ``since_by_repo``, POSTs the per-repo window map (the scoring
        window). Without it, GETs all currently-open issues unbounded — the
        open-issue-count path.
        """
        path = f'/api/v1/miners/{github_id}/issues'
        data = self._fetch_windowed(path, 'issues', since_by_repo)
        try:
            return MirrorIssuesResponse.from_dict(data)
        except Exception as e:
            raise MirrorRequestError(f'Mirror response from {path} was invalid: {e}') from e

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
        try:
            return MirrorPullRequestFilesResponse.from_dict(data)
        except Exception as e:
            raise MirrorRequestError(f'Mirror response from {path} was invalid: {e}') from e

    def get_repo_maintainers(self, repo_full_name: str) -> MirrorRepoMaintainersResponse:
        """Fetch users whose latest known GitHub association for
        ``repo_full_name`` (``owner/repo``) is OWNER/MEMBER/COLLABORATOR.

        Used to route the per-repo ``maintainer_cut`` emission carve-out. An
        unknown repo returns an empty maintainer list rather than an error.
        """
        path = f'/api/v1/repos/{repo_full_name}/maintainers'
        data = self._get(path)
        try:
            return MirrorRepoMaintainersResponse.from_dict(data)
        except Exception as e:
            raise MirrorRequestError(f'Mirror response from {path} was invalid: {e}') from e

    def _fetch_windowed(
        self,
        path: str,
        list_key: str,
        since_by_repo: Optional[Dict[str, datetime]],
    ) -> dict:
        """Fetch a miner list endpoint, following pagination to completion.

        POSTs a per-repo ``since`` map when one is given, else GETs the mirror's
        default window. Either way the response is paged via ``next_cursor`` and
        the per-page ``list_key`` arrays are concatenated into one response dict.
        """
        if since_by_repo:
            body = {
                'since_by_repo': {repo: dt.astimezone(timezone.utc).isoformat() for repo, dt in since_by_repo.items()}
            }
            return self._fetch_paginated('POST', path, list_key, json_body=body)
        return self._fetch_paginated('GET', path, list_key)

    def _fetch_paginated(
        self,
        method: str,
        path: str,
        list_key: str,
        json_body: Optional[dict] = None,
    ) -> dict:
        """Page through ``path``, concatenating each page's ``list_key`` rows.

        Sends ``limit`` on every request and ``cursor`` once the mirror returns
        a ``next_cursor``, stopping when no cursor comes back. A mirror that
        predates windowed pagination ignores the params and returns the full
        list with no ``next_cursor`` — the loop then completes in one page, so
        this degrades cleanly to a single unbounded request.
        """
        merged: Optional[dict] = None
        items: list = []
        cursor: Optional[str] = None
        for _ in range(MIRROR_MAX_PAGES):
            params: dict = {'limit': MIRROR_PAGE_LIMIT}
            if cursor:
                params['cursor'] = cursor
            data = self._request(method, path, params=params, json_body=json_body)
            if merged is None:
                merged = data
            page_items = data.get(list_key)
            if page_items:
                items.extend(page_items)
            cursor = data.get('next_cursor')
            if not cursor:
                break
        else:
            bt.logging.warning(
                f'Mirror {method} {path} stopped at the {MIRROR_MAX_PAGES}-page cap with a '
                f'cursor still set; the {len(items)} {list_key} loaded may be incomplete.'
            )
        if merged is None:
            merged = {}
        merged[list_key] = items
        return merged

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request('GET', path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        url = f'{self.base_url}{path}'
        last_error: Optional[str] = None

        for attempt in range(self.max_attempts):
            try:
                if method == 'POST':
                    response = self.session.post(url, json=json_body, params=params, timeout=self.timeout)
                else:
                    response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = f'request exception: {e}'
                if attempt < self.max_attempts - 1:
                    backoff = backoff_seconds(attempt)
                    bt.logging.warning(
                        f'Mirror {method} {path} raised {e} '
                        f'(attempt {attempt + 1}/{self.max_attempts}), retrying in {backoff}s...'
                    )
                    time.sleep(backoff)
                continue

            if 200 <= response.status_code < 300:
                try:
                    return response.json()
                except ValueError as e:
                    last_error = f'invalid JSON: {e}; body={_body_preview(response)!r}'
                    if attempt < self.max_attempts - 1:
                        backoff = backoff_seconds(attempt)
                        bt.logging.warning(
                            f'Mirror {method} {path} failed ({last_error}) '
                            f'(attempt {attempt + 1}/{self.max_attempts}), retrying in {backoff}s...'
                        )
                        time.sleep(backoff)
                    continue

            # 4xx except 429 are not retryable — fail fast so callers see the real error.
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise MirrorRequestError(
                    f'Mirror {method} {path} returned {response.status_code}: {_body_preview(response)}'
                )

            last_error = f'status {response.status_code}: {_body_preview(response)}'
            if attempt < self.max_attempts - 1:
                backoff = backoff_seconds(attempt)
                bt.logging.warning(
                    f'Mirror {method} {path} failed ({last_error}) '
                    f'(attempt {attempt + 1}/{self.max_attempts}), retrying in {backoff}s...'
                )
                time.sleep(backoff)

        raise MirrorRequestError(f'Mirror {method} {path} failed after {self.max_attempts} attempts: {last_error}')
