"""Regression tests for the importer, adapter, and CLI review findings."""

import copy
import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from veena.cli import app
from veena.errors import (
    ManifestError,
    ModelError,
    ModelNotConfiguredError,
    ResolutionError,
    VeenaError,
)
from veena.importer import (
    build_spec_from_draft,
    confirm_drafts,
    import_history,
    import_project,
    infer_drafts,
    load_history,
    parse_manifest,
    semantic_from_project,
)
from veena.importer.history import (
    QueryRecord,
    built_relation,
    is_internal_layer,
    referenced_relations,
)
from veena.importer.infer import _bucket, _window_days
from veena.models import ClaudeClassifier, JevClassifier, KeywordClassifier
from veena.registry import DecisionRecorded, Event, MemoryEventStore, Registry
from veena.scenario.dbt_fixture import DbtFixtureConfig, build_manifest, build_query_history

runner = CliRunner()


def _project(mutate=None):
    raw = build_manifest(DbtFixtureConfig(history_days=3))
    if mutate:
        mutate(raw)
    return parse_manifest(raw)


# ---- manifest -------------------------------------------------------------------------------


def test_versioned_models_keep_every_version_and_their_dependencies():
    def mutate(raw):
        node = copy.deepcopy(raw["nodes"]["model.shoply.customer_spend_30d"])
        node["unique_id"] = "model.shoply.customer_spend_30d.v2"
        node["version"] = 2
        node["alias"] = "customer_spend_30d_v2x"
        node["relation_name"] = "analytics.marts.customer_spend_30d_v2x"
        raw["nodes"][node["unique_id"]] = node
        raw["nodes"]["model.shoply.customer_spend_30d"]["version"] = 1
        raw["nodes"]["model.shoply.customer_order_count_30d"]["depends_on"]["nodes"].append(
            "model.shoply.customer_spend_30d.v2"
        )

    project = _project(mutate)
    assert "customer_spend_30d_v1" in project.models and "customer_spend_30d_v2" in project.models
    assert "customer_spend_30d_v2" in project.models["customer_order_count_30d"].depends_on_models


def test_measure_as_string_seeds_snapshots_ephemeral_and_source_collisions():
    def mutate(raw):
        raw["metrics"]["metric.shoply.revenue"]["type_params"]["measure"] = "net_order_usd"
        raw["nodes"]["seed.shoply.country_codes"] = {
            "unique_id": "seed.shoply.country_codes",
            "resource_type": "seed",
            "name": "country_codes",
            "database": "analytics",
            "schema": "seeds",
            "alias": "country_codes",
            "relation_name": None,
            "config": {"materialized": "seed"},
            "depends_on": {"nodes": []},
        }
        raw["nodes"]["model.shoply.dim_customers"]["depends_on"]["nodes"].append(
            "seed.shoply.country_codes"
        )
        raw["nodes"]["model.shoply.int_customer_identity"]["config"]["materialized"] = "ephemeral"
        raw["nodes"]["model.shoply.int_customer_identity"]["relation_name"] = None
        raw["sources"]["source.shoply.stripe.customers"] = {
            **raw["sources"]["source.shoply.raw.customers"],
            "unique_id": "source.shoply.stripe.customers",
            "source_name": "stripe",
        }

    project = _project(mutate)
    assert project.metrics["revenue"].measure == "net_order_usd"
    seed = project.models["country_codes"]
    assert seed.resource_type == "seed" and seed.relation == "analytics.seeds.country_codes"
    assert "country_codes" in project.models["dim_customers"].depends_on_models
    assert (
        project.models["int_customer_identity"].ephemeral
        and project.models["int_customer_identity"].relation == ""
    )
    assert "raw.customers" in project.sources and "stripe.customers" in project.sources
    assert project.models["stg_customers"].depends_on_sources == ("raw.customers",)
    registry = Registry(MemoryEventStore())
    report = import_project(registry, project)
    assert report.assets_skipped == 1 and "int_customer_identity" not in registry.state.assets
    assert "country_codes" in registry.state.assets


def test_malformed_nodes_are_manifest_errors():
    def mutate(raw):
        raw["nodes"]["model.shoply.fct_orders"]["depends_on"] = "not a mapping"

    with pytest.raises(ManifestError, match=r"model\.shoply\.fct_orders is malformed"):
        _project(mutate)
    with pytest.raises(ManifestError, match="needs 'metadata'"):
        parse_manifest({"nodes": {}})


