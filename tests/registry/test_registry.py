import pytest

from tests.conftest import make_spec
from veena.errors import RegistryError
from veena.evidence import CheckResult, Grade
from veena.registry import (
    AssetRequested,
    AssetRetired,
    CommitmentRegistered,
    CommitmentReleased,
    DecisionRecorded,
    DeploymentActivated,
    Event,
    ImplementationPlanned,
    MemoryEventStore,
    ProposalOpened,
    QuestionOpened,
    Registry,
    SpecResolved,
    VerificationRecorded,
)
from veena.registry.state import AssetStatus


def ev(payload, actor="engine", authority="policy:routine@1") -> Event:
    return Event(actor=actor, authority=authority, payload=payload)


@pytest.fixture
def registry() -> Registry:
    return Registry(MemoryEventStore())


def request_flow(registry: Registry, consumer: str = "app:sales") -> None:
    spec = make_spec()
    registry.append(
        ev(
            AssetRequested(asset_id=spec.asset_id, consumer=consumer, request_text="30d"),
            actor=consumer,
        )
    )
    registry.append(ev(SpecResolved(spec=spec)))
    registry.append(
        ev(
            CommitmentRegistered(
                commitment_id=f"{spec.asset_id}:{consumer}",
                asset_id=spec.asset_id,
                consumer=consumer,
                incorporate_within_seconds=300,
            )
        )
    )


def test_request_flow_projects_asset_and_commitment(registry):
    request_flow(registry)
    asset = registry.state.assets["customer_spend_30d"]
    assert asset.status is AssetStatus.SPECIFIED
    assert asset.spec is not None
    assert asset.commitments == ("customer_spend_30d:app:sales",)
    assert asset.history == (1, 2, 3)
    assert registry.revision == 3


def test_spec_for_unknown_asset_is_rejected_before_storing(registry):
    with pytest.raises(RegistryError, match="unknown asset"):
        registry.append(ev(SpecResolved(spec=make_spec("nobody_asked"))))
    assert registry.revision == 0


def test_decision_must_pick_an_offered_alternative(registry):
    request_flow(registry)
    registry.append(
        ev(
            QuestionOpened(
                question_id="q1",
                asset_id="customer_spend_30d",
                question="Which household definition?",
                alternatives=("primary_residence", "linked_billing"),
            )
        )
    )
    with pytest.raises(RegistryError, match="not one of"):
        registry.append(
            ev(
                DecisionRecorded(
                    decision_id="d1",
                    question_id="q1",
                    selected="something_else",
                    rationale="",
                    scope="asset:customer_spend_30d",
                ),
                actor="user:owner",
                authority="role:data_owner",
            )
        )
    registry.append(
        ev(
            DecisionRecorded(
                decision_id="d1",
                question_id="q1",
                selected="primary_residence",
                rationale="matches the support team's definition",
                scope="asset:customer_spend_30d",
            ),
            actor="user:owner",
            authority="role:data_owner",
        )
    )
    assert registry.state.questions["q1"].decision_id == "d1"
    assert registry.state.decisions["d1"].actor == "user:owner"


def _implementation(impl_id="i1"):
    from veena.planner import Dependency, Implementation, Plan, RefreshRule, SourceScan

    return Implementation(
        implementation_id=impl_id,
        asset_id="customer_spend_30d",
        spec_version=1,
        plan=Plan(
            asset_id="customer_spend_30d",
            spec_version=1,
            semantic_fingerprint="abc",
            steps=(SourceScan(source="purchases", table="purchases", columns=("purchase_id",)),),
        ),
        backend="duckdb",
        sql="SELECT 1",
        dependencies=(Dependency(name="purchases", kind="source", version="abc"),),
        refresh_rule=RefreshRule(
            incorporate_within_seconds=300,
            boundary_timezone="UTC",
            roll_at_day_boundary=True,
            correction_lookback_days=30,
        ),
    )


def _verification(vid, passed):
    from tests.conftest import AS_OF

    return VerificationRecorded(
        verification_id=vid,
        proposal_id="p1",
        implementation_id="i1",
        as_of=AS_OF,
        grade=Grade.RECONCILIATION,
        passed=passed,
        checks=(
            CheckResult(
                name="reconcile_spend_usd",
                description="",
                grade=Grade.RECONCILIATION,
                structural=False,
                passed=passed,
                detail="" if passed else "double counted remapped accounts",
            ),
        ),
    )


def test_deployment_requires_a_passing_verification(registry):
    request_flow(registry)
    registry.append(
        ev(
            ProposalOpened(
                proposal_id="p1",
                summary="direct aggregation",
                changes=("create customer_spend_30d impl i1",),
                affected_assets=("customer_spend_30d",),
                assumed_revision=registry.revision,
            )
        )
    )
    with pytest.raises(RegistryError, match="unknown implementation"):
        registry.append(ev(_verification("v0", passed=True)))
    registry.append(ev(ImplementationPlanned(proposal_id="p1", implementation=_implementation())))
    with pytest.raises(RegistryError, match="already exists"):
        registry.append(
            ev(ImplementationPlanned(proposal_id="p1", implementation=_implementation()))
        )
    registry.append(ev(_verification("v1", passed=False)))
    with pytest.raises(RegistryError, match="did not pass"):
        registry.append(
            ev(
                DeploymentActivated(
                    deployment_id="dep1",
                    proposal_id="p1",
                    verification_id="v1",
                    assumed_revision=registry.revision,
                    activated={"customer_spend_30d": "i1"},
                )
            )
        )
    registry.append(ev(_verification("v2", passed=True)))
    with pytest.raises(RegistryError, match="not an implementation"):
        registry.append(
            ev(
                DeploymentActivated(
                    deployment_id="dep1",
                    proposal_id="p1",
                    verification_id="v2",
                    assumed_revision=registry.revision,
                    activated={"customer_spend_30d": "i_other"},
                )
            )
        )
    registry.append(
        ev(
            DeploymentActivated(
                deployment_id="dep1",
                proposal_id="p1",
                verification_id="v2",
                assumed_revision=registry.revision,
                activated={"customer_spend_30d": "i1"},
            )
        )
    )
    asset = registry.state.assets["customer_spend_30d"]
    assert asset.status is AssetStatus.ACTIVE
    assert asset.active_implementation == "i1"
    assert asset.implementations == ("i1",)
    assert registry.state.proposals["p1"].deployment_id == "dep1"
    assert registry.state.proposals["p1"].implementations == ("i1",)


def test_retirement_waits_for_commitments(registry):
    request_flow(registry)
    with pytest.raises(RegistryError, match="still has commitments"):
        registry.append(ev(AssetRetired(asset_id="customer_spend_30d", reason="unused")))
    registry.append(ev(CommitmentReleased(commitment_id="customer_spend_30d:app:sales")))
    registry.append(ev(AssetRetired(asset_id="customer_spend_30d", reason="no commitments")))
    assert registry.state.assets["customer_spend_30d"].status is AssetStatus.RETIRED
    assert registry.state.commitments["customer_spend_30d:app:sales"].released


def test_registry_replays_from_store(registry):
    request_flow(registry)
    store = registry._store
    reopened = Registry(store)
    assert reopened.state.assets.keys() == registry.state.assets.keys()
    assert reopened.revision == 3
