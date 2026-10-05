"""The projection refuses events that would leave the registry inconsistent."""

import pytest

from tests.conftest import AS_OF, make_spec
from veena.errors import RegistryError, RevisionConflictError
from veena.evidence import CheckResult, Grade
from veena.planner import Dependency, Implementation, Plan, RefreshRule, SourceScan
from veena.registry import (
    AssetObserved,
    AssetRequested,
    AssetRetired,
    CommitmentRegistered,
    CommitmentReleased,
    DecisionRecorded,
    DeploymentActivated,
    DeploymentRolledBack,
    Event,
    ImplementationPlanned,
    MemoryEventStore,
    ProposalApproved,
    ProposalOpened,
    QuestionOpened,
    Registry,
    SpecResolved,
    SqliteEventStore,
    VerificationRecorded,
)
from veena.registry.state import AssetStatus


def ev(payload, actor="engine", authority="policy:test@1"):
    return Event(actor=actor, authority=authority, payload=payload)


def impl(impl_id, asset_id="a_30d", spec_version=1, reads=()):
    return Implementation(
        implementation_id=impl_id,
        asset_id=asset_id,
        spec_version=spec_version,
        plan=Plan(
            asset_id=asset_id,
            spec_version=spec_version,
            semantic_fingerprint="f",
            steps=(SourceScan(source="purchases", table="purchases", columns=("purchase_id",)),),
        ),
        backend="duckdb",
        sql="SELECT 1",
        dependencies=tuple(Dependency(name=r, kind="asset", version="x") for r in reads)
        or (Dependency(name="purchases", kind="source", version="f"),),
        refresh_rule=RefreshRule(
            incorporate_within_seconds=300,
            boundary_timezone="UTC",
            roll_at_day_boundary=True,
            correction_lookback_days=30,
        ),
    )


def verification(vid, proposal_id, impl_id, passed=True):
    return VerificationRecorded(
        verification_id=vid,
        proposal_id=proposal_id,
        implementation_id=impl_id,
        as_of=AS_OF,
        grade=Grade.RECONCILIATION,
        passed=passed,
        checks=(
            CheckResult(
                name="r",
                description="",
                grade=Grade.RECONCILIATION,
                structural=False,
                passed=passed,
            ),
        ),
    )


@pytest.fixture
def registry():
    r = Registry(MemoryEventStore())
    spec = make_spec("a_30d")
    r.append(
        ev(AssetRequested(asset_id="a_30d", consumer="app:a", request_text="t"), actor="app:a")
    )
    r.append(ev(SpecResolved(spec=spec)))
    r.append(
        ev(
            CommitmentRegistered(
                commitment_id="a_30d:app:a",
                asset_id="a_30d",
                consumer="app:a",
                incorporate_within_seconds=300,
            )
        )
    )
    return r


def activate(registry, *, proposal="p1", implementation="i1", deployment="d1", approve=True):
    registry.append(
        ev(
            ProposalOpened(
                proposal_id=proposal,
                summary="s",
                changes=("c",),
                affected_assets=("a_30d",),
                assumed_revision=registry.revision,
            )
        )
    )
    if approve:
        registry.append(ev(ProposalApproved(proposal_id=proposal)))
    registry.append(
        ev(ImplementationPlanned(proposal_id=proposal, implementation=impl(implementation)))
    )
    registry.append(ev(verification(f"{proposal}_v1", proposal, implementation)))
    registry.append(
        ev(
            DeploymentActivated(
                deployment_id=deployment,
                proposal_id=proposal,
                verification_id=f"{proposal}_v1",
                verifications={"a_30d": f"{proposal}_v1"},
                assumed_revision=registry.revision,
                activated={"a_30d": implementation},
                replaced={"a_30d": registry.state.assets["a_30d"].active_implementation},
            )
        )
    )