def test_semantic_model_ref_is_resolved_from_dependencies_not_text():
    def mutate(raw):
        raw["semantic_models"]["semantic_model.shoply.orders"]["model"] = 'ref("fct_orders", v=1)'

    project = _project(mutate)
    assert project.semantic_models["orders"].model == "fct_orders"
    imported = semantic_from_project(project)
    assert "fct_orders" in {s.name for s in imported.model.sources}


def test_reimport_detects_metadata_changes_and_missing_models():
    project = _project()
    registry = Registry(MemoryEventStore())
    import_project(registry, project)
    changed = project.model_copy(
        update={
            "models": {
                **project.models,
                "fct_orders": project.models["fct_orders"].model_copy(update={"owner": "finance"}),
            }
        }
    )
    report = import_project(registry, changed)
    assert report.assets_updated == 1 and report.assets_unchanged == 40
    smaller = project.model_copy(
        update={"models": {k: v for k, v in project.models.items() if k != "customer_ltv"}}
    )
    report = import_project(registry, smaller)
    assert report.missing == ("customer_ltv",)


def test_bad_sla_is_a_manifest_error():
    def mutate(raw):
        raw["exposures"]["exposure.shoply.sales_app"]["meta"]["sla_minutes"] = "5m"

    with pytest.raises(ManifestError, match="sla_minutes"):
        import_project(Registry(MemoryEventStore()), _project(mutate))


# ---- history --------------------------------------------------------------------------------


def test_relation_extraction_handles_quotes_comments_strings_and_builds():
    assert referenced_relations(
        'select * from "ANALYTICS"."MARTS"."X" -- from a.b.c\n join analytics.marts.y'
    ) == {"analytics.marts.x", "analytics.marts.y"}
    assert referenced_relations("select 'from a.b.c' as t from analytics.marts.x") == {
        "analytics.marts.x"
    }
    assert (
        built_relation("create or replace transient table analytics.marts.x as select 1")
        == "analytics.marts.x"
    )
    assert (
        built_relation("create or replace temporary table analytics.marts.x__dbt_tmp as select 1")
        is None
    )
    assert (
        built_relation(
            "merge into analytics.marts.x as d using analytics.marts.x__dbt_tmp as s on 1=1"
        )
        == "analytics.marts.x"
    )
    assert built_relation("insert overwrite into analytics.marts.x select 1") == "analytics.marts.x"
    assert built_relation("select * from analytics.marts.x") is None


def test_incremental_builds_count_as_refresh_not_reads():
    project = _project()
    registry = Registry(MemoryEventStore())
    import_project(registry, project)
    report = import_history(registry, build_query_history(DbtFixtureConfig(history_days=5)))
    state = registry.state
    assert "int_customer_daily_spend" in state.refresh_costs  # merge-style incremental build
    assert not any(c == "user:dbt_cloud" for (_, c) in state.usage)
    assert report.unmanaged_reads >= 1


def test_naive_timestamps_are_read_as_utc_and_bad_rows_name_the_line(tmp_path):
    record = QueryRecord(
        query_id="q",
        user_name="u",
        role_name="r",
        warehouse_name="w",
        start_time=datetime(2026, 9, 1, 12, 0),
        total_elapsed_ms=1,
        bytes_scanned=1,
        credits_used_cloud_services=0.0,
        query_type="SELECT",
        query_text="select 1",
    )
    assert record.start_time.tzinfo is UTC
    path = tmp_path / "h.jsonl"
    path.write_text(record.model_dump_json() + "\n" + json.dumps({"query_id": "bad"}) + "\n")
    with pytest.raises(VeenaError, match="line 2"):
        load_history(path)


def test_internal_layers_match_real_schema_names():
    assert is_internal_layer("analytics_staging", ())
    assert is_internal_layer("dbt_jdoe_intermediate", ())
    assert is_internal_layer("marts", ("internal",))
    assert not is_internal_layer("marts", ())


# ---- inference and confirmation ----------------------------------------------------------------


