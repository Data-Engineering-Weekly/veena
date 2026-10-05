"""Regression tests for the coordinator review findings."""

import pytest

from tests.conftest import AS_OF, bundled_spec
from veena.backends import DuckDBBackend
from veena.coordinator import Coordinator
from veena.errors import BackendError, CoordinationError, PolicyError, RevisionConflictError
from veena.planner import AssetScan, compile_from_provider
from veena.policy import ReorganizationPolicy
from veena.refs import SemanticRef
from veena.registry import (
    AssetRequested,
    CommitmentRegistered,
    DecisionRecorded,
    Event,
    EventKind,
    MemoryEventStore,
    Registry,
    SpecResolved,
)
from veena.registry.state import AssetStatus, ProposalStatus
from veena.spec import Aggregation, Measure


def request(registry, spec, consumer):
    registry.append(
        Event(
            actor=consumer,
            authority=f"request:{consumer}",
            payload=AssetRequested(asset_id=spec.asset_id, consumer=consumer, request_text="spend"),
        )
    )
    registry.append(
        Event(actor="engine", authority=f"request:{consumer}", payload=SpecResolved(spec=spec))
    )
    registry.append(
        Event(
            actor="engine",
            authority=f"request:{consumer}",
            payload=CommitmentRegistered(
                commitment_id=f"{spec.asset_id}:{consumer}",
                asset_id=spec.asset_id,
                consumer=consumer,
                incorporate_within_seconds=300,
            ),
        )
    )


def tables(conn):
    return {r[0] for r in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()}


