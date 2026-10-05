import pytest

from tests.conftest import AS_OF, bundled_spec
from veena.backends import DuckDBBackend
from veena.coordinator import Coordinator
from veena.errors import CoordinationError, RevisionConflictError
from veena.planner import IdentityJoin, compile_plan
from veena.registry import (
    AssetRequested,
    CommitmentRegistered,
    Event,
    EventKind,
    MemoryEventStore,
    Registry,
    SpecResolved,
)
from veena.registry.state import AssetStatus


def _request(registry, spec, consumer="app:sales"):
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


def naive_planner(spec, semantic, attempt):
    plan = compile_plan(spec, semantic)
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
def setup(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    spec = bundled_spec("customer_spend_30d")
    _request(registry, spec)
    return registry, DuckDBBackend(scenario_conn), semantic


def test_activate_runs_the_full_loop(setup, scenario_conn):
    registry, backend, semantic = setup
    outcome = Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    assert outcome.activated
    assert outcome.attempts == 1
    assert outcome.deployment_id == "customer_spend_30d:d1"
    kinds = [s.event.kind for s in registry.events()]
    assert kinds == [
        EventKind.ASSET_REQUESTED,
        EventKind.SPEC_RESOLVED,
        EventKind.COMMITMENT_REGISTERED,
        EventKind.PROPOSAL_OPENED,
        EventKind.IMPLEMENTATION_PLANNED,
        EventKind.IMPLEMENTATION_REFRESHED,
        EventKind.VERIFICATION_RECORDED,
        EventKind.DEPLOYMENT_ACTIVATED,
    ]
    asset = registry.state.assets["customer_spend_30d"]
    assert asset.status is AssetStatus.ACTIVE
    assert asset.active_implementation == "customer_spend_30d__v1__i1"
    assert "purchases" in asset.source_progress
    rows = scenario_conn.execute("SELECT count(*) FROM customer_spend_30d").fetchone()
    assert rows[0] == 200
    verification = next(iter(registry.state.verifications.values()))
    assert verification.grade == "reconciliation"
    assert verification.source_progress == asset.source_progress


def test_commitment_registered_during_verification_blocks_activation(setup):
    registry, backend, semantic = setup
    coordinator = Coordinator(registry, backend, semantic)
    handle = coordinator.propose("customer_spend_30d", as_of=AS_OF)
    handle = coordinator.implement(handle)
    handle = coordinator.verify(handle)
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
    with pytest.raises(RevisionConflictError) as info:
        coordinator.deploy(handle)
    assert info.value.expected == handle.assumed_revision
    assert registry.state.assets["customer_spend_30d"].status is AssetStatus.SPECIFIED


def test_repair_bound_escalates_with_a_question(setup):
    registry, backend, semantic = setup
    outcome = Coordinator(
        registry, backend, semantic, planner=naive_planner, repair_bound=1
    ).activate("customer_spend_30d", as_of=AS_OF)
    assert not outcome.activated
    assert outcome.question_id == "customer_spend_30d:q1"
    verifications = list(registry.state.verifications.values())
    assert len(verifications) == 1
    assert not verifications[0].passed
    question = registry.state.questions["customer_spend_30d:q1"]
    assert "reconcile_spend_usd" in question.question
    assert registry.state.assets["customer_spend_30d"].status is AssetStatus.SPECIFIED


def test_deploy_refuses_unverified_or_failed(setup):
    registry, backend, semantic = setup
    coordinator = Coordinator(registry, backend, semantic, planner=naive_planner)
    handle = coordinator.propose("customer_spend_30d", as_of=AS_OF)
    with pytest.raises(CoordinationError, match="not verified"):
        coordinator.deploy(handle)
    handle = coordinator.verify(coordinator.implement(handle))
    with pytest.raises(CoordinationError, match="failed verification"):
        coordinator.deploy(handle)


def test_propose_needs_a_spec(setup):
    registry, backend, semantic = setup
    with pytest.raises(CoordinationError, match="no resolved specification"):
        Coordinator(registry, backend, semantic).propose("nobody", as_of=AS_OF)