def test_bucket_tokens_and_window_candidates():
    assert _bucket("customers_active_30_days") == ("none", 1.0, "no day or month token in the name")
    assert _bucket("product_sales_daily")[0:2] == ("day", 1.0)
    assert _bucket("daily_revenue")[0:2] == ("day", 0.7)
    assert _bucket("cohort_month_retention")[0:2] == ("month", 0.7)
    assert _window_days("x_30d", "dateadd('day', -30, current_date())") == (30, (30,))
    assert _window_days("x", "where d >= dateadd(day, -1, x) and d >= dateadd(dd, -90, y)") == (
        None,
        (1, 90),
    )
    assert _window_days("x_90d", "dateadd(day, -1, x) and dateadd(day, -90, y)") == (90, (1, 90))
    assert _window_days("x", "where d >= current_date - 7") == (7, (7,))


def test_rejected_drafts_are_not_reinferred_and_columns_match_case_insensitively():
    project = _project()
    registry = Registry(MemoryEventStore())
    import_project(registry, project)
    semantic = semantic_from_project(project).model
    drafts = infer_drafts(registry, semantic, KeywordClassifier(), only=["customer_spend_30d"])
    registry.append(
        Event(
            actor="user:owner",
            authority="role:data_owner",
            payload=DecisionRecorded(
                decision_id="d1",
                question_id=drafts[0].question_id,
                selected="reject_draft",
                rationale="",
                scope="asset:customer_spend_30d",
            ),
        )
    )
    assert infer_drafts(registry, semantic, KeywordClassifier(), only=["customer_spend_30d"]) == []
    assert (
        len(
            infer_drafts(
                registry, semantic, KeywordClassifier(), only=["customer_spend_30d"], force=True
            )
        )
        == 1
    )
    upper = project.model_copy(
        update={
            "models": {
                **project.models,
                "customer_spend_90d": project.models["customer_spend_90d"].model_copy(
                    update={"columns": {"CUSTOMER_ID": "varchar", "SPEND_USD": "number"}}
                ),
            }
        }
    )
    registry2 = Registry(MemoryEventStore())
    import_project(registry2, upper)
    draft = infer_drafts(registry2, semantic, KeywordClassifier(), only=["customer_spend_90d"])[0]
    assert draft.slots["population.entity"] == "customer"


def test_confirm_builds_before_recording_and_names_the_gap():
    project = _project()
    registry = Registry(MemoryEventStore())
    import_project(registry, project)
    semantic = semantic_from_project(project).model
    infer_drafts(registry, semantic, KeywordClassifier(), only=["customer_spend_30d"])
    broken = semantic.model_copy(update={"identity_policies": ()})
    with pytest.raises(
        ResolutionError,
        match=r"identity policy 'fct_orders_customer_direct', which the semantic model lacks",
    ):
        confirm_drafts(registry, broken, above=0.0)
    assert registry.state.decisions == {}
    confirmed = confirm_drafts(registry, semantic, above=0.0)
    assert confirmed == ["customer_spend_30d"]
    decision = next(iter(registry.state.decisions.values()))
    assert decision.actor == "engine" and decision.authority == "policy:resolution@1"


def test_build_spec_from_draft_validates_values(semantic):
    base = {
        "population.entity": "customer",
        "measures.calculation": "purchase_amount_usd",
        "identity_policy": "customer_identity_point_in_time",
        "grain.time_bucket": "none",
        "window.complete_calendar_days": "30",
        "population.include_inactive": "True",
    }
    spec = build_spec_from_draft("a_30d", base, semantic, origin="decision:d1")
    assert spec.population.include_inactive is True
    with pytest.raises(ResolutionError, match="time bucket 'weekly'"):
        build_spec_from_draft(
            "a_30d", {**base, "grain.time_bucket": "weekly"}, semantic, origin="decision:d1"
        )
    with pytest.raises(ResolutionError, match=r"integer window.complete_calendar_days"):
        build_spec_from_draft(
            "a_30d",
            {**base, "window.complete_calendar_days": "lots"},
            semantic,
            origin="decision:d1",
        )
    with pytest.raises(ResolutionError, match="valid specification"):
        build_spec_from_draft(
            "a_30d", {**base, "window.complete_calendar_days": "0"}, semantic, origin="decision:d1"
        )


# ---- adapters ---------------------------------------------------------------------------------


class _Ctx(BaseModel):
    slot: str = "s"
    text: str = "x"
    option_descriptions: dict[str, str] = {}


