from datetime import UTC, datetime
from pathlib import Path

import pytest

from veena.errors import PolicyError
from veena.importer import (
    confirm_drafts,
    duplicates_report,
    import_history,
    import_project,
    infer_drafts,
    load_history,
    load_manifest,
    parse_manifest,
    propose_consolidations,
    semantic_from_project,
)
from veena.importer.history import ReaderClass, ReaderContext, RulesReaderClassifier
from veena.models import KeywordClassifier, ScriptedDrafter
from veena.policy import ReorganizationPolicy
from veena.registry import EventKind, MemoryEventStore, Registry
from veena.registry.state import AssetStatus, ProposalStatus
from veena.scenario.dbt_fixture import DbtFixtureConfig, build_manifest, build_query_history
from veena.semantic.model import SemanticModel

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "src" / "veena" / "scenario" / "dbt"


@pytest.fixture(scope="module")
def project():
    return parse_manifest(build_manifest(DbtFixtureConfig(history_days=5)))


@pytest.fixture(scope="module")
def history():
    return build_query_history(DbtFixtureConfig(history_days=5))


@pytest.fixture
def registry():
    return Registry(MemoryEventStore())


def test_bundled_manifest_file_parses():
    project = load_manifest(FIXTURE_DIR / "manifest.json")
    assert project.project_name == "shoply" and project.adapter_type == "snowflake"
    assert len(project.models) == 41 and len(project.sources) == 15
    assert (
        len(project.exposures) == 5
        and len(project.semantic_models) == 4
        and len(project.metrics) == 6
    )
    spend = project.models["customer_spend_30d"]
    assert spend.relation == "analytics.marts.customer_spend_30d"
    assert spend.depends_on_models == ("stg_customers", "int_customer_daily_spend")
    assert spend.owner == "sales-engineering" and spend.meta["sla_minutes"] == 5
    assert "dateadd(day, -30" in spend.compiled_code
    rows = load_history(FIXTURE_DIR / "query_history.jsonl")
    assert len(rows) > 1000


def test_import_is_idempotent_and_records_exposures(registry, project):
    first = import_project(registry, project)
    assert first.assets_added == 41 and first.assets_updated == 0
    assert first.commitments_added == 14  # every exposure edge whose model exists
    second = import_project(registry, project)
    assert second.assets_added == 0 and second.assets_unchanged == 41
    assert second.commitments_added == 0 and second.commitments_unchanged == 14
    asset = registry.state.assets["customer_spend_30d"]
    assert asset.status is AssetStatus.OBSERVED
    assert asset.observed is not None and asset.observed.layer == "marts"
    assert asset.depends_on == ("stg_customers", "int_customer_daily_spend")
    consumers = {registry.state.commitments[c].consumer for c in asset.commitments}
    assert consumers == {"exposure:sales_app"}
    assert (
        registry.state.commitments[
            "customer_spend_30d:exposure:sales_app"
        ].incorporate_within_seconds
        == 300
    )
    assert not any(a.spec for a in registry.state.assets.values())


def test_changed_checksum_updates_the_asset(registry, project):
    import_project(registry, project)
    changed = project.model_copy(
        update={
            "models": {
                **project.models,
                "customer_spend_30d": project.models["customer_spend_30d"].model_copy(
                    update={"checksum": "new"}
                ),
            }
        }
    )
    report = import_project(registry, changed)
    assert report.assets_updated == 1 and report.assets_unchanged == 40


def test_metricflow_becomes_a_semantic_model_with_direct_identity(project):
    imported = semantic_from_project(project)
    model = imported.model
    assert isinstance(model, SemanticModel)
    assert {e.name for e in model.entities} == {"customer", "order", "payment", "refund"}
    assert {c.name for c in model.calculations} == {
        "net_order_usd",
        "order_count",
        "gross_payment_usd",
        "refund_usd",
    }
    count = next(c for c in model.calculations if c.name == "order_count")
    assert count.expression == "CASE WHEN order_id IS NOT NULL THEN 1 ELSE 0 END"
    assert {r.source for r in model.time_rules} == {"fct_orders", "fct_payments", "fct_refunds"}
    direct = next(p for p in model.identity_policies if p.name == "fct_orders_customer_direct")
    assert (
        direct.kind == "direct"
        and direct.fact_key == "customer_id"
        and direct.fact_timestamp == "ordered_at"
    )
    texts = [g.text for g in imported.gaps]
    assert any("average_order_value is a ratio" in t for t in texts)
    assert any("refund_rate is a ratio" in t for t in texts)
    assert all(g.asset_id for g in imported.gaps)


