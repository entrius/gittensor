import math
from datetime import datetime, timezone
from typing import Optional

from gittensor.constants import SECONDS_PER_HOUR
from gittensor.validator.utils.load_weights import ResolvedTimeDecay


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


def calculate_time_decay(
    merged_at: datetime,
    time_decay: ResolvedTimeDecay,
    *,
    reference_time: Optional[datetime] = None,
) -> float:
    """Calculate sigmoid-based time decay multiplier from a merge timestamp.

    When ``reference_time`` is set (validator round anchor), every PR in the
    round uses the same clock so decay and lookback windows stay consistent
    across miners and scoring phases.
    """
    now = reference_time if reference_time is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    hours_since_merge = (now - merged_at).total_seconds() / SECONDS_PER_HOUR

    if hours_since_merge < time_decay.grace_period_hours:
        return 1.0

    days_since_merge = hours_since_merge / 24
    sigmoid = 1 / (1 + math.exp(time_decay.sigmoid_steepness * (days_since_merge - time_decay.sigmoid_midpoint_days)))
    return max(sigmoid, time_decay.min_multiplier)
