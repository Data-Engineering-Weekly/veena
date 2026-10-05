from pathlib import Path

import pytest

from tests.conftest import bundled_spec, make_spec
from veena.errors import PlanError
from veena.planner import (
    Aggregate,
    IdentityJoin,
    PopulationJoin,
    SourceScan,
    TimeAttribution,
    WindowStep,
    compile_plan,
    describe_plan,
)
from veena.planner.compile import dependencies, refresh_rule
from veena.semantic import SemanticModel
from veena.spec import TimeBucket


def test_thirty_day_plan_has_six_steps_in_order(semantic):
    plan = compile_plan(bundled_spec("customer_spend_30d"), semantic)
    kinds = [type(s) for s in plan.steps]
    assert kinds == [
        SourceScan,
        TimeAttribution,
        WindowStep,
        IdentityJoin,
        Aggregate,
        PopulationJoin,
    ]
    assert plan.output_columns == ("customer_id", "spend_usd")
    assert plan.step(WindowStep).complete_calendar_days == 30
    assert plan.step(IdentityJoin).respect_validity is True
    assert plan.step(Aggregate).measures[0].expression.startswith("CASE WHEN status = 'settled'")


def test_daily_plan_buckets_by_day_and_skips_population_join(semantic):
    plan = compile_plan(bundled_spec("customer_spend_daily"), semantic)
    assert plan.output_columns == ("customer_id", "day", "spend_usd")
    assert plan.step(WindowStep).history_days == 120
    assert all(not isinstance(s, PopulationJoin) for s in plan.steps)


def test_plan_is_deterministic(semantic):
    spec = bundled_spec("customer_spend_30d")
    assert (
        compile_plan(spec, semantic).model_dump_json()
        == compile_plan(spec, semantic).model_dump_json()
    )


def test_dependencies_and_refresh_rule(semantic):
    spec = bundled_spec("customer_spend_30d")
    plan = compile_plan(spec, semantic)
    assert [d.name for d in dependencies(plan)] == ["purchases", "identity_mappings", "customers"]
    assert {d.version for d in dependencies(plan)} == {semantic.fingerprint()}
    rule = refresh_rule(spec)
    assert rule.incorporate_within_seconds == 300
    assert rule.boundary_timezone == "UTC"
    assert rule.roll_at_day_boundary is True
    assert rule.correction_lookback_days == 30


def test_unknown_ref_is_a_plan_error(semantic):
    spec = make_spec(identity="semantic:customer_identity_current@1")
    with pytest.raises(
        PlanError, match="unknown identity policy semantic:customer_identity_current@1"
    ):
        compile_plan(spec, semantic)


def test_calculation_on_unattached_source_is_a_plan_error(semantic):
    model = semantic.model_copy(
        update={
            "calculations": (
                *semantic.calculations,
                semantic.calculations[0].model_copy(
                    update={
                        "name": "account_age",
                        "version": 1,
                        "source": "accounts",
                        "expression": "1",
                    }
                ),
            )
        }
    )
    spec = make_spec().model_copy(
        update={
            "measures": (
                make_spec()
                .measures[0]
                .model_copy(
                    update={
                        "calculation": __import__(
                            "veena.refs", fromlist=["SemanticRef"]
                        ).SemanticRef(name="account_age", version=1)
                    }
                ),
            )
        }
    )
    with pytest.raises(PlanError, match="not attached by identity policy"):
        compile_plan(spec, model)


def test_missing_time_rule_is_a_plan_error(semantic):
    model = semantic.model_copy(update={"time_rules": ()})
    with pytest.raises(PlanError, match="no time rule"):
        compile_plan(make_spec(), model)


def test_filters_and_multi_key_grains_are_refused(semantic):
    spec = make_spec().model_copy(
        update={
            "filters": (
                __import__("veena.spec", fromlist=["Filter"]).Filter.model_validate(
                    {"attribute": "semantic:customer@1", "op": "eq", "values": ("c1",)}
                ),
            )
        }
    )
    with pytest.raises(PlanError, match="filters are not supported"):
        compile_plan(spec, semantic)


def test_describe_plan_is_one_line_per_step(semantic):
    text = describe_plan(compile_plan(bundled_spec("customer_spend_30d"), semantic))
    assert text.count("\n") == 7
    assert "at fact time" in text


def test_planner_never_imports_a_model():
    root = Path(__file__).resolve().parents[2] / "src" / "veena" / "planner"
    for path in root.glob("*.py"):
        imports = [
            line for line in path.read_text().splitlines() if line.startswith(("from ", "import "))
        ]
        assert not any("veena.models" in line for line in imports), path


def test_plan_has_no_backend_specific_fields(semantic):
    plan = compile_plan(bundled_spec("customer_spend_30d"), semantic)
    dumped = plan.model_dump_json().lower()
    for word in ("duckdb", "snowflake", "bigquery", "timestamptz", "create table"):
        assert word not in dumped


def test_model_copy_keeps_semantic_model_valid(semantic):
    assert isinstance(semantic, SemanticModel)
    assert TimeBucket.DAY in TimeBucket