def test_history_records_usage_cost_unmanaged_and_proposals(registry, project, history):
    import_project(registry, project)
    report = import_history(registry, history)
    assert report.queries == len(history)
    assert report.matched > len(history) * 0.95
    state = registry.state
    sales = state.usage[("customer_spend_30d", "user:svc_sales_app")]
    assert sales.reader_class == "managed" and sales.runs_per_day > 100
    # The exposure is declared by dbt as exposure:sales_app; the warehouse shows user:svc_sales_app.
    # Nothing links the two yet, so the engine proposes a commitment for the observed reader.
    proposal = state.commitment_proposals[("customer_spend_30d", "user:svc_sales_app")]
    assert proposal.reader_class == "managed" and "holds no commitment" in proposal.rationale
    report_reader = state.usage[("daily_revenue", "user:svc_reporting")]
    assert report_reader.reader_class == "scheduled" and report_reader.distinct_hours == 1
    bi = state.commitment_proposals[("cohort_retention", "user:svc_bi")]
    assert bi.reader_class in {"scheduled", "managed"}
    unmanaged = state.unmanaged_reads[("int_customer_daily_spend", "user:rchen")]
    assert "intermediate" in unmanaged.reason
    analyst = next(u for (a, c), u in state.usage.items() if c == "user:jdoe")
    assert analyst.reader_class == "ad_hoc"
    cost = state.refresh_costs["customer_spend_30d"]
    assert cost.runs == 5 and cost.credits > 0
    assert "stg_customers" not in state.refresh_costs  # views are not rebuilt


def test_rules_reader_classifier_abstains_on_other_contexts():
    from pydantic import BaseModel

    class Other(BaseModel):
        text: str

    verdict = RulesReaderClassifier().classify(Other(text="x"), ReaderClass)
    assert verdict.probability == 0.25
    ctx = ReaderContext(
        consumer="user:x",
        role="APP_READER",
        warehouse="W",
        asset_id="a",
        layer="marts",
        internal=False,
        queries=500,
        runs_per_day=100.0,
        distinct_hours=24,
        median_interval_minutes=5.0,
        sample_query="select 1",
    )
    assert RulesReaderClassifier().classify(ctx, ReaderClass).value is ReaderClass.MANAGED


def test_infer_opens_a_draft_question_per_observed_asset(registry, project):
    import_project(registry, project)
    semantic = semantic_from_project(project).model
    drafts = infer_drafts(
        registry,
        semantic,
        KeywordClassifier(),
        only=["customer_spend_30d", "stg_orders", "int_customer_daily_spend"],
    )
    by_id = {d.asset_id: d for d in drafts}
    spend = by_id["customer_spend_30d"]
    assert spend.slots["window.complete_calendar_days"] == "30"
    assert spend.slots["population.include_inactive"] == "true"
    assert spend.slots["grain.time_bucket"] == "none"
    assert spend.slots["identity_policy"] == "fct_orders_customer_direct"
    assert spend.slots["population.entity"] == "customer"
    assert "shape" not in spend.slots
    staging = by_id["stg_orders"]
    assert staging.slots["shape"] == "open" and staging.min_probability == 0.0
    daily = by_id["int_customer_daily_spend"]
    assert (
        daily.slots["grain.time_bucket"] == "day" and daily.slots["window.history_days"] == "open"
    )
    question = registry.state.questions[spend.question_id]
    assert question.slot == "inferred_spec" and question.draft == spend.slots
    assert question.alternatives == ("confirm_draft", "edit_draft", "reject_draft")
    assert registry.state.assets["customer_spend_30d"].spec is None
    kinds = [s.event.kind for s in registry.events()]
    assert kinds.count(EventKind.CLASSIFICATION_PRODUCED) == 6
    again = infer_drafts(registry, semantic, KeywordClassifier(), only=["customer_spend_30d"])
    assert again == []  # a pending question is not reopened


def test_drafter_writes_the_rationale(registry, project):
    import_project(registry, project)
    semantic = semantic_from_project(project).model
    drafter = ScriptedDrafter(lambda req: f"rationale for {getattr(req, 'asset_id', '?')}")
    drafts = infer_drafts(
        registry, semantic, KeywordClassifier(), drafter=drafter, only=["customer_spend_90d"]
    )
    q = registry.state.questions[drafts[0].question_id]
    assert q.question == "rationale for customer_spend_90d"
    assert q.draft["window.complete_calendar_days"] == "90"
    assert len(registry.state.drafts) == 1


