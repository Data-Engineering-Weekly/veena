"""The operator view, trail, lags, impact, and narration (R6.1-R6.4, R6.8, R6.10-R6.12)."""

from datetime import timedelta
from typing import Any

import pytest

from tests.conftest import AS_OF, bundled_spec
from tests.registry.test_invariants import activate, ev, impl, verification
from tests.test_coordinator_share import request
from veena.backends import DuckDBBackend
from veena.coordinator import Coordinator
from veena.errors import CoordinationError
from veena.models import ScriptedClassifier, ScriptedDrafter
from veena.ops import (
    TrailNarrationRequest,
    lags_for,
    narrate,
    operator_view,
    render_view,
    trail,
    unmet_commitments,
)
from veena.policy import CauseClass
from veena.registry import (
    AssetRequested,
    CommitmentRegistered,
    DeploymentActivated,
    EventKind,
    ImplementationPlanned,
    ImplementationRefreshed,
    MemoryEventStore,
    ProposalApproved,
    ProposalOpened,
    Registry,
    SourcePositionObserved,
    SpecResolved,
)
from veena.scenario import DriftKind, apply_drift

OPERATOR: dict[str, Any] = {"actor": "user:oncall", "authority": "role:operator"}


@pytest.fixture
def shared(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    request(registry, bundled_spec("customer_spend_90d"), "agent:support")
    outcome = Coordinator(registry, backend, semantic).activate("customer_spend_90d", as_of=AS_OF)
    assert outcome.activated
    return registry, backend, semantic, scenario_conn


@pytest.fixture
def synthetic():
    r = Registry(MemoryEventStore())
    r.append(
        ev(AssetRequested(asset_id="a_30d", consumer="app:a", request_text="t"), actor="app:a")
    )
    from tests.conftest import make_spec

    r.append(ev(SpecResolved(spec=make_spec("a_30d"))))
    return r


# ---- R6.2 one test per condition ----


def test_freshness_is_unmet_from_observed_source_positions(shared):
    registry, _, _, _ = shared
    progress = registry.state.assets["customer_spend_90d"].source_progress["purchases"]
    registry.append(
        ev(
            SourcePositionObserved(
                source="purchases", position=progress + timedelta(hours=2), observed_at=AS_OF
            )
        )
    )
    unmet = unmet_commitments(registry.state, as_of=AS_OF)
    conditions = {(u.commitment_id, u.condition) for u in unmet}
    assert ("customer_spend_90d:agent:support", "freshness") in conditions
    assert ("customer_spend_30d:app:sales", "freshness") in conditions
    reason = next(u.reason for u in unmet if u.condition == "freshness")
    assert "7200s behind, bound 300s" in reason


def test_failed_live_verification_is_unmet_and_degraded_until_a_fix_deploys(shared):
    registry, backend, semantic, conn = shared
    apply_drift(conn, DriftKind.MAPPING_CHANGE)
    coordinator = Coordinator(
        registry,
        backend,
        semantic,
        classifier=ScriptedClassifier({"TriageInput": (CauseClass.UNKNOWN, 0.9)}),
    )
    coordinator.check_live("customer_spend_30d", as_of=AS_OF)
    unmet = unmet_commitments(registry.state, as_of=AS_OF)
    hit = [u for u in unmet if u.condition == "verification"]
    assert len(hit) == 1 and hit[0].degraded and "reconcile_spend_usd" in hit[0].reason
    view = operator_view(registry.state, as_of=AS_OF)
    assert view.degraded[0].reads == "customer_spend_30d__v1__i2"
    assert view.degraded[0].verified_implementation == "customer_spend_30d__v1__i2"
    assert "purchases" in view.degraded[0].verified_position
    text = render_view(view, registry.state)
    assert "degraded assets" in text and "source position purchases" in text
    repaired = Coordinator(
        registry,
        backend,
        semantic,
        classifier=ScriptedClassifier({"TriageInput": (CauseClass.IDENTITY_MAPPING_CHANGE, 0.9)}),
    ).check_live("customer_spend_30d", as_of=AS_OF)
    assert repaired.activation is not None and repaired.activation.activated
    assert not [
        u for u in unmet_commitments(registry.state, as_of=AS_OF) if u.condition == "verification"
    ]


def test_schema_change_on_a_stable_commitment_is_unmet(synthetic):
    r = synthetic
    activate(r)
    r.append(
        ev(
            CommitmentRegistered(
                commitment_id="a_30d:app:a",
                asset_id="a_30d",
                consumer="app:a",
                incorporate_within_seconds=300,
                schema_stable=True,
            )
        )
    )
    assert r.state.commitments["a_30d:app:a"].interface_columns == ("purchase_id",)
    wider = impl("i2").model_copy(
        update={
            "plan": impl("i2").plan.model_copy(
                update={
                    "steps": (
                        impl("i2")
                        .plan.steps[0]
                        .model_copy(update={"columns": ("purchase_id", "extra")}),
                    )
                }
            )
        }
    )
    r.append(
        ev(
            ProposalOpened(
                proposal_id="p2",
                summary="s",
                changes=("c",),
                affected_assets=("a_30d",),
                assumed_revision=0,
            )
        )
    )
    r.append(ev(ProposalApproved(proposal_id="p2")))
    r.append(ev(ImplementationPlanned(proposal_id="p2", implementation=wider)))
    r.append(ev(verification("p2_v1", "p2", "i2")))
    r.append(
        ev(
            DeploymentActivated(
                deployment_id="d2",
                proposal_id="p2",
                verification_id="p2_v1",
                assumed_revision=0,
                activated={"a_30d": "i2"},
                replaced={"a_30d": "i1"},
            )
        )
    )
    unmet = unmet_commitments(r.state, as_of=AS_OF)
    assert [u.condition for u in unmet] == ["schema"]
    assert "extra" in unmet[0].reason


def test_cost_above_the_ceiling_is_unmet(synthetic):
    r = synthetic
    activate(r)
    r.append(
        ev(
            CommitmentRegistered(
                commitment_id="a_30d:app:a",
                asset_id="a_30d",
                consumer="app:a",
                incorporate_within_seconds=300,
                cost_ceiling_rows=1000,
            )
        )
    )
    assert not unmet_commitments(r.state, as_of=AS_OF)
    r.append(
        ev(
            ImplementationRefreshed(
                implementation_id="i1",
                as_of=AS_OF,
                row_count=5,
                source_progress={},
                rows_processed=5000,
            )
        )
    )
    unmet = unmet_commitments(r.state, as_of=AS_OF)
    assert [u.condition for u in unmet] == ["cost"]
    assert "5000 fact rows; the ceiling is 1000" in unmet[0].reason


# ---- R6.1, R6.4, R6.8 the view ----


def test_view_prints_the_four_sections_first_and_in_order(shared):
    registry, backend, semantic, _ = shared
    coordinator = Coordinator(registry, backend, semantic)
    coordinator.observe_sources(as_of=AS_OF)
    coordinator.freeze("customer_spend_daily", reason="quarter close", **OPERATOR)
    coordinator.pin(
        "customer_spend_30d", "customer_spend_30d__v1__i1", reason="known good", **OPERATOR
    )
    view = operator_view(registry.state, as_of=AS_OF)
    text = render_view(view, registry.state)
    lines = text.splitlines()
    assert lines[0].startswith("operator view as of")
    order = [
        lines.index("unmet commitments"),
        lines.index("affected consumers"),
        lines.index("recent deployments touching them"),
        lines.index("what the engine proposes next"),
    ]
    assert order == sorted(order) and order[0] == 2
    assert "lag per asset and source (source lag | incorporation lag)" in text
    assert "active brakes" in text
    assert "freeze customer_spend_daily" in text.replace("  ", " ")
    assert "pin    customer_spend_30d" in text
    assert "by user:oncall (role:operator)" in text
    assert {b.kind for b in view.brakes} == {"freeze", "pin"}
    assert all(b.age_seconds >= 0 for b in view.brakes)


def test_lags_keep_source_and_incorporation_apart(shared):
    registry, backend, semantic, _ = shared
    Coordinator(registry, backend, semantic).observe_sources(as_of=AS_OF + timedelta(hours=3))
    lag = lags_for(registry.state, "customer_spend_90d")[0]
    assert lag.source == "purchases"
    assert lag.source_lag_seconds is not None and lag.source_lag_seconds > 3 * 3600
    assert lag.incorporation_lag_seconds == 0


# ---- R6.3, R6.11a the trail ----


def test_trail_has_five_sections_in_order_with_actors(shared):
    registry, _, _, _ = shared
    t = trail(registry.state, "customer_spend_90d:agent:support")
    assert [s.title for s in t.sections] == [
        "deployment",
        "proposal",
        "verification",
        "specification",
        "decisions",
    ]
    text = t.render()
    assert len(text.splitlines()) <= 40
    assert "customer_spend_daily_d1 (active)" in text
    assert "by engine (policy:routine_activation@1)" in text
    assert "customer_spend_daily_p1 (share, activated)" in text
    assert "identity semantic:customer_identity_point_in_time@2" in text
    assert "identity_policy <- policy:approved_spending_semantics@4" in text
    with pytest.raises(CoordinationError, match="unknown commitment"):
        trail(registry.state, "nope")


def test_narration_comes_from_the_structured_trail_and_is_recorded(shared):
    registry, _, _, _ = shared
    seen = {}

    def render(request):
        assert isinstance(request, TrailNarrationRequest)
        seen["sections"] = [s.title for s in request.sections]
        return f"The {request.asset_id} commitment reads a shared daily asset."

    t = trail(registry.state, "customer_spend_90d:agent:support")
    text = narrate(registry, t, ScriptedDrafter(render))
    assert text.startswith("The customer_spend_90d commitment")
    assert seen["sections"] == [s.title for s in t.sections]
    drafts = [d for d in registry.state.drafts if d.event_payload.site == "narrate_trail"]
    assert len(drafts) == 1 and drafts[0].event_payload.text == text
    # Without a drafter the structured trail is unchanged.
    assert trail(registry.state, "customer_spend_90d:agent:support").sections == t.sections


# ---- R6.12 impact ----


def test_rollback_shows_impact_until_acknowledged(shared):
    registry, backend, semantic, _ = shared
    coordinator = Coordinator(registry, backend, semantic)
    coordinator.rollback("customer_spend_daily_d1", reason="support saw wrong totals", as_of=AS_OF)
    view = operator_view(registry.state, as_of=AS_OF)
    assert len(view.impact) == 1
    assert view.impact[0].consumers == ("agent:support", "app:sales")
    text = render_view(view, registry.state)
    assert "assess impact" in text and "veena ops ack customer_spend_daily_d1" in text
    coordinator.acknowledge_impact(
        "customer_spend_daily_d1", reason="support re-ran its report", **OPERATOR
    )
    assert not operator_view(registry.state, as_of=AS_OF).impact
    kinds = [s.event.kind for s in registry.events()]
    assert EventKind.IMPACT_ACKNOWLEDGED in kinds
    with pytest.raises(CoordinationError, match="no impact waiting"):
        coordinator.acknowledge_impact("customer_spend_daily_d1", reason="again", **OPERATOR)
