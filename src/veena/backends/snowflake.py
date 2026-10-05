"""Snowflake backend. Render is tested offline; execution needs a live connection.

Pass `connect` returning a `snowflake.connector` connection, or any DB-API connection whose
`cursor().execute(sql)` returns a cursor. Snowflake autocommits DDL, so interface switches are
staged, one `InterfaceSwitched` event each.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from veena.backends.sql import SqlBackend
from veena.sqldialect import SnowflakeDialect, SqlDialect

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
    dialect: ClassVar[SqlDialect] = SnowflakeDialect()
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

    def close(self) -> None:
        """Close the live connection if one was opened."""
        if self._opened is not None:
            raw = getattr(self._opened, "_raw", self._opened)
            close = getattr(raw, "close", None)
            if callable(close):
                close()
            self._opened = None

    def create_table(self, name: str, sql: str) -> str:
        """Transient tables skip Fail-safe; implementations are rebuildable."""
        return f"CREATE OR REPLACE TRANSIENT TABLE {name} AS {sql}"


class _LazyConnection:
    def __init__(self, backend: SnowflakeBackend) -> None:
        self._backend = backend

    def execute(self, sql: str) -> Any:
        return self._backend._connection().execute(sql)
