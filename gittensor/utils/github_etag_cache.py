# Entrius 2025
"""On-disk ETag cache for GitHub REST GETs - 304s do not consume rate quota."""

import os
import threading
from pathlib import Path
from typing import NamedTuple, Optional, Tuple, cast
from urllib.parse import urlencode

import diskcache

from gittensor.constants import ETAG_CACHE_DEFAULT_PATH, ETAG_CACHE_TTL_DAYS

_DISABLED_ENV = 'GITTENSOR_ETAG_CACHE_DISABLED'
_PATH_ENV = 'GITTENSOR_ETAG_CACHE_PATH'


class CacheEntry(NamedTuple):
    etag: str
    body: bytes
    content_type: Optional[str]


_counter_lock = threading.Lock()
_hits = 0
_misses = 0


def record_hit() -> None:
    global _hits
    with _counter_lock:
        _hits += 1


def record_miss() -> None:
    global _misses
    with _counter_lock:
        _misses += 1


def snapshot_and_reset() -> Tuple[int, int]:
    """Return (hits, misses) since the last call and reset both counters."""
    global _hits, _misses
    with _counter_lock:
        h, m = _hits, _misses
        _hits = 0
        _misses = 0
    return h, m


def _is_disabled() -> bool:
    return os.environ.get(_DISABLED_ENV) == '1'


def _resolved_root() -> Path:
    raw = os.environ.get(_PATH_ENV) or ETAG_CACHE_DEFAULT_PATH
    return Path(os.path.expanduser(raw))


def build_request_key(url: str, params: Optional[dict] = None) -> str:
    """Canonical cache key. PAT excluded so cross-PAT reuse works on public data."""
    query = urlencode(sorted((params or {}).items()), doseq=True)
    return f'GET {url}?{query}' if query else f'GET {url}'


class GithubEtagCache:
    """diskcache-backed (etag, body, content_type) store keyed by request URL."""

    def __init__(self, root: Optional[Path] = None, ttl_days: Optional[int] = None) -> None:
        if _is_disabled():
            self._cache = None
            self._ttl_seconds = 0
            return
        path = root if root is not None else _resolved_root()
        self._cache = diskcache.Cache(str(path))
        self._ttl_seconds = (ttl_days if ttl_days is not None else ETAG_CACHE_TTL_DAYS) * 86400

    def lookup(self, cache_key: str) -> Optional[CacheEntry]:
        if self._cache is None:
            return None
        return cast(Optional[CacheEntry], self._cache.get(cache_key))

    def store(self, cache_key: str, etag: str, body: bytes, content_type: Optional[str]) -> None:
        if self._cache is None or not etag:
            return
        self._cache.set(cache_key, CacheEntry(etag, body, content_type), expire=self._ttl_seconds)

    def prune_expired(self) -> int:
        """Proactively delete expired entries; lookups also self-clean. Returns count removed."""
        if self._cache is None:
            return 0
        return self._cache.expire()

    def close(self) -> None:
        if self._cache is not None:
            self._cache.close()


_default_cache: Optional[GithubEtagCache] = None


def get_default_cache() -> GithubEtagCache:
    """Return the process-wide singleton cache, constructing it on first call."""
    global _default_cache
    if _default_cache is None:
        _default_cache = GithubEtagCache()
    return _default_cache


def reset_default_cache_for_tests() -> None:
    """Drop the singleton so the next get_default_cache() re-reads env vars."""
    global _default_cache
    if _default_cache is not None:
        _default_cache.close()
    _default_cache = None
