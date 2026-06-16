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
        # Per-client cache of repo → maintainer GitHub IDs. A new MirrorClient is
        # created per scoring round, so cache lifetime == round. Used by the
        # issue-multiplier tier and the maintainer_cut carve-out so a repo's
        # maintainer set is fetched once per round, not once per PR.
        self._maintainer_github_ids_cache: Dict[str, frozenset[str]] = {}

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
        data = self._fetch_windowed(path, since_by_repo)
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
        data = self._fetch_windowed(path, since_by_repo)
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

    def get_maintainer_github_ids(self, repo_full_name: str) -> frozenset[str]:
        """Return the current maintainer GitHub IDs for ``repo_full_name``.

        Wraps ``get_repo_maintainers`` with a per-instance cache so the same
        mirror call is not re-issued across PRs being scored for the same repo
        in a single round. On mirror request failure, returns an empty
        ``frozenset`` so callers treat the repo as having no identifiable
        maintainers — conservative for the issue-bonus tier determination.

        Used by ``_calculate_issue_multiplier`` instead of consulting each
        linked issue's stored ``author_association`` field, which the mirror
        snapshots at ingest time and never refreshes (so a role change after
        an issue was filed would otherwise be invisible).
        """
        cached = self._maintainer_github_ids_cache.get(repo_full_name)
        if cached is not None:
            return cached
        try:
            response = self.get_repo_maintainers(repo_full_name)
            ids = frozenset(m.github_id for m in response.maintainers if m.github_id)
        except MirrorRequestError as e:
            bt.logging.warning(
                f'Mirror maintainer lookup failed for {repo_full_name} ({e}); '
                f'issue-bonus tier will default to standard for this repo this round'
            )
            ids = frozenset()
        self._maintainer_github_ids_cache[repo_full_name] = ids
        return ids

    def _fetch_windowed(self, path: str, since_by_repo: Optional[Dict[str, datetime]]) -> dict:
        """POST a per-repo ``since`` map when one is given, else GET the
        mirror's default window."""
        if since_by_repo:
            body = {
                'since_by_repo': {repo: dt.astimezone(timezone.utc).isoformat() for repo, dt in since_by_repo.items()}
            }
            return self._post(path, body)
        return self._get(path)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request('GET', path, params=params)

    def _post(self, path: str, json_body: dict) -> dict:
        return self._request('POST', path, json_body=json_body)

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
                    response = self.session.post(url, json=json_body, timeout=self.timeout)
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
