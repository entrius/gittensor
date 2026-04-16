import math
from datetime import datetime, timezone

import pytz

from gittensor.constants import (
    SECONDS_PER_HOUR,
    TIME_DECAY_GRACE_PERIOD_HOURS,
    TIME_DECAY_MIN_MULTIPLIER,
    TIME_DECAY_SIGMOID_MIDPOINT,
    TIME_DECAY_SIGMOID_STEEPNESS_SCALAR,
)
from gittensor.utils.github_iso_time import parse_github_utc_iso

CHICAGO_TZ = pytz.timezone('America/Chicago')


def parse_github_timestamp_to_cst(timestamp_str: str) -> datetime:
    """
    Parse GitHub's ISO format timestamp and convert to Chicago timezone.
    GitHub returns timestamps like: 2024-01-15T10:30:00Z
    """
    utc_dt = parse_github_utc_iso(timestamp_str)
    return utc_dt.astimezone(CHICAGO_TZ)


def calculate_time_decay(merged_at: datetime) -> float:
    """Calculate sigmoid-based time decay multiplier from a merge timestamp."""
    now = datetime.now(timezone.utc)
    hours_since_merge = (now - merged_at).total_seconds() / SECONDS_PER_HOUR

    if hours_since_merge < TIME_DECAY_GRACE_PERIOD_HOURS:
        return 1.0

    days_since_merge = hours_since_merge / 24
    sigmoid = 1 / (1 + math.exp(TIME_DECAY_SIGMOID_STEEPNESS_SCALAR * (days_since_merge - TIME_DECAY_SIGMOID_MIDPOINT)))
    return max(sigmoid, TIME_DECAY_MIN_MULTIPLIER)