def test_duplicate_ids_are_rejected(registry):
    registry.append(
        ev(
            QuestionOpened(
                question_id="q1", asset_id="a_30d", question="?", alternatives=("x", "y")
            )
        )
    )
    with pytest.raises(RegistryError, match="question 'q1' already exists"):
        registry.append(
            ev(
                QuestionOpened(
                    question_id="q1", asset_id="a_30d", question="?", alternatives=("x", "y")
                )
            )
        )
    registry.append(
        ev(
            DecisionRecorded(
                decision_id="d1", question_id="q1", selected="x", rationale="", scope="workspace"
            )
        )
    )
    with pytest.raises(RegistryError, match="already decided"):
        registry.append(
            ev(
                DecisionRecorded(
                    decision_id="d2",
                    question_id="q1",
                    selected="y",
                    rationale="",
                    scope="workspace",
                )
            )
        )
    with pytest.raises(RegistryError, match="commitment 'a_30d:app:a' already exists"):
        registry.append(
            ev(
                CommitmentRegistered(
                    commitment_id="a_30d:app:a",
                    asset_id="a_30d",
                    consumer="app:a",
                    incorporate_within_seconds=1,
                )
            )
        )
    registry.append(ev(CommitmentReleased(commitment_id="a_30d:app:a")))
    with pytest.raises(RegistryError, match="already released"):
        registry.append(ev(CommitmentReleased(commitment_id="a_30d:app:a")))
    registry.append(
        ev(
            ProposalOpened(
                proposal_id="p1",
                summary="s",
                changes=("c",),
                affected_assets=("a_30d",),
                assumed_revision=0,
            )
        )
    )
    with pytest.raises(RegistryError, match="proposal 'p1' already exists"):
        registry.append(
            ev(
                ProposalOpened(
                    proposal_id="p1",
                    summary="s",
                    changes=("c",),
                    affected_assets=("a_30d",),
                    assumed_revision=0,
                )
            )
        )


def test_deployment_needs_approval_matching_verification_and_spec_version(registry):
    registry.append(
        ev(
            ProposalOpened(
                proposal_id="p1",
                summary="s",
                changes=("c",),
                affected_assets=("a_30d",),
                assumed_revision=0,
            )
        )
    )
    registry.append(ev(ProposalApproved(proposal_id="p1")))
    with pytest.raises(RegistryError, match="spec version 2"):
        registry.append(
            ev(ImplementationPlanned(proposal_id="p1", implementation=impl("i1", spec_version=2)))
        )
    registry.append(ev(ImplementationPlanned(proposal_id="p1", implementation=impl("i1"))))
    registry.append(ev(ImplementationPlanned(proposal_id="p1", implementation=impl("i2"))))
    registry.append(ev(verification("v_other", "p1", "i2")))
    with pytest.raises(RegistryError, match="is for i2, not i1"):
        registry.append(
            ev(
                DeploymentActivated(
                    deployment_id="d1",
                    proposal_id="p1",
                    verification_id="v_other",
                    assumed_revision=0,
                    activated={"a_30d": "i1"},
                )
            )
        )
    registry.append(ev(verification("v1", "p1", "i1")))
    registry.append(
        ev(
            DeploymentActivated(
                deployment_id="d1",
                proposal_id="p1",
                verification_id="v1",
                assumed_revision=0,
                activated={"a_30d": "i1"},
            )
        )
    )
    assert registry.state.assets["a_30d"].active_implementation == "i1"
    with pytest.raises(RegistryError, match="is activated"):
        registry.append(
            ev(
                DeploymentActivated(
                    deployment_id="d2",
                    proposal_id="p1",
                    verification_id="v1",
                    assumed_revision=0,
                    activated={"a_30d": "i1"},
                )
            )
        )


def test_open_proposal_cannot_be_activated(registry):
    with pytest.raises(RegistryError, match="is open"):
        activate(registry, approve=False)


def test_only_the_latest_deployment_can_be_rolled_back(registry):
    activate(registry)
    activate(registry, proposal="p2", implementation="i2", deployment="d2")
    with pytest.raises(RegistryError, match="only the latest deployment"):
        registry.append(
            ev(
                DeploymentRolledBack(
                    deployment_id="d1",
                    reason="r",
                    affected_consumers=(),
                    interval_start=AS_OF,
                    interval_end=AS_OF,
                )
            )
        )
    registry.append(
        ev(
            DeploymentRolledBack(
                deployment_id="d2",
                reason="r",
                affected_consumers=(),
                interval_start=AS_OF,
                interval_end=AS_OF,
            )
        )
    )
    assert registry.state.assets["a_30d"].active_implementation == "i1"
    with pytest.raises(RegistryError, match="already rolled back"):
        registry.append(
            ev(
                DeploymentRolledBack(
                    deployment_id="d2",
                    reason="r",
                    affected_consumers=(),
                    interval_start=AS_OF,
                    interval_end=AS_OF,
                )
            )
        )


