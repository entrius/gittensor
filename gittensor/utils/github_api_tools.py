# Entrius 2025
"""Backward-compatible entrypoint for GitHub API utilities."""

import gittensor.utils.github_api as _github_api

for _name in _github_api.__all__:
    globals()[_name] = getattr(_github_api, _name)

__all__ = list(_github_api.__all__)

del _github_api, _name
