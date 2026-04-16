import math
import warnings
from datetime import datetime, timezone

import pytz

from gittensor.constants import (
    SECONDS_PER_HOUR,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
)

CHICAGO_TZ = pytz.timezone('America/Chicago')


def parse_github_iso_to_utc(timestamp_str: str) -> datetime:
    """Parse a GitHub-style ISO 8601 string to a timezone-aware UTC datetime.

    Accepts common GraphQL/REST shapes such as ``2024-01-15T10:30:00Z`` or
    values with a numeric UTC offset.
    """
    s = timestamp_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_github_timestamp_to_cst(timestamp_str: str) -> datetime:
    """Deprecated: prefer ``parse_github_iso_to_utc``.

    The Chicago timezone conversion is a no-op for the downstream arithmetic
    (subtraction, comparison) used across the codebase, and the wrapper plus
    the ``pytz`` dependency will be removed in a future release.
    """
    warnings.warn(
        'parse_github_timestamp_to_cst is deprecated; use parse_github_iso_to_utc instead.',
        DeprecationWarning,
        stacklevel=2,
    )
    return parse_github_iso_to_utc(timestamp_str).astimezone(CHICAGO_TZ)


def calculate_time_decay(merged_at: datetime) -> float:
    """Calculate sigmoid-based time decay multiplier from a merge timestamp."""
    now = datetime.now(timezone.utc)
    hours_since_merge = (now - merged_at).total_seconds() / SECONDS_PER_HOUR

    if hours_since_merge < TIME_DECAY_GRACE_PERIOD_HOURS:
        return 1.0

    days_since_merge = hours_since_merge / 24
    sigmoid = 1 / (1 + math.exp(TIME_DECAY_SIGMOID_STEEPNESS_SCALAR * (days_since_merge - TIME_DECAY_SIGMOID_MIDPOINT)))
    return max(sigmoid, TIME_DECAY_MIN_MULTIPLIER)
