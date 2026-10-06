"""Brakes, replay, live checks, and repair on the scenario (phase 6, R6.5 to R6.11, R6.18)."""

from datetime import timedelta
from typing import Any

import pytest

from tests.conftest import AS_OF, bundled_spec
from tests.test_coordinator_share import request
from veena.backends import DuckDBBackend
from veena.coordinator import Coordinator
from veena.errors import CoordinationError, PolicyError
from veena.models import ScriptedClassifier
from veena.policy import CauseClass, RepairStrategy
from veena.registry import EventKind, MemoryEventStore, Registry
from veena.registry.state import ProposalStatus
from veena.scenario import DriftKind, apply_drift

OPERATOR: dict[str, Any] = {"actor": "user:oncall", "authority": "role:operator"}


def view_target(conn, view):
    row = conn.execute(f"SELECT sql FROM duckdb_views() WHERE view_name = '{view}'").fetchone()
    return row[0] if row else None


@pytest.fixture
def world(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    request(registry, bundled_spec("customer_spend_90d"), "agent:support")
    return registry, backend, semantic, scenario_conn


@pytest.fixture
def shared(world):
    registry, backend, semantic, conn = world
    outcome = Coordinator(registry, backend, semantic).activate("customer_spend_90d", as_of=AS_OF)
    assert outcome.activated and outcome.kind_of_change == "share"
    return registry, backend, semantic, conn


# ---- R6.5 freeze ----


def test_frozen_daily_asset_makes_a_new_request_build_directly(world):
    registry, backend, semantic, _ = world
    coordinator = Coordinator(registry, backend, semantic)
    # Freeze the asset the share would create, before it exists as a shared intermediate: the
    # freeze names the id the derivation would use, which is the 30-day asset's provider-to-be.
    coordinator.freeze("customer_spend_30d", reason="quarter close", **OPERATOR)
    outcome = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert outcome.activated and outcome.kind_of_change == "direct"
    rejected = [p for p in registry.state.proposals.values() if p.status is ProposalStatus.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].kind_of_change == "share"
    assert "frozen by user:oncall (role:operator)" in rejected[0].reason
    assert "quarter close" in rejected[0].reason
    assert registry.state.assets["customer_spend_30d"].active_implementation == (
        "customer_spend_30d__v1__i1"
    )


def test_frozen_asset_itself_is_recorded_and_refused(world):
    registry, backend, semantic, _ = world
    coordinator = Coordinator(registry, backend, semantic)
    coordinator.freeze("customer_spend_90d", reason="hold", **OPERATOR)
    with pytest.raises(PolicyError, match="frozen"):
        coordinator.activate("customer_spend_90d", as_of=AS_OF)
    proposals = [p for p in registry.state.proposals.values() if "90d" in p.proposal_id]
    assert proposals and proposals[-1].status is ProposalStatus.REJECTED
    with pytest.raises(CoordinationError, match="already frozen"):
        coordinator.freeze("customer_spend_90d", reason="again", **OPERATOR)
    coordinator.unfreeze("customer_spend_90d", reason="done", **OPERATOR)
    assert coordinator.activate("customer_spend_90d", as_of=AS_OF).activated


def test_subgraph_freeze_reaches_dependents(shared):
    registry, backend, semantic, _ = shared
    coordinator = Coordinator(registry, backend, semantic)
    coordinator.freeze("customer_spend_daily", reason="hold", include_dependents=True, **OPERATOR)
    assert registry.state.frozen_by("customer_spend_30d") is not None
    assert registry.state.frozen_by("customer_spend_90d") is not None
    coordinator.unfreeze("customer_spend_daily", reason="done", **OPERATOR)
    assert registry.state.frozen_by("customer_spend_30d") is None


# ---- R6.6 pin ----


def test_pin_holds_the_interface_through_a_deployment_until_released(world):
    registry, backend, semantic, conn = world
    coordinator = Coordinator(registry, backend, semantic)
    coordinator.pin(
        "customer_spend_30d", "customer_spend_30d__v1__i1", reason="known good", **OPERATOR
    )
    outcome = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert outcome.activated and outcome.kind_of_change == "share"
    state = registry.state
    assert state.assets["customer_spend_30d"].active_implementation == "customer_spend_30d__v1__i2"
    assert state.interface_of("customer_spend_30d") == "customer_spend_30d__v1__i1"
    assert "customer_spend_30d__v1__i1" in view_target(conn, "customer_spend_30d")
    assert "customer_spend_90d__v1__i1" in view_target(conn, "customer_spend_90d")
    coordinator.unpin("customer_spend_30d", reason="repair verified", **OPERATOR)
    assert "customer_spend_30d__v1__i2" in view_target(conn, "customer_spend_30d")
    assert registry.state.interface_of("customer_spend_30d") == "customer_spend_30d__v1__i2"
    kinds = [s.event.kind for s in registry.events()]
    assert EventKind.PIN_APPLIED in kinds and EventKind.PIN_RELEASED in kinds
    with pytest.raises(CoordinationError, match="not an implementation"):
        coordinator.pin("customer_spend_30d", "nope", reason="x", **OPERATOR)


def test_rollback_leaves_a_pinned_interface_alone(shared):
    registry, backend, semantic, conn = shared
    coordinator = Coordinator(registry, backend, semantic)
    coordinator.pin(
        "customer_spend_30d", "customer_spend_30d__v1__i1", reason="known good", **OPERATOR
    )
    deployment = registry.state.latest_deployment_for("customer_spend_90d")
    assert deployment is not None
    coordinator.rollback(deployment.deployment_id, reason="bad totals", as_of=AS_OF)
    assert "customer_spend_30d__v1__i1" in view_target(conn, "customer_spend_30d")
    assert view_target(conn, "customer_spend_90d") is None


# ---- R6.7 replay ----


def test_replay_verifies_at_a_past_position_without_activating(shared):
    registry, backend, semantic, conn = shared
    coordinator = Coordinator(registry, backend, semantic)
    proposal_id = registry.state.deployments["customer_spend_daily_d1"].proposal_id
    before = len(registry.state.deployments)
    outcomes = coordinator.replay(proposal_id, at=AS_OF - timedelta(days=20))
    assert len(outcomes) == 3 and all(o.passed for o in outcomes)
    replays = [v for v in registry.state.verifications.values() if v.replay]
    assert len(replays) == 3
    assert all(v.as_of == AS_OF - timedelta(days=20) for v in replays)
    assert len(registry.state.deployments) == before
    tables = {r[0] for r in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert not any(t.endswith("__replay") for t in tables)


# ---- R6.9, R6.9a, R6.10, R6.11 live checks and repair ----


def scripted(cause, probability):
    return ScriptedClassifier({"TriageInput": (cause, probability)})


def test_mapping_change_fails_the_live_check_then_repairs_under_policy(world):
    registry, backend, semantic, conn = world
    note = apply_drift(conn, DriftKind.MAPPING_CHANGE)
    assert "moved from" in note
    coordinator = Coordinator(
        registry, backend, semantic, classifier=scripted(CauseClass.IDENTITY_MAPPING_CHANGE, 0.9)
    )
    outcome = coordinator.check_live("customer_spend_30d", as_of=AS_OF)
    assert not outcome.passed
    assert outcome.triage is not None
    assert outcome.triage.cause is CauseClass.IDENTITY_MAPPING_CHANGE
    assert outcome.triage.strategy is RepairStrategy.RE_PLAN_JOIN
    assert outcome.activation is not None and outcome.activation.activated
    state = registry.state
    proposal = state.proposals[outcome.proposal_id]
    assert proposal.kind_of_change == "repair"
    assert "identity_mapping_change p=0.90" in proposal.summary
    assert state.assets["customer_spend_30d"].active_implementation == "customer_spend_30d__v1__i2"
    live = state.latest_live_verification("customer_spend_30d")
    assert live is not None and not live.passed
    triage_calls = [c for c in state.classifications if c.event_payload.site == "triage_failure"]
    assert len(triage_calls) == 1 and triage_calls[0].event_payload.probability == 0.9
    assert coordinator.check_live("customer_spend_30d", as_of=AS_OF).passed


def test_unknown_cause_opens_a_question_without_a_repair_attempt(world):
    registry, backend, semantic, conn = world
    apply_drift(conn, DriftKind.MAPPING_CHANGE)
    coordinator = Coordinator(
        registry, backend, semantic, classifier=scripted(CauseClass.UNKNOWN, 0.95)
    )
    outcome = coordinator.check_live("customer_spend_30d", as_of=AS_OF)
    assert outcome.question_id is not None and outcome.proposal_id is None
    question = registry.state.questions[outcome.question_id]
    assert "No repair was attempted" in question.question
    assert "Alternatives tried: none" in question.question
    assert not any(p.kind_of_change == "repair" for p in registry.state.proposals.values())


def test_low_probability_escalates_even_for_a_repairable_class(world):
    registry, backend, semantic, conn = world
    apply_drift(conn, DriftKind.MAPPING_CHANGE)
    coordinator = Coordinator(
        registry, backend, semantic, classifier=scripted(CauseClass.IDENTITY_MAPPING_CHANGE, 0.4)
    )
    outcome = coordinator.check_live("customer_spend_30d", as_of=AS_OF)
    assert outcome.triage is not None and outcome.triage.strategy is RepairStrategy.ESCALATE
    assert outcome.question_id is not None


def test_unfixable_corruption_escalates_with_the_four_parts(world):
    registry, backend, semantic, conn = world
    apply_drift(conn, DriftKind.DUPLICATE_MAPPING)
    coordinator = Coordinator(
        registry,
        backend,
        semantic,
        repair_bound=1,
        classifier=scripted(CauseClass.IDENTITY_MAPPING_CHANGE, 0.9),
    )
    outcome = coordinator.check_live("customer_spend_30d", as_of=AS_OF)
    assert not outcome.passed
    assert outcome.activation is not None and not outcome.activation.activated
    questions = [q for q in registry.state.questions.values() if q.decision_id is None]
    assert len(questions) == 1
    text = questions[0].question
    assert "conserve_spend_usd" in text
    assert "magnitude" in text
    assert "triage identity_mapping_change p=0.90" in text
    assert "Alternatives tried: 1 x re_plan_join" in text
    # Consumers still read the last verified implementation while the repair is open.
    state = registry.state
    assert state.interface_of("customer_spend_30d") == "customer_spend_30d__v1__i1"
    verified = state.last_verified("customer_spend_30d")
    assert verified is not None and verified[0] == "customer_spend_30d__v1__i1"
    assert "purchases" in verified[1]


def test_wait_for_source_records_the_failure_and_nothing_else(world):
    registry, backend, semantic, conn = world
    apply_drift(conn, DriftKind.LATE_PURCHASES)
    coordinator = Coordinator(
        registry, backend, semantic, classifier=scripted(CauseClass.SOURCE_LAG, 0.9)
    )
    before = len(registry.state.proposals)
    outcome = coordinator.check_live("customer_spend_30d", as_of=AS_OF)
    assert not outcome.passed and outcome.proposal_id is None and outcome.question_id is None
    assert len(registry.state.proposals) == before


def test_repair_of_a_frozen_asset_is_recorded_and_refused(world):
    registry, backend, semantic, conn = world
    apply_drift(conn, DriftKind.MAPPING_CHANGE)
    coordinator = Coordinator(
        registry, backend, semantic, classifier=scripted(CauseClass.IDENTITY_MAPPING_CHANGE, 0.9)
    )
    coordinator.freeze("customer_spend_30d", reason="hold", **OPERATOR)
    outcome = coordinator.check_live("customer_spend_30d", as_of=AS_OF)
    assert outcome.frozen_by == "customer_spend_30d"
    assert registry.state.proposals[outcome.proposal_id].status is ProposalStatus.REJECTED
    assert registry.state.assets["customer_spend_30d"].active_implementation == (
        "customer_spend_30d__v1__i1"
    )


def test_rule_triage_baseline_reads_the_structured_evidence(world):
    registry, backend, semantic, conn = world
    apply_drift(conn, DriftKind.MAPPING_CHANGE)
    coordinator = Coordinator(registry, backend, semantic)
    outcome = coordinator.check_live("customer_spend_30d", as_of=AS_OF)
    assert outcome.triage is not None
    assert outcome.triage.model_id == "rules"
    assert outcome.triage.cause is CauseClass.IDENTITY_MAPPING_CHANGE
    assert outcome.activation is not None and outcome.activation.activated


# ---- R6.18 reproduction artifacts, source observation ----


def test_deployments_carry_reproduction_artifacts(shared):
    registry, _, _, _ = shared
    deployment = registry.state.deployments["customer_spend_daily_d1"]
    artifacts = deployment.artifacts
    assert artifacts is not None
    assert set(artifacts.sql_refs) == {
        "customer_spend_daily",
        "customer_spend_30d",
        "customer_spend_90d",
    }
    assert artifacts.backend == "duckdb" and artifacts.backend_version not in ("", "unknown")
    assert "purchases" in artifacts.source_positions
    assert artifacts.retention["source:purchases"] == AS_OF + timedelta(days=30)
    assert artifacts.retention["sql"] is None
    for impl_id in artifacts.sql_refs.values():
        assert registry.state.implementations[impl_id].sql


def test_observe_sources_records_each_time_rule_source(world):
    registry, backend, semantic, _ = world
    positions = Coordinator(registry, backend, semantic).observe_sources(as_of=AS_OF)
    assert "purchases" in positions
    assert registry.state.source_positions["purchases"].observed_at == AS_OF
    assert registry.state.assets["customer_spend_30d"].rows_processed
