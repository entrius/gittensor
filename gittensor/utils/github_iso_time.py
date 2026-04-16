# Entrius 2025
"""Parse GitHub API ISO 8601 timestamps to timezone-aware UTC."""

from datetime import datetime, timezone
from typing import Optional


def parse_github_utc_iso(timestamp_str: str) -> datetime:
    """
    Parse GitHub ISO 8601 timestamps (e.g. ``2024-01-15T10:30:00Z``) to timezone-aware UTC.

    Handles trailing ``Z`` and offset forms returned by REST and GraphQL APIs.
    """
    s = timestamp_str.strip()
    if not s:
        raise ValueError('empty timestamp')
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_github_utc_iso_optional(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse; returns ``None`` on missing or invalid input."""
    if not value:
        return None
    try:
        return parse_github_utc_iso(value)
    except (ValueError, TypeError):
        return None
