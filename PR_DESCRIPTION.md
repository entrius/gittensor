# Fix: classify `gitt miner post` PAT `/user` failures with distinct error codes

## Summary

This change fixes `gitt miner post` local PAT validation by distinguishing GitHub `/user` failures into actionable error categories instead of treating all failures as an invalid or expired token.

## Problem

The current local PAT preflight only returns a bare boolean from `_validate_pat_locally()`. That means:

- `401 Unauthorized` is indistinguishable from
- `5xx`, `408`, or rate-limit-style `403`, and
- `requests.RequestException` network failures.

All of these were reported as:

> GitHub PAT is invalid or expired. Check your GITTENSOR_MINER_PAT.

That is misleading when GitHub is unavailable, rate limited, or unreachable.

## Fix

### Behavior changes

`_validate_pat_locally()` now returns a tuple:
- `pat_valid: bool`
- `error_code: str | None`
- `error_message: str | None`

The `/user` preflight now distinguishes:

- `pat_invalid`: `401` or invalid `403`
- `github_rate_limited`: `429` or rate-limit-style `403`
- `github_unavailable`: `408` or `5xx`
- `github_network_error`: `requests.RequestException`

`_error()` now accepts an optional `error_code` and includes it in JSON output.

### Scope

This fix is intentionally narrow and only affects the local PAT `/user` preflight path in `gitt miner post`. The GraphQL permission check remains unchanged in behavior.

## Files changed

- `gittensor/cli/miner_commands/post.py`
  - Updated `_validate_pat_locally()` return shape and error classification
  - Updated CLI PAT validation flow to emit distinct error messages and `error_code` in JSON

- `gittensor/cli/miner_commands/helpers.py`
  - Extended `_error()` to include optional `error_code` in JSON responses

- `tests/cli/test_miner_commands.py`
  - Updated existing `_validate_pat_locally` patching to the new tuple contract
  - Added focused regression tests for `/user`:
    - `401` → `pat_invalid`
    - `500` → `github_unavailable`
    - `403` with rate-limit evidence → `github_rate_limited`
    - request timeout → `github_network_error`

## Testing

The modified files pass Python syntax checks:

```bash
python3 -m py_compile gittensor/cli/miner_commands/post.py \
    gittensor/cli/miner_commands/helpers.py \
    tests/cli/test_miner_commands.py
```

## Why this matters

Operators no longer get a misleading token-rotation message when GitHub is temporarily unavailable or rate limited. The CLI now provides actionable guidance and machine-readable error classification for automation.

## Commit message

`fix(cli): classify miner post PAT /user failures with distinct error codes`
