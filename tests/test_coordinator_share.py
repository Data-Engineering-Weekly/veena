from datetime import date

import pytest

from tests.conftest import AS_OF, bundled_spec
from veena.backends import DuckDBBackend
from veena.coordinator import Coordinator, default_planner
from veena.errors import RevisionConflictError
from veena.planner import IdentityJoin
from veena.policy import ReorganizationPolicy
from veena.registry import (
    AssetRequested,
    CommitmentRegistered,
    Event,
    EventKind,
    MemoryEventStore,
    Registry,
    SpecResolved,
)
from veena.registry.state import AssetStatus, ProposalStatus
from veena.scenario import ScenarioConfig, approved_spend_sql, build_scenario


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


def naive(plan):
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


@pytest.fixture
def world(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    first = Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    assert first.activated and first.kind_of_change == "direct"
    request(registry, bundled_spec("customer_spend_90d"), "agent:support")
    return registry, backend, semantic, scenario_conn


def test_second_request_creates_shared_daily_and_rewires_first(world):
    registry, backend, semantic, conn = world
    outcome = Coordinator(registry, backend, semantic).activate("customer_spend_90d", as_of=AS_OF)
    assert outcome.activated, outcome
    assert outcome.kind_of_change == "share"
    assert outcome.attempts == 1

    state = registry.state
    assert set(state.assets) == {"customer_spend_30d", "customer_spend_90d", "customer_spend_daily"}
    assert all(a.status is AssetStatus.ACTIVE for a in state.assets.values())
    deployment = state.deployments[outcome.deployment_id]
    assert set(deployment.activated) == set(state.assets)
    assert deployment.replaced["customer_spend_30d"] == "customer_spend_30d__v1__i1"
    assert deployment.replaced["customer_spend_90d"] is None
    assert not deployment.staged
    assert state.assets["customer_spend_30d"].depends_on == ("customer_spend_daily",)
    assert state.assets["customer_spend_90d"].depends_on == ("customer_spend_daily",)
    assert state.assets["customer_spend_30d"].active_implementation == "customer_spend_30d__v1__i2"

    proposal = state.proposals[outcome.proposal_id]
    assert proposal.kind_of_change == "share"
    assert proposal.status is ProposalStatus.ACTIVATED
    assert proposal.shared_asset == "customer_spend_daily"
    assert proposal.dependents == ("customer_spend_30d", "customer_spend_90d")
    assert proposal.cost_current_rows is not None and proposal.cost_proposed_rows is not None
    assert proposal.cost_current_rows > proposal.cost_proposed_rows
    assert any("window differs" in a for a in proposal.alternatives)
    assert len(proposal.verifications) == 3
    assert "equivalence of customer_spend_30d" in " ".join(proposal.evidence_required)

    daily_spec = state.assets["customer_spend_daily"].spec
    assert daily_spec is not None
    assert str(daily_spec.provenance["grain.time_bucket"]) == f"proposal:{outcome.proposal_id}"

    for days, view in ((30, "customer_spend_30d"), (90, "customer_spend_90d")):
        got = conn.execute(f"SELECT customer_id, spend_usd FROM {view} ORDER BY 1").fetchall()
        want = conn.execute(approved_spend_sql(days, date(2026, 9, 30))).fetchall()
        assert [(g[0], float(g[1])) for g in got] == [(w[0], float(w[1])) for w in want]

    kinds = [s.event.kind for s in registry.events()]
    assert kinds.count(EventKind.BACKFILL_PROGRESSED) == 1
    assert kinds.count(EventKind.DEPLOYMENT_ACTIVATED) == 2
    thirty = state.verifications[state.proposals[outcome.proposal_id].verifications[1]]
    names = [c.name for c in thirty.checks]
    assert "equivalence_at_version" in names
    assert thirty.grade == "reconciliation"


def test_naive_shared_candidate_is_caught_repaired_and_then_activated(world):
    registry, backend, semantic, _ = world

    def planner(spec, sem, attempt):
        plan = default_planner(spec, sem, attempt)
        return naive(plan) if attempt == 1 else plan

    outcome = Coordinator(registry, backend, semantic, planner=planner, repair_bound=2).activate(
        "customer_spend_90d", as_of=AS_OF
    )
    assert outcome.activated
    assert outcome.attempts == 2
    proposal = registry.state.proposals[outcome.proposal_id]
    verifications = [registry.state.verifications[v] for v in proposal.verifications]
    assert [v.passed for v in verifications] == [False, False, False, True, True, True]
    thirty_failed = verifications[1]
    assert thirty_failed.implementation_id.startswith("customer_spend_30d")
    assert "customer_spend_daily" in thirty_failed.uncertainty
    assert any("customer_spend_daily" in c.detail for c in thirty_failed.checks if not c.passed)
    assert len(registry.state.deployments) == 2


def test_policy_minimum_benefit_blocks_sharing(world):
    registry, backend, semantic, _ = world
    policy = ReorganizationPolicy(min_benefit_rows=10**9)
    outcome = Coordinator(registry, backend, semantic, policy=policy).activate(
        "customer_spend_90d", as_of=AS_OF
    )
    assert outcome.activated
    assert outcome.kind_of_change == "direct"
    rejected = [p for p in registry.state.proposals.values() if p.status is ProposalStatus.REJECTED]
    assert len(rejected) == 1
    assert "below the minimum" in rejected[0].reason
    assert (
        registry.state.assets["customer_spend_30d"].active_implementation
        == "customer_spend_30d__v1__i1"
    )
    assert registry.state.assets["customer_spend_daily"].status is AssetStatus.SPECIFIED


def test_policy_cap_blocks_sharing(world):
    registry, backend, semantic, _ = world
    policy = ReorganizationPolicy(max_reorganizations_per_period=0)
    outcome = Coordinator(registry, backend, semantic, policy=policy).activate(
        "customer_spend_90d", as_of=AS_OF
    )
    assert outcome.kind_of_change == "direct"
    rejected = [p for p in registry.state.proposals.values() if p.status is ProposalStatus.REJECTED]
    assert "the cap is 0" in rejected[0].reason


def test_insufficient_history_opens_a_question(semantic):
    conn = build_scenario(None, ScenarioConfig(history_days=60))
    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    request(registry, bundled_spec("customer_spend_90d"), "agent:support")
    outcome = Coordinator(registry, backend, semantic).activate("customer_spend_90d", as_of=AS_OF)
    assert not outcome.activated
    assert outcome.question_id == "customer_spend_90d_q1"
    question = registry.state.questions[outcome.question_id]
    assert "90 days of purchases history" in question.question
    assert "load_more_history" in question.alternatives
    assert registry.state.assets["customer_spend_90d"].status is AssetStatus.SPECIFIED


def test_commitment_during_verification_fails_then_second_proposal_activates(world):
    registry, backend, semantic, _ = world
    coordinator = Coordinator(registry, backend, semantic)
    handle = coordinator.propose("customer_spend_90d", as_of=AS_OF)
    assert not isinstance(handle, str)
    handle = coordinator.verify(coordinator.implement(handle))
    assert handle.passed
    registry.append(
        Event(
            actor="engine",
            authority="request:agent:ops",
            payload=CommitmentRegistered(
                commitment_id="customer_spend_30d:agent:ops",
                asset_id="customer_spend_30d",
                consumer="agent:ops",
                incorporate_within_seconds=60,
            ),
        )
    )
    with pytest.raises(RevisionConflictError):
        coordinator.deploy(handle)
    second = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert second.activated
    assert second.kind_of_change == "share"
    assert len(registry.state.proposals) == 3
    assert second.proposal_id == "customer_spend_daily_p2"


class StagedBackend(DuckDBBackend):
    @property
    def atomic_publish(self) -> bool:
        return False


def test_staged_migration_records_each_switch(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    backend = StagedBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    request(registry, bundled_spec("customer_spend_90d"), "agent:support")
    outcome = Coordinator(registry, backend, semantic).activate("customer_spend_90d", as_of=AS_OF)
    assert outcome.activated and outcome.deployment_id and outcome.proposal_id
    kinds = [s.event.kind for s in registry.events()]
    assert kinds.count(EventKind.INTERFACE_SWITCHED) == 4  # 1 for the direct build, 3 for the share
    deployment = registry.state.deployments[outcome.deployment_id]
    assert deployment.staged
    assert registry.state.proposals[outcome.proposal_id].switched == deployment.activated


def test_rollback_restores_previous_interfaces(world):
    registry, backend, semantic, conn = world
    coordinator = Coordinator(registry, backend, semantic)
    outcome = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert outcome.activated and outcome.deployment_id
    coordinator.rollback(outcome.deployment_id, reason="support saw wrong totals", as_of=AS_OF)
    state = registry.state
    assert state.deployments[outcome.deployment_id].rolled_back
    assert state.assets["customer_spend_30d"].active_implementation == "customer_spend_30d__v1__i1"
    assert state.assets["customer_spend_30d"].depends_on == ()
    assert state.assets["customer_spend_90d"].status is AssetStatus.SPECIFIED
    assert state.assets["customer_spend_daily"].status is AssetStatus.SPECIFIED
    view_sql = conn.execute(
        "SELECT sql FROM duckdb_views() WHERE view_name = 'customer_spend_30d'"
    ).fetchone()[0]
    assert "customer_spend_30d__v1__i1" in view_sql
    assert (
        conn.execute(
            "SELECT count(*) FROM duckdb_views() WHERE view_name = 'customer_spend_90d'"
        ).fetchone()[0]
        == 0
    )
    rolled = next(
        s.event.payload
        for s in registry.events()
        if s.event.kind == EventKind.DEPLOYMENT_ROLLED_BACK
    )
    assert rolled.affected_consumers == ("agent:support", "app:sales")
    assert rolled.interval_end == AS_OF


def test_require_approval_waits_then_approve_runs(world):
    registry, backend, semantic, _ = world
    policy = ReorganizationPolicy(require_approval=True)
    coordinator = Coordinator(registry, backend, semantic, policy=policy)
    pending = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert pending.pending_approval and not pending.activated and pending.proposal_id
    assert registry.state.proposals[pending.proposal_id].status is ProposalStatus.OPEN
    assert registry.state.assets["customer_spend_90d"].status is AssetStatus.SPECIFIED
    done = coordinator.approve(pending.proposal_id, as_of=AS_OF, reason="owner agreed")
    assert done.activated and done.kind_of_change == "share"
    assert registry.state.proposals[pending.proposal_id].status is ProposalStatus.ACTIVATED


def test_reject_records_reason(world):
    registry, backend, semantic, _ = world
    coordinator = Coordinator(
        registry, backend, semantic, policy=ReorganizationPolicy(require_approval=True)
    )
    pending = coordinator.activate("customer_spend_90d", as_of=AS_OF)
    assert pending.proposal_id
    coordinator.reject(pending.proposal_id, reason="not this quarter")
    proposal = registry.state.proposals[pending.proposal_id]
    assert proposal.status is ProposalStatus.REJECTED
    assert proposal.reason == "not this quarter"


def test_third_consumer_reads_from_the_shared_asset_directly(world):
    registry, backend, semantic, conn = world
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
    outcome = Coordinator(registry, backend, semantic).activate("customer_spend_60d", as_of=AS_OF)
    assert outcome.activated
    assert outcome.kind_of_change == "direct"
    assert registry.state.assets["customer_spend_60d"].depends_on == ("customer_spend_daily",)
    got = conn.execute(
        "SELECT customer_id, spend_usd FROM customer_spend_60d ORDER BY 1"
    ).fetchall()
    want = conn.execute(approved_spend_sql(60, date(2026, 9, 30))).fetchall()
    assert [(g[0], float(g[1])) for g in got] == [(w[0], float(w[1])) for w in want]
