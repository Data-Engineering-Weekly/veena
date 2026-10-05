"""Snowflake backend. Render is tested offline; execution needs a live connection.

Pass `connect` returning a `snowflake.connector` connection, or any DB-API connection whose
`cursor().execute(sql)` returns a cursor. Snowflake autocommits DDL, so interface switches are
staged, one `InterfaceSwitched` event each.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from veena.backends.sql import SqlBackend
from veena.spec.model import TimeBucket

if TYPE_CHECKING:
    from collections.abc import Callable

    from veena.models.protocol import Drafter


class _CursorConnection:
    """Adapts a DB-API connection (`cursor().execute`) to the `execute(sql) -> cursor` slice."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(self, sql: str) -> Any:
        cursor = self._raw.cursor()
        cursor.execute(sql)
        return cursor


class SnowflakeBackend(SqlBackend):
    """Renders Snowflake SQL and runs it through a DB-API connection."""

    name = "snowflake"
    dialect = "snowflake"
    atomic_ddl = False

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        drafter: Drafter | None = None,
        unsupported: frozenset[str] = frozenset(),
    ) -> None:
        """Open the connection lazily through `connect`."""
        self._connect = connect
        self._opened: Any | None = None
        super().__init__(_LazyConnection(self), drafter=drafter, unsupported=unsupported)

    def _connection(self) -> Any:
        if self._opened is None:
            raw = self._connect()
            self._opened = raw if hasattr(raw, "execute") else _CursorConnection(raw)
        return self._opened

    def ts_literal(self, value: datetime) -> str:
        """Snowflake takes `TIMESTAMP_TZ` through a cast."""
        return f"'{value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} +0000'::TIMESTAMP_TZ"

    def local_timestamp(self, expression: str, timezone: str) -> str:
        """Snowflake converts zones with CONVERT_TIMEZONE."""
        return f"CONVERT_TIMEZONE('{timezone}', {expression})"

    def bucket(self, step_bucket: TimeBucket, expression: str, timezone: str) -> str:
        """Truncate to a date in the target zone."""
        local = self.local_timestamp(expression, timezone)
        if step_bucket is TimeBucket.DAY:
            return f"CAST({local} AS DATE)"
        return f"CAST(DATE_TRUNC('MONTH', {local}) AS DATE)"

    def create_table(self, name: str, sql: str) -> str:
        """Transient tables skip Fail-safe; implementations are rebuildable."""
        return f"CREATE OR REPLACE TRANSIENT TABLE {name} AS {sql}"


class _LazyConnection:
    def __init__(self, backend: SnowflakeBackend) -> None:
        self._backend = backend

    def execute(self, sql: str) -> Any:
        return self._backend._connection().execute(sql)
