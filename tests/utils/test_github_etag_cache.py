#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for the on-disk GitHub ETag cache."""

import time

import pytest

github_etag_cache = pytest.importorskip(
    'gittensor.utils.github_etag_cache', reason='Requires gittensor package with all dependencies'
)

GithubEtagCache = github_etag_cache.GithubEtagCache
build_request_key = github_etag_cache.build_request_key
snapshot_and_reset = github_etag_cache.snapshot_and_reset


@pytest.fixture(autouse=True)
def _reset_counters():
    snapshot_and_reset()
    yield
    snapshot_and_reset()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.delenv('GITTENSOR_ETAG_CACHE_DISABLED', raising=False)
    c = GithubEtagCache(root=tmp_path / 'etag', ttl_days=30)
    yield c
    c.close()


class TestBuildRequestKey:
    def test_sorts_params_for_stable_key(self):
        k1 = build_request_key('https://api.github.com/x', {'b': 1, 'a': 2})
        k2 = build_request_key('https://api.github.com/x', {'a': 2, 'b': 1})
        assert k1 == k2

    def test_no_params_omits_query(self):
        assert build_request_key('https://api.github.com/x') == 'GET https://api.github.com/x'


class TestStoreAndLookup:
    def test_round_trip_with_non_ascii(self, cache):
        body = b'\xc3\xa9\x00\xff binary content'
        cache.store('GET https://api.github.com/x', 'W/"abc"', body, 'application/json')

        result = cache.lookup('GET https://api.github.com/x')
        assert result is not None
        assert result.etag == 'W/"abc"'
        assert result.body == body
        assert result.content_type == 'application/json'

    def test_miss_returns_none(self, cache):
        assert cache.lookup('GET https://api.github.com/missing') is None

    def test_empty_etag_is_not_stored(self, cache):
        cache.store('GET https://api.github.com/x', '', b'body', 'text/plain')
        assert cache.lookup('GET https://api.github.com/x') is None

    def test_overwrite_replaces_entry(self, cache):
        key = 'GET https://api.github.com/x'
        cache.store(key, 'v1', b'one', 'text/plain')
        cache.store(key, 'v2', b'two', 'text/plain')

        result = cache.lookup(key)
        assert result is not None
        assert result.etag == 'v2' and result.body == b'two'


class TestTtl:
    def test_expired_entry_returns_none(self, tmp_path):
        # ttl_days=0 yields 0-second expiry → next lookup sees expired entry.
        cache = GithubEtagCache(root=tmp_path / 'etag', ttl_days=0)
        cache.store('GET https://x/k', 'e', b'body', 'text/plain')
        time.sleep(0.01)
        assert cache.lookup('GET https://x/k') is None
        cache.close()

    def test_prune_expired_drops_entries(self, tmp_path):
        cache = GithubEtagCache(root=tmp_path / 'etag', ttl_days=0)
        cache.store('GET https://x/a', 'a', b'one', 'text/plain')
        cache.store('GET https://x/b', 'b', b'two', 'text/plain')
        time.sleep(0.01)

        cache.prune_expired()
        assert cache.lookup('GET https://x/a') is None
        assert cache.lookup('GET https://x/b') is None
        cache.close()


class TestDisabled:
    def test_disabled_store_and_lookup_are_noops(self, tmp_path, monkeypatch):
        monkeypatch.setenv('GITTENSOR_ETAG_CACHE_DISABLED', '1')
        cache = GithubEtagCache(root=tmp_path / 'etag', ttl_days=30)

        cache.store('GET https://x/y', 'e', b'body', 'text/plain')
        assert cache.lookup('GET https://x/y') is None
        cache.close()

    def test_disabled_skips_disk_allocation(self, tmp_path, monkeypatch):
        monkeypatch.setenv('GITTENSOR_ETAG_CACHE_DISABLED', '1')
        root = tmp_path / 'etag'

        cache = GithubEtagCache(root=root, ttl_days=30)
        cache.store('GET https://x', 'e', b'body', 'text/plain')
        cache.prune_expired()
        cache.close()

        assert not root.exists()


class TestCounters:
    def test_record_hit_and_miss(self):
        github_etag_cache.record_hit()
        github_etag_cache.record_hit()
        github_etag_cache.record_miss()
        assert snapshot_and_reset() == (2, 1)

    def test_snapshot_resets_counters(self):
        github_etag_cache.record_hit()
        github_etag_cache.record_miss()
        snapshot_and_reset()
        assert snapshot_and_reset() == (0, 0)
