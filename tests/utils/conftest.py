#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Pytest configuration for utils tests.
"""

import pytest


@pytest.fixture
def clear_github_cache():
    """Clear the GitHub user cache before and after test."""
    # Import here to avoid issues during collection
    try:
        import gittensor.utils.github_api_tools as api_tools
        api_tools._GITHUB_USER_CACHE.clear()
        yield
        api_tools._GITHUB_USER_CACHE.clear()
    except (ImportError, AttributeError):
        yield
