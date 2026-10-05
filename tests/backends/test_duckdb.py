from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from tests.conftest import AS_OF, bundled_spec
from veena.backends import DraftStepRequest, DuckDBBackend
from veena.errors import PlanError
from veena.models import ScriptedDrafter
from veena.planner import IdentityJoin, compile_plan
from veena.scenario import approved_spend_sql, naive_spend_sql

if TYPE_CHECKING:
    from pydantic import BaseModel


def _plan(semantic, name="customer_spend_30d"):
    return compile_plan(bundled_spec(name), semantic)


def _naive(plan):
    steps = tuple(
        s.model_copy(update={"respect_validity": False}) if isinstance(s, IdentityJoin) else s
        for s in plan.steps
    )
    return plan.model_copy(update={"steps": steps})


def test_rendered_plan_reproduces_the_approved_definition(scenario_conn, semantic):
    backend = DuckDBBackend(scenario_conn)
    sql, drafted = backend.render(_plan(semantic), as_of=AS_OF)
    assert drafted == ()
    rows = scenario_conn.execute(f"SELECT * FROM ({sql}) ORDER BY 1").fetchall()
    expected = scenario_conn.execute(approved_spend_sql(30, date(2026, 9, 30))).fetchall()
    assert len(rows) == 200
    assert [(r[0], float(r[1])) for r in rows] == [(e[0], float(e[1])) for e in expected]


def test_naive_join_reproduces_the_bug(scenario_conn, semantic):
    backend = DuckDBBackend(scenario_conn)
    sql, _ = backend.render(_naive(_plan(semantic)), as_of=AS_OF)
    rows = dict(scenario_conn.execute(f"SELECT * FROM ({sql})").fetchall())
    naive = dict(scenario_conn.execute(naive_spend_sql(30, date(2026, 9, 30))).fetchall())
    approved = dict(scenario_conn.execute(approved_spend_sql(30, date(2026, 9, 30))).fetchall())
    assert rows == naive
    assert sum(rows.values()) > sum(approved.values())


def test_window_literal_follows_as_of(scenario_conn, semantic):
    sql, _ = DuckDBBackend(scenario_conn).render(_plan(semantic), as_of=AS_OF)
    assert "TIMESTAMPTZ '2026-09-01 00:00:00+00'" in sql
    assert "TIMESTAMPTZ '2026-10-01 00:00:00+00'" in sql


def test_daily_plan_materializes_with_day_column(scenario_conn, semantic):
    backend = DuckDBBackend(scenario_conn)
    plan = _plan(semantic, "customer_spend_daily")
    sql, _ = backend.render(plan, as_of=AS_OF)
    progress = backend.materialize("daily_i1", sql, plan)
    cols = [r[0] for r in scenario_conn.execute("DESCRIBE daily_i1").fetchall()]
    assert cols == ["customer_id", "day", "spend_usd"]
    assert progress.row_count > 0
    assert "purchases" in progress.positions


def test_materialize_publish_and_repoint(scenario_conn, semantic):
    backend = DuckDBBackend(scenario_conn)
    plan = _plan(semantic)
    sql, _ = backend.render(plan, as_of=AS_OF)
    backend.materialize("impl_a", sql, plan)
    backend.materialize("impl_b", backend.render(_naive(plan), as_of=AS_OF)[0], plan)
    backend.publish("customer_spend_30d", "impl_a")
    before = scenario_conn.execute(
        "SELECT count(*), sum(spend_usd) FROM customer_spend_30d"
    ).fetchone()
    backend.publish("customer_spend_30d", "impl_b")
    after = scenario_conn.execute(
        "SELECT count(*), sum(spend_usd) FROM customer_spend_30d"
    ).fetchone()
    assert before[0] == after[0] == 200
    assert after[1] > before[1]
    view_sql = scenario_conn.execute(
        "SELECT sql FROM duckdb_views() WHERE view_name = 'customer_spend_30d'"
    ).fetchone()[0]
    assert "impl_b" in view_sql


def test_unsupported_step_without_drafter_is_a_plan_error(scenario_conn, semantic):
    backend = DuckDBBackend(scenario_conn, unsupported=frozenset({"window"}))
    with pytest.raises(PlanError, match="no drafter"):
        backend.render(_plan(semantic), as_of=AS_OF)


def test_correct_draft_is_accepted_and_marked(scenario_conn, semantic):
    def draft(req: BaseModel) -> str:
        assert isinstance(req, DraftStepRequest)
        return (
            f"SELECT * FROM {req.input_relation} "
            "WHERE event_at >= TIMESTAMPTZ '2026-09-01 00:00:00+00' "
            "AND event_at < TIMESTAMPTZ '2026-10-01 00:00:00+00'"
        )

    backend = DuckDBBackend(
        scenario_conn, drafter=ScriptedDrafter(draft), unsupported=frozenset({"window"})
    )
    sql, drafted = backend.render(_plan(semantic), as_of=AS_OF)
    assert drafted == ("window",)
    rows = scenario_conn.execute(f"SELECT * FROM ({sql}) ORDER BY 1").fetchall()
    expected = scenario_conn.execute(approved_spend_sql(30, date(2026, 9, 30))).fetchall()
    assert [(r[0], float(r[1])) for r in rows] == [(e[0], float(e[1])) for e in expected]


def test_draft_reading_an_undeclared_table_is_rejected(scenario_conn, semantic):
    def draft(req: BaseModel) -> str:
        assert isinstance(req, DraftStepRequest)
        return f"SELECT s1.* FROM {req.input_relation} JOIN accounts USING (account_id)"

    drafter = ScriptedDrafter(draft)
    backend = DuckDBBackend(scenario_conn, drafter=drafter, unsupported=frozenset({"window"}))
    with pytest.raises(PlanError, match=r"reads \['accounts'\]"):
        backend.render(_plan(semantic), as_of=AS_OF)


def test_draft_with_wrong_columns_is_rejected(scenario_conn, semantic):
    def draft(req: BaseModel) -> str:
        assert isinstance(req, DraftStepRequest)
        return f"SELECT account_id FROM {req.input_relation}"

    drafter = ScriptedDrafter(draft)
    backend = DuckDBBackend(scenario_conn, drafter=drafter, unsupported=frozenset({"window"}))
    with pytest.raises(PlanError, match="produces columns"):
        backend.render(_plan(semantic), as_of=AS_OF)
