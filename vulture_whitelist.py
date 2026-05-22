# Vulture whitelist: each bare name below counts as a use, suppressing the
# matching report. Prefer this over `[tool.vulture] ignore_names` (global).

# `__exit__(self, exc_type, exc, tb)` - PEP 343 signature, body unused args
exc_type  # noqa: F821
tb  # noqa: F821

# `ttl_cache` lru_cache cache-buster arg (gittensor/utils/misc.py)
ttl_hash  # noqa: F821
