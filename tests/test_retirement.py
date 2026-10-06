"""Graded retirement on the scenario (R6.13 to R6.16)."""

from datetime import timedelta

import pytest

from tests.conftest import AS_OF, bundled_spec
from tests.test_coordinator_share import request
from veena.backends import DuckDBBackend
from veena.coordinator import Coordinator
from veena.policy import RetentionPolicy
from veena.registry import (
    CommitmentReleased,
    Event,
    EventKind,
    MemoryEventStore,
    Registry,
    RetirementStep,
    UsageObserved,
)
from veena.registry.state import AssetStatus
from veena.retirement import Retirer, consider

LATER = AS_OF + timedelta(days=45)


def release(registry, commitment_id, *, at=AS_OF):
    registry.append(
        Event(
            actor="app:owner",
            authority="request:app:owner",
            effective_at=at,
            payload=CommitmentReleased(commitment_id=commitment_id, reason="no longer needed"),
        )
    )


def relations(conn):
    tables = {r[0] for r in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    views = {r[0] for r in conn.execute("SELECT view_name FROM duckdb_views()").fetchall()}
    return tables, views


@pytest.fixture
def shared(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    request(registry, bundled_spec("customer_spend_90d"), "agent:support")
    assert (
        Coordinator(registry, backend, semantic)
        .activate("customer_spend_90d", as_of=AS_OF)
        .activated
    )
    return registry, backend, semantic, scenario_conn


def test_zero_queries_with_an_open_commitment_never_retires(shared):
    registry, backend, _, _ = shared
    decision = consider(registry.state, "customer_spend_30d", as_of=LATER)
    assert not decision.eligible
    assert "open commitment(s): app:sales" in decision.reasons[0]
    assert Retirer(registry, backend).advance(as_of=LATER) == ()


def test_observed_reads_keep_an_asset_without_commitments(shared):
    registry, _, _, _ = shared
    release(registry, "customer_spend_30d:app:sales")
    registry.append(
        Event(
            actor="engine",
            authority="import:history",
            payload=UsageObserved(
                asset_id="customer_spend_30d",
                consumer="user:analyst",
                queries=0,
                bytes_scanned=0,
                credits=0.0,
                first_seen=AS_OF - timedelta(days=10),
                last_seen=AS_OF - timedelta(days=1),
                runs_per_day=0.1,
                distinct_hours=1,
            ),
        )
    )
    decision = consider(registry.state, "customer_spend_30d", as_of=AS_OF)
    assert not decision.eligible and "user:analyst read it" in decision.reasons[0]
    quiet = consider(registry.state, "customer_spend_30d", as_of=AS_OF + timedelta(days=40))
    assert quiet.eligible and quiet.next_step is RetirementStep.STOP_REFRESH


def test_releasing_sales_moves_the_30d_asset_through_graded_steps(shared):
    registry, backend, _, conn = shared
    release(registry, "customer_spend_30d:app:sales")
    retirer = Retirer(registry, backend)

    first = retirer.advance(as_of=AS_OF)
    assert [(s.asset_id, s.label) for s in first] == [("customer_spend_30d", "stop_refresh")]
    assert registry.state.assets["customer_spend_30d"].status is AssetStatus.ACTIVE

    second = retirer.advance(as_of=AS_OF)
    assert [(s.asset_id, s.label) for s in second] == [("customer_spend_30d", "replace_with_view")]
    tables, views = relations(conn)
    assert "customer_spend_30d__v1__i2" in views and "customer_spend_30d__v1__i2" not in tables
    rows = conn.execute("SELECT count(*) FROM customer_spend_30d").fetchone()[0]
    assert rows == 200

    held = retirer.advance(as_of=AS_OF)
    assert held == ()
    decision = consider(registry.state, "customer_spend_30d", as_of=AS_OF)
    assert decision.next_step is RetirementStep.DROP_MATERIALIZATION and not decision.eligible
    assert "keeps the materialization until" in decision.reasons[-1]

    third = retirer.advance(as_of=LATER)
    assert [(s.asset_id, s.label) for s in third] == [
        ("customer_spend_30d", "drop_materialization")
    ]
    state = registry.state
    assert state.assets["customer_spend_30d"].status is AssetStatus.DORMANT
    assert state.assets["customer_spend_daily"].status is AssetStatus.ACTIVE
    tables, views = relations(conn)
    assert "customer_spend_30d" not in views and "customer_spend_30d__v1__i2" not in views
    assert "customer_spend_30d__v1__i1" not in tables
    kinds = [s.event.kind for s in registry.events()]
    assert kinds.count(EventKind.RETIREMENT_STEP_TAKEN) == 3


def test_retired_asset_keeps_its_specification_decisions_and_history(shared):
    registry, backend, _, _ = shared
    release(registry, "customer_spend_30d:app:sales")
    retirer = Retirer(registry, backend)
    for _ in range(3):
        retirer.advance(as_of=LATER)
    final = retirer.advance(as_of=LATER)
    assert [(s.asset_id, s.label) for s in final] == [("customer_spend_30d", "retire")]
    asset = registry.state.assets["customer_spend_30d"]
    assert asset.status is AssetStatus.RETIRED
    assert asset.spec is not None and asset.spec.ref == "customer_spend_30d@1"
    assert len(asset.implementations) == 2
    assert len(asset.history) > 10
    deployments = [
        d for d in registry.state.deployments.values() if "customer_spend_30d" in d.activated
    ]
    assert len(deployments) == 2


def test_shared_intermediate_waits_for_its_dependents(shared):
    registry, backend, _, conn = shared
    release(registry, "customer_spend_30d:app:sales")
    release(registry, "customer_spend_90d:agent:support")
    retirer = Retirer(registry, backend)
    taken: list[tuple[str, str]] = []
    for _ in range(12):
        step = retirer.advance(as_of=LATER)
        if not step:
            break
        taken.extend((s.asset_id, s.label) for s in step)
    daily_first = next(i for i, (a, _) in enumerate(taken) if a == "customer_spend_daily")
    outputs_dropped = max(
        i
        for i, (a, label) in enumerate(taken)
        if a != "customer_spend_daily" and label == "drop_materialization"
    )
    assert outputs_dropped < daily_first
    retire_order = [a for a, label in taken if label == "retire"]
    assert retire_order[-1] == "customer_spend_daily" and len(retire_order) == 3
    assert taken[-1] == ("customer_spend_daily", "retire")
    assert all(a.status is AssetStatus.RETIRED for a in registry.state.assets.values())
    tables, views = relations(conn)
    assert not any(t.startswith("customer_spend") for t in tables | views)
    daily_steps = [label for a, label in taken if a == "customer_spend_daily"]
    assert daily_steps == ["stop_refresh", "drop_materialization", "retire"]


def test_expensive_assets_wait_longer_and_a_new_commitment_reverses_stop_refresh(shared):
    registry, backend, _, _ = shared
    release(registry, "customer_spend_30d:app:sales")
    policy = RetentionPolicy(expensive_rows=1, keep_expensive_days=200)
    retirer = Retirer(registry, backend, policy=policy)
    retirer.advance(as_of=AS_OF)
    retirer.advance(as_of=AS_OF)
    decision = consider(registry.state, "customer_spend_30d", as_of=LATER, policy=policy)
    assert not decision.eligible and "rebuilding costs" in decision.reasons[0]
    assert retirer.advance(as_of=AS_OF + timedelta(days=201))
    assert registry.state.assets["customer_spend_30d"].status is AssetStatus.DORMANT

    release(registry, "customer_spend_90d:agent:support")
    retirer.advance(as_of=LATER)
    assert (
        registry.state.assets["customer_spend_90d"].retirement_step is RetirementStep.STOP_REFRESH
    )
    request(
        registry,
        bundled_spec("customer_spend_90d").model_copy(update={"spec_version": 2}),
        "agent:crm",
    )
    asset = registry.state.assets["customer_spend_90d"]
    assert asset.retirement_step is None and asset.status is AssetStatus.ACTIVE
