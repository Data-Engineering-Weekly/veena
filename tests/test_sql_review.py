"""Regression tests for the SQL and verification review findings."""

from datetime import date

import pytest
from pydantic import BaseModel, ValidationError

from tests.conftest import AS_OF, bundled_spec, make_spec
from veena.backends import DraftStepRequest, DuckDBBackend
from veena.errors import PlanError, VerificationError
from veena.evidence import Grade
from veena.models import ScriptedDrafter
from veena.planner import PopulationJoin, compile_plan
from veena.refs import SemanticRef
from veena.semantic.model import IdentityPolicy, Source, TimeRule
from veena.spec import Aggregation, AssetSpec, Measure, TimeBucket, Window
from veena.sqldialect import SnowflakeDialect
from veena.verification import build_checks, equivalence, verify


def test_bucketed_spec_cannot_include_inactive_members():
    daily = bundled_spec("customer_spend_daily").model_dump()
    daily["population"]["include_inactive"] = True
    with pytest.raises(ValidationError, match="cannot include inactive members"):
        AssetSpec.model_validate(daily)


def test_count_measure_passes_conservation(scenario_conn, semantic):
    spec = make_spec("customer_orders_30d").model_copy(
        update={
            "measures": (
                Measure(
                    name="purchase_count",
                    calculation=SemanticRef(name="purchase_count", version=1),
                    aggregation=Aggregation.SUM,
                ),
            )
        }
    )
    backend = DuckDBBackend(scenario_conn)
    plan = compile_plan(spec, semantic)
    sql, _ = backend.render(plan, as_of=AS_OF)
    backend.materialize("orders_30d", sql, plan)
    outcome = verify(spec, semantic, "orders_30d", backend.execute, as_of=AS_OF)
    assert outcome.passed, [c for c in outcome.checks if not c.passed]
    counted = scenario_conn.execute("SELECT sum(purchase_count) FROM orders_30d").fetchone()[0]
    settled = scenario_conn.execute(
        "SELECT count(*) FROM purchases WHERE status = 'settled' "
        "AND purchased_at >= TIMESTAMPTZ '2026-09-01 00:00:00+00' "
        "AND purchased_at < TIMESTAMPTZ '2026-10-01 00:00:00+00'"
    ).fetchone()[0]
    assert counted == settled


def test_window_with_both_fields_uses_the_same_days_in_planner_and_verification(
    scenario_conn, semantic
):
    spec = make_spec().model_copy(
        update={"window": Window(complete_calendar_days=30, history_days=90, timezone="UTC")}
    )
    assert spec.window.effective_days(TimeBucket.NONE) == 30
    assert spec.window.effective_days(TimeBucket.DAY) == 90
    backend = DuckDBBackend(scenario_conn)
    plan = compile_plan(spec, semantic)
    sql, _ = backend.render(plan, as_of=AS_OF)
    backend.materialize("both_fields", sql, plan)
    outcome = verify(spec, semantic, "both_fields", backend.execute, as_of=AS_OF)
    assert outcome.passed, [c.detail for c in outcome.checks if not c.passed]


def test_verification_sql_follows_the_dialect(semantic):
    checks = build_checks(
        bundled_spec("customer_spend_daily"),
        semantic,
        "rel",
        as_of=AS_OF,
        dialect=SnowflakeDialect(),
    )
    text = "\n".join(c.sql for c in checks)
    assert "AT TIME ZONE" not in text and "TIMESTAMPTZ '" not in text
    assert "CONVERT_TIMEZONE('UTC'" in text and "::TIMESTAMP_TZ" in text
    assert "COUNT(DISTINCT customer_id, day)" in text
    duck = "\n".join(
        c.sql
        for c in build_checks(bundled_spec("customer_spend_daily"), semantic, "rel", as_of=AS_OF)
    )
    assert "count(DISTINCT (customer_id, day))" in duck


def _direct_model(semantic, fact_columns):
    facts = Source(name="orders", table="orders", primary_key=("order_id",), columns=fact_columns)
    rule = TimeRule(name="order_time", version=1, source="orders", timestamp_column="ordered_at")
    policy = IdentityPolicy(
        name="orders_customer_direct",
        version=1,
        kind="direct",
        entity=SemanticRef(name="customer", version=1),
        fact_source="orders",
        fact_key="buyer_id",
        fact_timestamp="ordered_at",
    )
    calc = semantic.calculations[0].model_copy(
        update={"name": "order_total", "version": 1, "source": "orders", "expression": "amount"}
    )
    return semantic.model_copy(
        update={
            "sources": (*semantic.sources, facts),
            "time_rules": (*semantic.time_rules, rule),
            "identity_policies": (*semantic.identity_policies, policy),
            "calculations": (*semantic.calculations, calc),
        }
    )


def test_direct_identity_refuses_a_duplicate_key_column(semantic):
    spec = make_spec("orders_30d", identity="semantic:orders_customer_direct@1").model_copy(
        update={
            "measures": (
                Measure(
                    name="order_total",
                    calculation=SemanticRef(name="order_total", version=1),
                    aggregation=Aggregation.SUM,
                ),
            )
        }
    )
    clashing = _direct_model(
        semantic, ("order_id", "buyer_id", "customer_id", "ordered_at", "amount")
    )
    with pytest.raises(PlanError, match="already has a column named 'customer_id'"):
        compile_plan(spec, clashing)
    with pytest.raises(VerificationError, match="would produce two"):
        build_checks(spec, clashing, "rel", as_of=AS_OF)
    clean = _direct_model(semantic, ("order_id", "buyer_id", "ordered_at", "amount"))
    plan = compile_plan(spec, clean)
    assert plan.output_columns == ("customer_id", "order_total")


