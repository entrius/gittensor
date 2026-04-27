#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Pytest configuration for utils tests.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_github_etag_cache(tmp_path, monkeypatch):
    """Point the ETag cache at a per-test tmp path so tests never touch ~/.gittensor."""
    monkeypatch.setenv('GITTENSOR_ETAG_CACHE_PATH', str(tmp_path / 'etag'))
    monkeypatch.delenv('GITTENSOR_ETAG_CACHE_DISABLED', raising=False)

    github_etag_cache = pytest.importorskip('gittensor.utils.github_etag_cache')
    github_etag_cache.reset_default_cache_for_tests()
    github_etag_cache.snapshot_and_reset()
    yield
    github_etag_cache.reset_default_cache_for_tests()
    github_etag_cache.snapshot_and_reset()
