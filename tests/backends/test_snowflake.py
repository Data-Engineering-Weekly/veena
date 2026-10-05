from datetime import UTC, datetime

import pytest
import sqlglot

from tests.conftest import AS_OF, bundled_spec
from veena.backends import DuckDBBackend, SnowflakeBackend
from veena.planner import compile_plan


class FakeCursor:
    def __init__(self, rows, description=None):
        self._rows = rows
        self.description = description or [("customer_id",), ("spend_usd",)]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeSnowflakeConnection:
    """Records every statement and answers with canned rows."""

    def __init__(self):
        self.statements: list[str] = []

    def cursor(self):
        return self

    def execute(self, sql: str):
        self.statements.append(sql)
        lowered = sql.lower().strip()
        if lowered.startswith("select count(*)"):
            return FakeCursor([(200,)])
        if lowered.startswith("select min("):
            return FakeCursor(
                [(datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC))]
            )
        if lowered.startswith("select max("):
            return FakeCursor([(datetime(2026, 9, 30, tzinfo=UTC),)])
        return FakeCursor([])


def test_snowflake_render_parses_in_the_snowflake_dialect(semantic):
    conn = FakeSnowflakeConnection()
    backend = SnowflakeBackend(lambda: conn)
    for name in ("customer_spend_30d", "customer_spend_daily"):
        plan = compile_plan(bundled_spec(name), semantic)
        sql, drafted = backend.render(plan, as_of=AS_OF)
        assert drafted == ()
        sqlglot.parse_one(sql, read="snowflake")
        assert "::TIMESTAMP_TZ" in sql and "AT TIME ZONE" not in sql
    daily_sql, _ = backend.render(
        compile_plan(bundled_spec("customer_spend_daily"), semantic), as_of=AS_OF
    )
    assert "CONVERT_TIMEZONE('UTC', event_at)" in daily_sql


def test_duckdb_and_snowflake_render_the_same_plan_differently_but_equivalently(
    semantic, scenario_conn
):
    plan = compile_plan(bundled_spec("customer_spend_30d"), semantic)
    duck_sql, _ = DuckDBBackend(scenario_conn).render(plan, as_of=AS_OF)
    snow_sql, _ = SnowflakeBackend(FakeSnowflakeConnection).render(plan, as_of=AS_OF)
    assert duck_sql.count("AS (") == snow_sql.count("AS (") == 6
    assert "TIMESTAMPTZ '2026-09-01 00:00:00+00'" in duck_sql
    assert "'2026-09-01 00:00:00 +0000'::TIMESTAMP_TZ" in snow_sql


def test_snowflake_conformance_sequence_is_staged(semantic):
    conn = FakeSnowflakeConnection()
    backend = SnowflakeBackend(lambda: conn)
    plan = compile_plan(bundled_spec("customer_spend_30d"), semantic)
    sql, _ = backend.render(plan, as_of=AS_OF)
    progress = backend.materialize("impl_a", sql, plan)
    assert progress.row_count == 200 and "purchases" in progress.positions
    assert conn.statements[0].startswith("CREATE OR REPLACE TRANSIENT TABLE impl_a AS")
    assert backend.atomic_publish is False
    backend.publish_many({"customer_spend_30d": "impl_a", "customer_spend_90d": "impl_b"})
    views = [s for s in conn.statements if s.startswith("CREATE OR REPLACE VIEW")]
    assert len(views) == 2 and not any(s == "BEGIN" for s in conn.statements)
    bounds = backend.source_bounds("purchases", "purchased_at")
    assert bounds is not None and bounds[0].year == 2026
    backend.unpublish("customer_spend_90d")
    assert conn.statements[-1] == "DROP VIEW IF EXISTS customer_spend_90d"


def test_conformance_same_protocol_surface(scenario_conn):
    for backend in (DuckDBBackend(scenario_conn), SnowflakeBackend(FakeSnowflakeConnection)):
        for method in (
            "render",
            "materialize",
            "publish",
            "unpublish",
            "publish_many",
            "execute",
            "measure_rows_processed",
            "source_bounds",
        ):
            assert callable(getattr(backend, method)), method
        assert isinstance(backend.atomic_publish, bool)
    with pytest.raises(Exception):  # noqa: B017  (any failure before a network call is the point)
        SnowflakeBackend(lambda: (_ for _ in ()).throw(RuntimeError("no network"))).execute(
            "select 1"
        )