def test_direct_identity_renders_and_verifies_on_duckdb(scenario_conn, semantic):
    scenario_conn.execute(
        "CREATE TABLE orders AS SELECT purchase_id AS order_id, m.customer_id AS buyer_id, "
        "purchased_at AS ordered_at, amount_usd AS amount "
        "FROM purchases p JOIN identity_mappings m ON m.account_id = p.account_id "
        "AND p.purchased_at >= m.valid_from "
        "AND (m.valid_to IS NULL OR p.purchased_at < m.valid_to)"
    )
    model = _direct_model(semantic, ("order_id", "buyer_id", "ordered_at", "amount"))
    spec = make_spec("orders_30d", identity="semantic:orders_customer_direct@1").model_copy(
        update={
            "measures": (
                Measure(
                    name="order_total",
                    calculation=SemanticRef(name="order_total", version=1),
                    aggregation=Aggregation.SUM,
                ),
            )
        }
    )
    backend = DuckDBBackend(scenario_conn)
    plan = compile_plan(spec, model)
    sql, _ = backend.render(plan, as_of=AS_OF)
    assert "f.buyer_id AS customer_id" in sql
    backend.materialize("orders_30d", sql, plan)
    outcome = verify(spec, model, "orders_30d", backend.execute, as_of=AS_OF)
    assert outcome.passed, [c.detail for c in outcome.checks if not c.passed]


def test_draft_table_check_uses_qualified_names(scenario_conn, semantic):
    plan = compile_plan(bundled_spec("customer_spend_30d"), semantic)
    scenario_conn.execute(
        "CREATE SCHEMA other; CREATE TABLE other.purchases AS SELECT * FROM purchases LIMIT 1"
    )

    def draft(req: BaseModel) -> str:
        assert isinstance(req, DraftStepRequest)
        return (
            "SELECT purchase_id, account_id, purchased_at, amount_usd, status FROM other.purchases"
        )

    backend = DuckDBBackend(
        scenario_conn, drafter=ScriptedDrafter(draft), unsupported=frozenset({"source_scan"})
    )
    with pytest.raises(PlanError, match=r"reads \['other\.purchases'\]"):
        backend.render(plan, as_of=AS_OF)

    def function_draft(req: BaseModel) -> str:
        return "SELECT * FROM read_csv('/etc/passwd')"

    backend = DuckDBBackend(
        scenario_conn,
        drafter=ScriptedDrafter(function_draft),
        unsupported=frozenset({"source_scan"}),
    )
    with pytest.raises(PlanError, match="does not declare"):
        backend.render(plan, as_of=AS_OF)


def test_timezones_and_identifiers_are_validated():
    with pytest.raises(ValidationError, match="unknown timezone"):
        make_spec(timezone="Region/Country")
    with pytest.raises(ValidationError, match="outside"):
        make_spec(timezone="UTC'; --")
    assert (
        make_spec(timezone="America/Argentina/Buenos_Aires").window.timezone
        == "America/Argentina/Buenos_Aires"
    )
    with pytest.raises(ValidationError):
        Source(name="s", table="raw.public.t; drop table x", primary_key=("id",), columns=("id",))
    with pytest.raises(ValidationError):
        Source(name="s", table="t", primary_key=("id",), columns=("lower(customer_id)",))
    with pytest.raises(ValidationError):
        TimeRule(name="r", version=1, source="t", timestamp_column="ts", timezone="Nowhere/Land")


def test_non_additive_measures_are_carried_not_zero_filled(semantic):
    spec = make_spec(aggregation=Aggregation.MAX)
    plan = compile_plan(spec, semantic)
    join = plan.step(PopulationJoin)
    assert join.zero_fill == () and join.carry == ("spend_usd",)


def test_equivalence_treats_null_keys_as_equal(scenario_conn):
    scenario_conn.execute(
        "CREATE TABLE a (k VARCHAR, v DECIMAL(12,2)); INSERT INTO a VALUES (NULL, 1), ('x', 2)"
    )
    scenario_conn.execute(
        "CREATE TABLE b (k VARCHAR, v DECIMAL(12,2)); INSERT INTO b VALUES (NULL, 1), ('x', 2)"
    )
    same = equivalence("a", "b", ("k",), ("v",), DuckDBBackend(scenario_conn).execute)
    assert same.passed, same.detail
    scenario_conn.execute("UPDATE b SET v = NULL WHERE k = 'x'")
    differs = equivalence("a", "b", ("k",), ("v",), DuckDBBackend(scenario_conn).execute)
    assert not differs.passed


def test_grade_unaffected_by_review_changes(scenario_conn, semantic):
    backend = DuckDBBackend(scenario_conn)
    plan = compile_plan(bundled_spec("customer_spend_30d"), semantic)
    sql, _ = backend.render(plan, as_of=AS_OF)
    backend.materialize("spend30", sql, plan)
    outcome = verify(
        bundled_spec("customer_spend_30d"), semantic, "spend30", backend.execute, as_of=AS_OF
    )
    assert outcome.passed and outcome.grade is Grade.RECONCILIATION
    rows = scenario_conn.execute("SELECT * FROM spend30 ORDER BY 1").fetchall()
    from veena.scenario import approved_spend_sql

    want = scenario_conn.execute(approved_spend_sql(30, date(2026, 9, 30))).fetchall()
    assert [(r[0], float(r[1])) for r in rows] == [(w[0], float(w[1])) for w in want]
