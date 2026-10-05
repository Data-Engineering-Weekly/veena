"""Calendar arithmetic for complete-day windows."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def last_complete_day(as_of: datetime, timezone: str) -> date:
    """The most recent calendar day in `timezone` that had ended at `as_of`."""
    local = as_of.astimezone(ZoneInfo(timezone))
    return local.date() - timedelta(days=1)


def window_bounds(as_of: datetime, days: int, timezone: str) -> tuple[datetime, datetime]:
    """UTC instants `[start, end)` covering the trailing `days` complete days in `timezone`.

    Args:
        as_of: A timezone-aware instant.
        days: Number of complete calendar days.
        timezone: IANA zone that defines the day boundary.

    Returns:
        Start and end as UTC datetimes. `end` is the local midnight that began the current day.
    """
    zone = ZoneInfo(timezone)
    end_local = datetime.combine(
        last_complete_day(as_of, timezone) + timedelta(days=1), time(), zone
    )
    start_local = datetime.combine(end_local.date() - timedelta(days=days), time(), zone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def sql_timestamptz(value: datetime) -> str:
    """Render a UTC instant as a DuckDB TIMESTAMPTZ literal."""
    return f"TIMESTAMPTZ '{value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')}+00'"