def test_confirm_above_threshold_resolves_specs_and_report_finds_the_three_duplicates(
    registry, project, history
):
    import_project(registry, project)
    import_history(registry, history)
    semantic = semantic_from_project(project).model
    marts = [m.name for m in project.models.values() if m.schema_name == "marts"]
    drafts = infer_drafts(registry, semantic, KeywordClassifier(), only=marts)
    spend = next(d for d in drafts if d.asset_id == "customer_spend_30d")
    # The keyword baseline splits USD measures; its weakest slot sits near 0.5. A real classifier
    # must beat this, which is what the calibration report measures.
    assert 0.5 <= spend.min_probability < 0.8, spend.probabilities
    confirmed = confirm_drafts(
        registry, semantic, above=0.5, actor="user:owner", authority="role:data_owner"
    )
    assert {
        "customer_spend_30d",
        "cust_spend_last_30d",
        "customer_spend_30d_v2",
        "customer_spend_90d",
    } <= set(confirmed)
    assert registry.state.assets["customer_spend_30d"].status is AssetStatus.SPECIFIED
    spec = registry.state.assets["customer_spend_30d"].spec
    assert spec is not None and spec.window.complete_calendar_days == 30
    assert str(spec.provenance["population.entity"]).startswith("decision:")
    assert (
        "household_spend_90d" not in confirmed
    )  # its output has no customer_id: grain is a postal code
    household_q = next(
        q for q in registry.state.questions.values() if q.asset_id == "household_spend_90d"
    )
    assert household_q.draft["population.entity"] == "open"
    assert "daily_revenue" not in confirmed

    report = duplicates_report(registry)
    sets = [sorted(a.asset_id for a in g.assets) for g in report.groups]
    assert ["cust_spend_last_30d", "customer_spend_30d", "customer_spend_30d_v2"] in sets
    group = next(g for g in report.groups if len(g.assets) == 3)
    sales = next(a for a in group.assets if a.asset_id == "customer_spend_30d")
    assert sales.consumers == ("exposure:sales_app",) and sales.refresh_runs == 5
    assert group.total_credits > 0
    assert any("decided by specification equality" in e for e in group.evidence)
    text = report.render()
    assert "equivalence set 1" in text

    opened = propose_consolidations(
        registry, report, as_of=datetime.now(UTC), policy=ReorganizationPolicy(autonomy="shadow")
    )
    assert len(opened) == 1
    proposal = registry.state.proposals[opened[0]]
    assert proposal.kind_of_change == "consolidate" and proposal.status is ProposalStatus.OPEN
    assert set(proposal.affected_assets) == {
        "cust_spend_last_30d",
        "customer_spend_30d",
        "customer_spend_30d_v2",
    }
    assert (
        propose_consolidations(registry, report, as_of=datetime.now(UTC)) == []
    )  # no duplicate proposal


def test_manual_confirm_answer_is_honored(registry, project):
    import_project(registry, project)
    semantic = semantic_from_project(project).model
    drafts = infer_drafts(registry, semantic, KeywordClassifier(), only=["customer_spend_90d"])
    from veena.registry import DecisionRecorded, Event

    registry.append(
        Event(
            actor="user:ananth",
            authority="role:data_owner",
            payload=DecisionRecorded(
                decision_id="d1",
                question_id=drafts[0].question_id,
                selected="confirm_draft",
                rationale="looks right",
                scope="asset:customer_spend_90d",
            ),
        )
    )
    confirmed = confirm_drafts(
        registry, semantic, above=1.1, actor="engine", authority="policy:resolution@1"
    )
    assert confirmed == ["customer_spend_90d"]


def test_shadow_autonomy_refuses_to_activate(scenario_conn, semantic):
    from tests.conftest import AS_OF, bundled_spec
    from veena.backends import DuckDBBackend
    from veena.coordinator import Coordinator
    from veena.registry import AssetRequested, Event, SpecResolved

    registry = Registry(MemoryEventStore())
    spec = bundled_spec("customer_spend_30d")
    registry.append(
        Event(
            actor="app:sales",
            authority="request:app:sales",
            payload=AssetRequested(asset_id=spec.asset_id, consumer="app:sales", request_text="x"),
        )
    )
    registry.append(
        Event(actor="engine", authority="request:app:sales", payload=SpecResolved(spec=spec))
    )
    coordinator = Coordinator(
        registry,
        DuckDBBackend(scenario_conn),
        semantic,
        policy=ReorganizationPolicy(autonomy="shadow"),
    )
    with pytest.raises(PolicyError, match="shadow"):
        coordinator.activate("customer_spend_30d", as_of=AS_OF)
    assert len(registry.state.proposals) == 1
    assert len(registry.state.deployments) == 0
    assert registry.state.assets["customer_spend_30d"].status is AssetStatus.SPECIFIED
