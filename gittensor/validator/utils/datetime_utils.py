from datetime import datetime
from typing import Optional

import pytz

CHICAGO_TZ = pytz.timezone('America/Chicago')


def parse_github_timestamp_to_cst(timestamp_str: str) -> datetime:
    """
    Parse GitHub's ISO format timestamp and convert to Chicago timezone.
    GitHub returns timestamps like: 2024-01-15T10:30:00Z
    """
    # Parse the UTC timestamp
    utc_dt = datetime.fromisoformat(timestamp_str.rstrip('Z'))

    # Add UTC timezone info
    utc_dt = pytz.utc.localize(utc_dt)

    # Convert to Chicago timezone
    chicago_dt = utc_dt.astimezone(CHICAGO_TZ)

    return chicago_dt


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string to datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
