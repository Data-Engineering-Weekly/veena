"""Triage input, the rule baseline, and the repair policy table (R6.9a, R6.9b)."""

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from tests.conftest import AS_OF
from veena.evidence import CheckResult, Grade
from veena.models import KeywordClassifier
from veena.policy import CauseClass, RepairPolicy, RepairStrategy
from veena.repair import RuleTriageClassifier, TriageInput, magnitude_of, triage
from veena.scenario import DriftKind, ScenarioConfig, apply_drift, build_scenario

REPAIR = Path(__file__).resolve().parents[1] / "src" / "veena" / "repair.py"


def test_triage_input_has_no_sql_field_and_repair_never_imports_the_planner():
    assert not any("sql" in name for name in TriageInput.model_fields)
    tree = ast.parse(REPAIR.read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(name.startswith(("veena.planner", "veena.backends")) for name in imported)


def check(name, detail, *, structural=False):
    return CheckResult(
        name=name,
        description="",
        grade=Grade.RECONCILIATION,
        structural=structural,
        passed=False,
        detail=detail,
    )


def test_magnitude_reads_the_check_detail():
    assert (
        magnitude_of(check("reconcile", "3 keys differ, total discrepancy 42.50; c1: ...")) == 42.5
    )
    assert (
        magnitude_of(
            check("unique_grain", "2 keys differ, total discrepancy 0.00", structural=True)
        )
        == 2
    )
    assert magnitude_of(check("conserve", "difference -17.25")) == 17.25
    assert magnitude_of(check("x", "nothing numeric")) == 0.0


def triage_input(**overrides):
    base = {
        "asset_id": "a",
        "implementation_id": "a__v1__i1",
        "failing_check": "reconcile_spend_usd",
        "check_grade": "reconciliation",
        "structural": False,
        "detail": "1 keys differ, total discrepancy 10.00",
        "magnitude": 10.0,
        "source_progress": {"purchases": AS_OF},
        "source_positions": {"purchases": AS_OF},
        "recent_deployments": (),
        "semantic_changed": False,
        "interface_columns_changed": False,
        "uses_mapping_source": True,
    }
    return TriageInput(**{**base, **overrides})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, CauseClass.IDENTITY_MAPPING_CHANGE),
        ({"structural": True, "interface_columns_changed": True}, CauseClass.SCHEMA_DRIFT),
        ({"source_positions": {"purchases": AS_OF + timedelta(hours=1)}}, CauseClass.SOURCE_LAG),
        ({"semantic_changed": True}, CauseClass.CALCULATION_DRIFT),
        (
            {"failing_check": "population_preserved", "uses_mapping_source": False},
            CauseClass.UNKNOWN,
        ),
    ],
)
def test_rule_baseline_picks_a_cause_from_evidence(overrides, expected):
    result = RuleTriageClassifier().classify(triage_input(**overrides), CauseClass)
    assert result.value is expected
    assert abs(result.probability + sum(p for _, p in result.alternatives) - 1.0) < 0.01


def test_rule_baseline_abstains_on_other_contexts():
    from veena.resolver import SlotContext

    ctx = SlotContext(slot="population.entity", text="x", option_descriptions={})
    result = RuleTriageClassifier().classify(ctx, CauseClass)
    assert result.probability == pytest.approx(1 / len(CauseClass), abs=0.001)


def test_policy_table_applies_the_threshold_and_the_unknown_rule():
    policy = RepairPolicy()
    assert policy.strategy(CauseClass.IDENTITY_MAPPING_CHANGE, 0.9) is RepairStrategy.RE_PLAN_JOIN
    assert policy.strategy(CauseClass.IDENTITY_MAPPING_CHANGE, 0.69) is RepairStrategy.ESCALATE
    assert policy.strategy(CauseClass.UNKNOWN, 1.0) is RepairStrategy.ESCALATE
    assert policy.strategy(CauseClass.SOURCE_LAG, 0.8) is RepairStrategy.WAIT_FOR_SOURCE
    assert policy.strategy(CauseClass.LATE_CORRECTIONS, 0.8) is RepairStrategy.REBUILD_WITH_LOOKBACK
    loose = RepairPolicy(
        min_probability=0.5,
        strategies={CauseClass.SCHEMA_DRIFT: RepairStrategy.RE_PLAN_JOIN},
    )
    assert loose.strategy(CauseClass.SCHEMA_DRIFT, 0.6) is RepairStrategy.RE_PLAN_JOIN
    assert loose.strategy(CauseClass.SOURCE_LAG, 0.9) is RepairStrategy.ESCALATE


def test_triage_records_model_id_and_input_hash_and_keyword_baseline_abstains():
    result = triage(triage_input(), KeywordClassifier(), RepairPolicy())
    assert result.model_id == "keyword"
    assert result.strategy is RepairStrategy.ESCALATE
    assert len(result.input_hash) == 16
    rules = triage(triage_input(), RuleTriageClassifier(), RepairPolicy())
    assert rules.cause is CauseClass.IDENTITY_MAPPING_CHANGE
    assert rules.strategy is RepairStrategy.RE_PLAN_JOIN
    assert "p=0.75" in rules.summary()


def count(conn, sql):
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_drift_kinds_change_the_sources_deterministically():
    conn = build_scenario(None, ScenarioConfig())
    try:
        before = count(conn, "SELECT count(*) FROM identity_mappings")
        first = apply_drift(conn, DriftKind.DUPLICATE_MAPPING)
        assert "maps to both" in first
        assert count(conn, "SELECT count(*) FROM identity_mappings") == before + 1
        late = apply_drift(conn, DriftKind.LATE_PURCHASES)
        assert late.startswith("20 settled purchases")
        assert count(conn, "SELECT count(*) FROM purchases WHERE purchase_id LIKE 'late%'") == 20
    finally:
        conn.close()
    other = build_scenario(None, ScenarioConfig())
    try:
        assert apply_drift(other, DriftKind.MAPPING_CHANGE) == apply_drift(
            build_scenario(None, ScenarioConfig()), DriftKind.MAPPING_CHANGE
        )
    finally:
        other.close()
