import math
from datetime import datetime, timedelta, timezone
from typing import Optional

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


def parse_optional_github_iso_to_utc(value: Optional[str]) -> Optional[datetime]:
    """``parse_github_iso_to_utc`` lifted to handle ``Optional[str]`` inputs.

    Returns ``None`` when the input is falsy (``None`` or empty string), letting
    callers feed ``data.get(...)`` straight through without per-site None-checks.
    """
    return parse_github_iso_to_utc(value) if value else None


def parse_github_timestamp_to_cst(timestamp_str: str) -> datetime:
    """
    Parse GitHub's ISO format timestamp and convert to Chicago timezone.
    GitHub returns timestamps like: 2024-01-15T10:30:00Z
    """
    return parse_github_iso_to_utc(timestamp_str).astimezone(CHICAGO_TZ)


def lookback_cutoff(lookback_days: int) -> datetime:
    """Return the lookback cutoff floored to 00:00:00 UTC.

    Quantising to the start of the UTC day ensures that multiple validators
    computing ``datetime.now()`` at different wall-clock times within the same
    UTC day all arrive at the *identical* cutoff, eliminating the discrete
    include/exclude flip described in #1003.
    """
    now = datetime.now(timezone.utc)
    raw = now - timedelta(days=lookback_days)
    return raw.replace(hour=0, minute=0, second=0, microsecond=0)


def calculate_time_decay(merged_at: datetime) -> float:
    """Calculate sigmoid-based time decay multiplier from a merge timestamp."""
    now = datetime.now(timezone.utc)
    hours_since_merge = (now - merged_at).total_seconds() / SECONDS_PER_HOUR

    if hours_since_merge < TIME_DECAY_GRACE_PERIOD_HOURS:
        return 1.0

    days_since_merge = hours_since_merge / 24
    sigmoid = 1 / (1 + math.exp(TIME_DECAY_SIGMOID_STEEPNESS_SCALAR * (days_since_merge - TIME_DECAY_SIGMOID_MIDPOINT)))
    return max(sigmoid, TIME_DECAY_MIN_MULTIPLIER)