@pytest.fixture
def world(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    request(registry, bundled_spec("customer_spend_90d"), "agent:support")
    return registry, backend, semantic, scenario_conn


def test_share_measurement_leaves_no_candidate_table_and_shadow_writes_nothing(world):
    registry, backend, semantic, conn = world
    before = tables(conn)
    policy = ReorganizationPolicy(min_benefit_rows=10**9)
    Coordinator(registry, backend, semantic, policy=policy).activate(
        "customer_spend_90d", as_of=AS_OF
    )
    after = tables(conn)
    assert not any("candidate" in t for t in after)
    assert "customer_spend_daily__v1__i1" not in after  # the live name was never touched
    assert after - before == {"customer_spend_90d__v1__i1"}

    registry2 = Registry(MemoryEventStore())
    request(registry2, bundled_spec("customer_spend_30d"), "app:sales")
    Coordinator(registry2, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    request(registry2, bundled_spec("customer_spend_90d"), "agent:support")
    before = tables(conn)
    with pytest.raises(PolicyError, match="shadow"):
        Coordinator(
            registry2, backend, semantic, policy=ReorganizationPolicy(autonomy="shadow")
        ).activate("customer_spend_90d", as_of=AS_OF)
    assert tables(conn) == before
    proposal = next(p for p in registry2.state.proposals.values() if p.kind_of_change == "share")
    assert proposal.cost_current_rows is None and proposal.status is ProposalStatus.OPEN


def test_deploy_records_before_publishing_and_rolls_back_on_backend_failure(world):
    registry, _, semantic, conn = world

    class FailingPublish(DuckDBBackend):
        def publish_many(self, switches):
            raise BackendError("warehouse refused")

    failing = FailingPublish(conn)
    coordinator = Coordinator(registry, failing, semantic)
    with pytest.raises(BackendError, match="warehouse refused"):
        coordinator.activate("customer_spend_90d", as_of=AS_OF)
    kinds = [s.event.kind for s in registry.events()]
    assert kinds[-2:] == [EventKind.DEPLOYMENT_ACTIVATED, EventKind.DEPLOYMENT_ROLLED_BACK]
    assert (
        registry.state.assets["customer_spend_30d"].active_implementation
        == "customer_spend_30d__v1__i1"
    )
    assert registry.state.assets["customer_spend_90d"].status is AssetStatus.SPECIFIED
    view_sql = conn.execute(
        "SELECT sql FROM duckdb_views() WHERE view_name = 'customer_spend_30d'"
    ).fetchone()[0]
    assert "customer_spend_30d__v1__i1" in view_sql


def test_only_the_latest_deployment_rolls_back(world):
    registry, backend, semantic, _ = world
    coordinator = Coordinator(registry, backend, semantic)
    share = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert share.activated and share.deployment_id
    sixty = bundled_spec("customer_spend_30d").model_copy(
        update={
            "asset_id": "customer_spend_60d",
            "window": bundled_spec("customer_spend_30d").window.model_copy(
                update={"complete_calendar_days": 60}
            ),
        }
    )
    request(registry, sixty, "agent:finance")
    later = Coordinator(registry, backend, semantic).activate("customer_spend_60d", as_of=AS_OF)
    assert later.activated
    # The share deployment is still the latest for its three assets, so it can roll back.
    coordinator.rollback(share.deployment_id, reason="r", as_of=AS_OF)
    assert (
        registry.state.assets["customer_spend_30d"].active_implementation
        == "customer_spend_30d__v1__i1"
    )
    with pytest.raises(CoordinationError, match="already rolled back"):
        coordinator.rollback(share.deployment_id, reason="r", as_of=AS_OF)


def test_superseded_deployment_cannot_roll_back(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    coordinator = Coordinator(registry, backend, semantic)
    first = coordinator.activate("customer_spend_30d", as_of=AS_OF)
    registry.append(
        Event(
            actor="engine",
            authority="x",
            payload=SpecResolved(
                spec=bundled_spec("customer_spend_30d").model_copy(update={"spec_version": 2})
            ),
        )
    )
    second = coordinator.activate("customer_spend_30d", as_of=AS_OF)
    assert second.activated and second.deployment_id != first.deployment_id
    assert first.deployment_id
    with pytest.raises(CoordinationError, match="roll back customer_spend_30d_d2 first"):
        coordinator.rollback(first.deployment_id, reason="r", as_of=AS_OF)


def test_equal_windowed_provider_becomes_a_copy_not_a_plan_error(world, semantic):
    registry, backend, _, conn = world
    twin = bundled_spec("customer_spend_30d").model_copy(update={"asset_id": "sales_spend_30d"})
    request(registry, twin, "app:sales2")
    outcome = Coordinator(registry, backend, semantic).activate("sales_spend_30d", as_of=AS_OF)
    assert outcome.activated and outcome.kind_of_change == "direct"
    impl = registry.state.implementations[
        registry.state.assets["sales_spend_30d"].active_implementation
    ]
    assert isinstance(impl.plan.steps[0], AssetScan) and len(impl.plan.steps) == 1
    assert registry.state.assets["sales_spend_30d"].depends_on == ("customer_spend_30d",)
    rows = conn.execute("SELECT count(*) FROM sales_spend_30d").fetchone()[0]
    assert rows == 200


def test_copy_plan_compiles_only_for_equal_or_serving_windowed_providers(semantic):
    a, b = (
        bundled_spec("customer_spend_30d"),
        bundled_spec("customer_spend_30d").model_copy(update={"asset_id": "twin"}),
    )
    plan = compile_from_provider(b, a, "impl_x", semantic)
    assert isinstance(plan.steps[0], AssetScan)


def test_shared_spec_that_changed_gets_a_new_version(world):
    registry, backend, semantic, _ = world
    first = Coordinator(registry, backend, semantic).activate("customer_spend_90d", as_of=AS_OF)
    assert first.activated
    with_count = bundled_spec("customer_spend_30d").model_copy(
        update={
            "asset_id": "customer_spend_count_30d",
            "measures": (
                *bundled_spec("customer_spend_30d").measures,
                Measure(
                    name="purchase_count",
                    calculation=SemanticRef(name="purchase_count", version=1),
                    aggregation=Aggregation.SUM,
                ),
            ),
        }
    )
    request(registry, with_count, "app:ops")
    before = registry.state.assets["customer_spend_daily"].spec
    outcome = Coordinator(
        registry, backend, semantic, policy=ReorganizationPolicy(max_reorganizations_per_period=5)
    ).activate("customer_spend_count_30d", as_of=AS_OF)
    after = registry.state.assets["customer_spend_daily"].spec
    assert before is not None and after is not None
    if outcome.kind_of_change == "share":
        assert after.spec_version == before.spec_version + 1
        assert {m.name for m in after.measures} == {"spend_usd", "purchase_count"}


def test_resume_keeps_the_provider(world):
    registry, backend, semantic, _ = world
    Coordinator(registry, backend, semantic).activate("customer_spend_90d", as_of=AS_OF)
    sixty = bundled_spec("customer_spend_30d").model_copy(
        update={
            "asset_id": "customer_spend_60d",
            "window": bundled_spec("customer_spend_30d").window.model_copy(
                update={"complete_calendar_days": 60}
            ),
        }
    )
    request(registry, sixty, "agent:finance")
    coordinator = Coordinator(registry, backend, semantic)
    handle = coordinator.propose("customer_spend_60d", as_of=AS_OF)
    assert not isinstance(handle, str)
    resumed = coordinator.resume(handle.proposal_id, as_of=AS_OF)
    assert resumed.providers == {"customer_spend_60d": "customer_spend_daily"}
    assert resumed.assumed_active == handle.assumed_active


def test_deploy_guard_sees_changes_made_before_approval(world):
    registry, backend, semantic, _ = world
    coordinator = Coordinator(
        registry, backend, semantic, policy=ReorganizationPolicy(require_approval=True)
    )
    pending = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert pending.pending_approval and pending.proposal_id
    registry.append(
        Event(
            actor="engine",
            authority="request:x",
            payload=CommitmentRegistered(
                commitment_id="customer_spend_30d:agent:x",
                asset_id="customer_spend_30d",
                consumer="agent:x",
                incorporate_within_seconds=60,
            ),
        )
    )
    with pytest.raises(RevisionConflictError):
        coordinator.approve(pending.proposal_id, as_of=AS_OF)


def test_failed_attempts_drop_their_tables_and_decisions_are_honored(scenario_conn, semantic):
    from tests.test_coordinator import naive_planner

    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    outcome = Coordinator(
        registry, backend, semantic, planner=naive_planner, repair_bound=1
    ).activate("customer_spend_30d", as_of=AS_OF)
    assert not outcome.activated and outcome.question_id
    assert "customer_spend_30d__v1__i1" not in tables(scenario_conn)
    registry.append(
        Event(
            actor="user:owner",
            authority="role:data_owner",
            payload=DecisionRecorded(
                decision_id="d1",
                question_id=outcome.question_id,
                selected="abandon_request",
                rationale="",
                scope="asset:customer_spend_30d",
            ),
        )
    )
    with pytest.raises(CoordinationError, match="abandoned"):
        Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)


def test_allow_more_attempts_extends_the_bound(scenario_conn, semantic):
    from veena.coordinator import default_planner
    from veena.planner import IdentityJoin

    calls = {"n": 0}

    def planner(spec, sem, attempt):
        calls["n"] += 1
        plan = default_planner(spec, sem, attempt)
        if attempt <= 2:
            return plan.model_copy(
                update={
                    "steps": tuple(
                        s.model_copy(update={"respect_validity": False})
                        if isinstance(s, IdentityJoin)
                        else s
                        for s in plan.steps
                    )
                }
            )
        return plan

    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    first = Coordinator(registry, backend, semantic, planner=planner, repair_bound=2).activate(
        "customer_spend_30d", as_of=AS_OF
    )
    assert not first.activated and first.question_id
    registry.append(
        Event(
            actor="user:owner",
            authority="role:data_owner",
            payload=DecisionRecorded(
                decision_id="d1",
                question_id=first.question_id,
                selected="allow_more_attempts",
                rationale="",
                scope="asset:customer_spend_30d",
            ),
        )
    )
    second = Coordinator(registry, backend, semantic, planner=planner, repair_bound=2).activate(
        "customer_spend_30d", as_of=AS_OF
    )
    assert second.activated and second.attempts == 3


def test_direct_deploy_requires_an_approved_proposal(world):
    registry, backend, semantic, _ = world
    coordinator = Coordinator(
        registry, backend, semantic, policy=ReorganizationPolicy(require_approval=True)
    )
    pending = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert pending.proposal_id
    handle = coordinator.resume(pending.proposal_id, as_of=AS_OF)
    with pytest.raises(CoordinationError, match="only an approved proposal runs"):
        coordinator.run(handle)
    with pytest.raises(CoordinationError, match="not verified"):
        coordinator.deploy(handle)