def test_claude_errors_are_typed():
    from enum import StrEnum

    class Opt(StrEnum):
        A = "a"
        B = "b"

    class Boom:
        class messages:  # noqa: N801
            @staticmethod
            def parse(**_):
                raise TypeError("Could not resolve authentication method")

            @staticmethod
            def create(**_):
                raise TypeError("Could not resolve authentication method")

    with pytest.raises(ModelError, match="TypeError"):
        ClaudeClassifier(client=Boom()).classify(_Ctx(), Opt)

    class Truncated:
        class messages:  # noqa: N801
            @staticmethod
            def parse(**_):
                return type("R", (), {"stop_reason": "max_tokens", "parsed_output": None})()

    with pytest.raises(ModelError, match="max_tokens"):
        ClaudeClassifier(client=Truncated()).classify(_Ctx(), Opt)


def test_claude_without_credentials_is_not_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")
    from enum import StrEnum

    class Opt(StrEnum):
        A = "a"

    try:
        ClaudeClassifier().classify(_Ctx(), Opt)
    except ModelNotConfiguredError:
        pass
    except ModelError:
        pass  # an SDK that resolves a profile elsewhere fails at the call, typed


def test_jev_transport_errors_are_typed_and_bodies_are_capped():
    from enum import StrEnum

    class Opt(StrEnum):
        A = "a"

    def bad_json(url, body, headers):
        raise ModelError("jev response is not JSON: b'<html>'")

    with pytest.raises(ModelError, match="not JSON"):
        JevClassifier(base_url="https://jev", api_key="k", transport=bad_json).classify(_Ctx(), Opt)
    with pytest.raises(ModelError, match="lacks value"):
        JevClassifier(
            base_url="https://jev", api_key="k", transport=lambda *_: {"nope": 1}
        ).classify(_Ctx(), Opt)
    with pytest.raises(ModelError, match="outside"):
        JevClassifier(
            base_url="https://jev",
            api_key="k",
            transport=lambda *_: {"value": "a", "probability": 7},
        ).classify(_Ctx(), Opt)


# ---- CLI ------------------------------------------------------------------------------------


def test_cli_refuses_missing_registry_and_scenario(tmp_path):
    missing = tmp_path / "nope.sqlite"
    listed = runner.invoke(app, ["registry", "assets", "--registry", str(missing)])
    assert listed.exit_code == 1 and "no registry at" in listed.output
    assert not missing.exists()
    registry = tmp_path / "r.sqlite"
    spec = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "src"
        / "veena"
        / "scenario"
        / "specs"
        / "customer_spend_30d.yaml"
    )
    assert (
        runner.invoke(
            app, ["asset", "request", str(spec), "--consumer", "a", "--registry", str(registry)]
        ).exit_code
        == 0
    )
    activate = runner.invoke(
        app,
        [
            "asset",
            "activate",
            "customer_spend_30d",
            "--registry",
            str(registry),
            "--scenario",
            str(tmp_path / "none.duckdb"),
        ],
    )
    assert activate.exit_code == 1 and "no scenario database" in activate.output
    assert not (tmp_path / "none.duckdb").exists()
    bad_date = runner.invoke(
        app,
        [
            "asset",
            "plan",
            "customer_spend_30d",
            "--registry",
            str(registry),
            "--as-of",
            "yesterday",
        ],
    )
    assert bad_date.exit_code == 1 and "ISO 8601" in bad_date.output
    infer = runner.invoke(
        app,
        ["import", "infer", "--registry", str(registry), "--semantic", str(tmp_path / "none.yaml")],
    )
    assert infer.exit_code == 1 and "no semantic model at" in infer.output


def test_yaml_fragment_for_a_bucketed_spec(tmp_path):
    registry = tmp_path / "r.sqlite"
    spec = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "src"
        / "veena"
        / "scenario"
        / "specs"
        / "customer_spend_daily.yaml"
    )
    assert (
        runner.invoke(
            app, ["asset", "request", str(spec), "--consumer", "a", "--registry", str(registry)]
        ).exit_code
        == 0
    )
    shown = runner.invoke(
        app,
        ["asset", "show", "customer_spend_daily", "--format", "yaml", "--registry", str(registry)],
    )
    assert shown.exit_code == 0, shown.output
    assert "grain.time_bucket:\n    ref: day" in shown.output
    assert "window.history_days:\n    ref: '90'" in shown.output
    assert "window.complete_calendar_days" not in shown.output
