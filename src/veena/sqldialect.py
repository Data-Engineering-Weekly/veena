"""SQL dialects: the few expressions that differ between engines.

Both the backend renderer and verification use a dialect, so the same spec yields runnable SQL
on each engine. Verification imports this module, never the backends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from veena.spec.model import TimeBucket

if TYPE_CHECKING:
    from collections.abc import Sequence


class SqlDialect(Protocol):
    """What an engine needs to say differently."""

    @property
    def name(self) -> str:
        """Sqlglot dialect name."""
        ...

    def ts_literal(self, value: datetime) -> str:
        """A timezone-aware timestamp literal for a UTC instant."""
        ...

    def as_tz(self, expression: str) -> str:
        """Cast a timestamp expression to the engine's timezone-aware type."""
        ...

    def local_timestamp(self, expression: str, timezone: str) -> str:
        """Wall-clock time of a timezone-aware expression in `timezone`."""
        ...

    def bucket(self, bucket: TimeBucket, expression: str, timezone: str) -> str:
        """Truncate a timezone-aware expression to a date bucket in `timezone`."""
        ...

    def count_distinct_rows(self, columns: Sequence[str]) -> str:
        """Count distinct combinations of several columns."""
        ...

    def session_setup(self) -> tuple[str, ...]:
        """Statements to run once per connection so naive timestamps resolve in UTC."""
        ...


class DuckDBDialect:
    """DuckDB, also the base the SQL backend assumes."""

    name = "duckdb"

    def ts_literal(self, value: datetime) -> str:
        """`TIMESTAMPTZ 'YYYY-MM-DD HH:MM:SS+00'`."""
        return f"TIMESTAMPTZ '{value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')}+00'"

    def as_tz(self, expression: str) -> str:
        """Cast to TIMESTAMPTZ; with the session zone at UTC a naive value is read as UTC."""
        return f"CAST({expression} AS TIMESTAMPTZ)"

    def local_timestamp(self, expression: str, timezone: str) -> str:
        """`AT TIME ZONE` converts a TIMESTAMPTZ to wall-clock time."""
        return f"{expression} AT TIME ZONE '{timezone}'"

    def bucket(self, bucket: TimeBucket, expression: str, timezone: str) -> str:
        """Date of the local day, or first day of the local month."""
        local = self.local_timestamp(expression, timezone)
        if bucket is TimeBucket.DAY:
            return f"CAST({local} AS DATE)"
        return f"CAST(DATE_TRUNC('month', {local}) AS DATE)"

    def count_distinct_rows(self, columns: Sequence[str]) -> str:
        """DuckDB counts distinct row tuples."""
        return f"count(DISTINCT ({', '.join(columns)}))"

    def session_setup(self) -> tuple[str, ...]:
        """Pin the session zone so naive timestamps are not read in the machine's zone."""
        return ("SET TimeZone = 'UTC'",)


class SnowflakeDialect:
    """Snowflake."""

    name = "snowflake"

    def ts_literal(self, value: datetime) -> str:
        """A string cast to TIMESTAMP_TZ."""
        return f"'{value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} +0000'::TIMESTAMP_TZ"

    def as_tz(self, expression: str) -> str:
        """TO_TIMESTAMP_TZ; with the session zone at UTC a naive value is read as UTC."""
        return f"TO_TIMESTAMP_TZ({expression})"

    def local_timestamp(self, expression: str, timezone: str) -> str:
        """CONVERT_TIMEZONE to the target zone."""
        return f"CONVERT_TIMEZONE('{timezone}', {expression})"

    def bucket(self, bucket: TimeBucket, expression: str, timezone: str) -> str:
        """Date of the local day, or first day of the local month."""
        local = self.local_timestamp(expression, timezone)
        if bucket is TimeBucket.DAY:
            return f"CAST({local} AS DATE)"
        return f"CAST(DATE_TRUNC('MONTH', {local}) AS DATE)"

    def count_distinct_rows(self, columns: Sequence[str]) -> str:
        """Snowflake takes several arguments to COUNT(DISTINCT ...)."""
        return f"COUNT(DISTINCT {', '.join(columns)})"

    def session_setup(self) -> tuple[str, ...]:
        """Pin the session zone."""
        return ("ALTER SESSION SET TIMEZONE = 'UTC'",)


__all__ = ["DuckDBDialect", "SnowflakeDialect", "SqlDialect"]
