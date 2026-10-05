"""DuckDB backend: the development and test engine."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from veena.backends.sql import SqlBackend
from veena.sqldialect import DuckDBDialect, SqlDialect

if TYPE_CHECKING:
    import duckdb

    from veena.models.protocol import Drafter


class DuckDBBackend(SqlBackend):
    """DuckDB speaks the base dialect and runs DDL inside transactions."""

    name = "duckdb"
    dialect: ClassVar[SqlDialect] = DuckDBDialect()
    atomic_ddl = True

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        drafter: Drafter | None = None,
        unsupported: frozenset[str] = frozenset(),
    ) -> None:
        """Wrap an open DuckDB connection."""
        super().__init__(conn, drafter=drafter, unsupported=unsupported)