def test_retired_assets_refuse_new_work_and_retirement_checks_dependents(registry):
    activate(registry)
    registry.append(
        ev(AssetRequested(asset_id="b_30d", consumer="app:b", request_text="t"), actor="app:b")
    )
    registry.append(ev(SpecResolved(spec=make_spec("b_30d"))))
    registry.append(
        ev(
            ProposalOpened(
                proposal_id="pb",
                summary="s",
                changes=("c",),
                affected_assets=("b_30d",),
                assumed_revision=0,
            )
        )
    )
    registry.append(ev(ProposalApproved(proposal_id="pb")))
    registry.append(
        ev(
            ImplementationPlanned(
                proposal_id="pb", implementation=impl("ib", asset_id="b_30d", reads=("a_30d",))
            )
        )
    )
    registry.append(ev(verification("vb", "pb", "ib")))
    registry.append(
        ev(
            DeploymentActivated(
                deployment_id="db",
                proposal_id="pb",
                verification_id="vb",
                assumed_revision=0,
                activated={"b_30d": "ib"},
            )
        )
    )
    assert registry.state.dependents_of("a_30d") == ("b_30d",)
    registry.append(ev(CommitmentReleased(commitment_id="a_30d:app:a")))
    with pytest.raises(RegistryError, match="still read by \\['b_30d'\\]"):
        registry.append(ev(AssetRetired(asset_id="a_30d", reason="unused")))
    registry.append(ev(AssetRetired(asset_id="b_30d", reason="unused")))
    registry.append(ev(AssetRetired(asset_id="a_30d", reason="unused")))
    assert registry.state.assets["a_30d"].status is AssetStatus.RETIRED
    with pytest.raises(RegistryError, match="retired asset"):
        registry.append(
            ev(
                CommitmentRegistered(
                    commitment_id="late",
                    asset_id="a_30d",
                    consumer="x",
                    incorporate_within_seconds=1,
                )
            )
        )
    with pytest.raises(RegistryError, match="retired asset"):
        registry.append(
            ev(
                ProposalOpened(
                    proposal_id="p9",
                    summary="s",
                    changes=("c",),
                    affected_assets=("a_30d",),
                    assumed_revision=0,
                )
            )
        )


def test_observation_does_not_overwrite_engine_dependencies(registry):
    activate(registry)
    registry.append(
        ev(
            AssetObserved(
                asset_id="a_30d",
                origin="dbt:x",
                relation="db.s.a_30d",
                depends_on_assets=("stg_x",),
            )
        )
    )
    assert registry.state.assets["a_30d"].depends_on == ()
    assert registry.state.assets["a_30d"].status is AssetStatus.ACTIVE


def test_spec_versions_must_increase(registry):
    with pytest.raises(RegistryError, match="not after 1"):
        registry.append(ev(SpecResolved(spec=make_spec("a_30d"))))
    registry.append(
        ev(SpecResolved(spec=make_spec("a_30d").model_copy(update={"spec_version": 2})))
    )


def test_concurrent_writers_never_poison_the_log(tmp_path):
    path = tmp_path / "shared.sqlite"
    first = Registry(SqliteEventStore(path))
    second = Registry(SqliteEventStore(path))
    first.append(
        ev(AssetRequested(asset_id="a_30d", consumer="app:a", request_text="t"), actor="app:a")
    )
    # `second` is stale; its append must land after catching up, not at the stale revision.
    seq = second.append(ev(SpecResolved(spec=make_spec("a_30d"))))
    assert seq == 2
    assert first.state.assets["a_30d"].spec is not None
    with pytest.raises(RevisionConflictError):
        first.append(
            ev(
                QuestionOpened(
                    question_id="q", asset_id="a_30d", question="?", alternatives=("x", "y")
                )
            ),
            expected_revision=1,
        )
    third = Registry(first.store)
    assert third.revision == 2
    first.close()
    second.close()


def test_bad_sqlite_file_is_a_registry_error(tmp_path):
    bad = tmp_path / "bad.sqlite"
    bad.write_text("not a database")
    with pytest.raises(RegistryError, match="not a usable registry"):
        SqliteEventStore(bad)
