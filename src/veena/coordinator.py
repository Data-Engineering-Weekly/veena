"""The coordinator: propose, implement, verify, deploy, with the revision guard."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veena.errors import CoordinationError, RevisionConflictError
from veena.planner.compile import compile_plan, dependencies, implementation_id, refresh_rule
from veena.planner.plan import Implementation
from veena.registry.events import (
    DeploymentActivated,
    Event,
    ImplementationPlanned,
    ImplementationRefreshed,
    ProposalOpened,
    QuestionOpened,
    VerificationRecorded,
)
from veena.registry.state import AssetStatus
from veena.verification.checks import VerificationOutcome, verify

if TYPE_CHECKING:
    from veena.backends.protocol import Backend
    from veena.planner.plan import Plan
    from veena.registry.registry import Registry
    from veena.semantic.model import SemanticModel
    from veena.spec.model import AssetSpec

Planner = Callable[["AssetSpec", "SemanticModel", int], "Plan"]


def default_planner(spec: AssetSpec, semantic: SemanticModel, attempt: int) -> Plan:
    """The deterministic compiler. It ignores the attempt number because it has one answer."""
    del attempt
    return compile_plan(spec, semantic)


class ProposalHandle(BaseModel):
    """What the coordinator knows about one proposal in flight."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    asset_id: str
    as_of: datetime
    assumed_revision: int
    commitments: tuple[str, ...]
    implementation_id: str | None = None
    verification_id: str | None = None
    outcome: VerificationOutcome | None = None


class ActivationOutcome(BaseModel):
    """How an activation run ended."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    activated: bool
    attempts: int
    deployment_id: str | None = None
    question_id: str | None = None
    last_outcome: VerificationOutcome | None = None


class Coordinator:
    """Runs the change loop for one asset at a time against one backend."""

    def __init__(
        self,
        registry: Registry,
        backend: Backend,
        semantic: SemanticModel,
        *,
        planner: Planner = default_planner,
        repair_bound: int = 2,
        actor: str = "engine",
        authority: str = "policy:routine_activation@1",
    ) -> None:
        """Bind the registry, backend, and semantic model the loop will use.

        Args:
            registry: Where every step is recorded.
            backend: Renders, materializes, publishes, and answers verification queries.
            semantic: Approved definitions for planning and verification.
            planner: Produces a plan per attempt. Tests inject a deliberately naive one.
            repair_bound: Maximum verification attempts before escalating with a question.
            actor: Recorded on every event the coordinator writes.
            authority: Recorded on every event the coordinator writes.
        """
        self._registry = registry
        self._backend = backend
        self._semantic = semantic
        self._planner = planner
        self._repair_bound = max(1, repair_bound)
        self._actor = actor
        self._authority = authority

    def activate(self, asset_id: str, *, as_of: datetime) -> ActivationOutcome:
        """Propose, implement, verify, and deploy, re-planning up to the repair bound.

        Returns:
            The deployment id on success, or the question id after escalation.
        """
        handle = self.propose(asset_id, as_of=as_of)
        last: VerificationOutcome | None = None
        for attempt in range(1, self._repair_bound + 1):
            handle = self.implement(handle, attempt=attempt)
            handle = self.verify(handle)
            last = handle.outcome
            if last is not None and last.passed:
                deployment_id = self.deploy(handle)
                return ActivationOutcome(
                    asset_id=asset_id,
                    activated=True,
                    attempts=attempt,
                    deployment_id=deployment_id,
                    last_outcome=last,
                )
        question_id = self._escalate(handle, last)
        return ActivationOutcome(
            asset_id=asset_id,
            activated=False,
            attempts=self._repair_bound,
            question_id=question_id,
            last_outcome=last,
        )

    def propose(self, asset_id: str, *, as_of: datetime) -> ProposalHandle:
        """Open a proposal to create or revise the asset's implementation.

        Raises:
            CoordinationError: The asset has no resolved spec or is retired.
        """
        state = self._registry.state
        asset = state.assets.get(asset_id)
        if asset is None or asset.spec is None:
            msg = f"asset {asset_id!r} has no resolved specification"
            raise CoordinationError(msg)
        if asset.status is AssetStatus.RETIRED:
            msg = f"asset {asset_id!r} is retired"
            raise CoordinationError(msg)
        ordinal = sum(1 for p in state.proposals.values() if asset_id in p.affected_assets) + 1
        proposal_id = f"{asset_id}:p{ordinal}"
        revision = self._registry.revision
        self._append(
            ProposalOpened(
                proposal_id=proposal_id,
                summary=f"implement {asset.spec.ref} directly from approved sources",
                changes=(f"create implementation of {asset.spec.ref}",),
                affected_assets=(asset_id,),
                assumed_revision=revision,
            ),
            causes=(f"asset:{asset_id}",),
        )
        return ProposalHandle(
            proposal_id=proposal_id,
            asset_id=asset_id,
            as_of=as_of,
            assumed_revision=revision,
            commitments=tuple(sorted(asset.commitments)),
        )

    def implement(self, handle: ProposalHandle, *, attempt: int = 1) -> ProposalHandle:
        """Plan, render, record, and materialize one implementation for the proposal."""
        asset = self._registry.state.assets[handle.asset_id]
        spec = asset.spec
        if spec is None:
            msg = f"asset {handle.asset_id!r} lost its specification"
            raise CoordinationError(msg)
        plan = self._planner(spec, self._semantic, attempt)
        sql, drafted = self._backend.render(plan, as_of=handle.as_of)
        impl_id = implementation_id(
            spec.asset_id, spec.spec_version, len(asset.implementations) + 1
        )
        implementation = self._implementation(impl_id, plan, sql, drafted, spec)
        self._append(
            ImplementationPlanned(proposal_id=handle.proposal_id, implementation=implementation),
            causes=(f"proposal:{handle.proposal_id}",),
        )
        progress = self._backend.materialize(impl_id, sql, plan)
        self._append(
            ImplementationRefreshed(
                implementation_id=impl_id,
                as_of=handle.as_of,
                row_count=progress.row_count,
                source_progress=progress.positions,
            ),
            causes=(f"implementation:{impl_id}",),
        )
        return handle.model_copy(update={"implementation_id": impl_id})

    def verify(self, handle: ProposalHandle) -> ProposalHandle:
        """Run verification against the implementation's table and record the evidence."""
        if handle.implementation_id is None:
            msg = f"proposal {handle.proposal_id} has no implementation to verify"
            raise CoordinationError(msg)
        asset = self._registry.state.assets[handle.asset_id]
        if asset.spec is None:
            msg = f"asset {handle.asset_id!r} lost its specification"
            raise CoordinationError(msg)
        outcome = verify(
            asset.spec,
            self._semantic,
            handle.implementation_id,
            self._backend.execute,
            as_of=handle.as_of,
        )
        count = sum(
            1
            for v in self._registry.state.verifications.values()
            if v.proposal_id == handle.proposal_id
        )
        verification_id = f"{handle.proposal_id}:v{count + 1}"
        self._append(
            VerificationRecorded(
                verification_id=verification_id,
                proposal_id=handle.proposal_id,
                implementation_id=handle.implementation_id,
                as_of=handle.as_of,
                grade=outcome.grade,
                passed=outcome.passed,
                checks=outcome.checks,
                source_progress=asset.source_progress,
                uncertainty=outcome.uncertainty,
            ),
            causes=(f"implementation:{handle.implementation_id}",),
        )
        return handle.model_copy(update={"verification_id": verification_id, "outcome": outcome})

    def deploy(self, handle: ProposalHandle) -> str:
        """Publish the interface and record the deployment, guarded by the assumed revision.

        Raises:
            CoordinationError: No passing verification is attached.
            RevisionConflictError: A commitment changed since the proposal opened.
        """
        if (
            handle.implementation_id is None
            or handle.verification_id is None
            or handle.outcome is None
        ):
            msg = f"proposal {handle.proposal_id} is not verified"
            raise CoordinationError(msg)
        if not handle.outcome.passed:
            msg = f"proposal {handle.proposal_id} failed verification {handle.verification_id}"
            raise CoordinationError(msg)
        state = self._registry.state
        current = tuple(sorted(state.assets[handle.asset_id].commitments))
        if current != handle.commitments:
            raise RevisionConflictError(handle.assumed_revision, self._registry.revision)
        ordinal = sum(1 for d in state.deployments.values() if handle.asset_id in d.activated) + 1
        deployment_id = f"{handle.asset_id}:d{ordinal}"
        revision = self._registry.revision
        self._backend.publish(handle.asset_id, handle.implementation_id)
        self._append(
            DeploymentActivated(
                deployment_id=deployment_id,
                proposal_id=handle.proposal_id,
                verification_id=handle.verification_id,
                assumed_revision=handle.assumed_revision,
                activated={handle.asset_id: handle.implementation_id},
            ),
            causes=(f"verification:{handle.verification_id}",),
            expected_revision=revision,
        )
        return deployment_id

    def _escalate(self, handle: ProposalHandle, last: VerificationOutcome | None) -> str:
        failed = [c for c in (last.checks if last else ()) if not c.passed]
        summary = "; ".join(f"{c.name}: {c.detail}" for c in failed) or "no verification ran"
        count = sum(
            1 for q in self._registry.state.questions.values() if q.asset_id == handle.asset_id
        )
        question_id = f"{handle.asset_id}:q{count + 1}"
        self._append(
            QuestionOpened(
                question_id=question_id,
                asset_id=handle.asset_id,
                question=(
                    f"{self._repair_bound} implementation attempt(s) failed verification for "
                    f"{handle.asset_id}. Last failure: {summary}. How should the engine proceed?"
                ),
                alternatives=("revise_definitions", "allow_more_attempts", "abandon_request"),
            ),
            causes=(f"proposal:{handle.proposal_id}",),
        )
        return question_id

    def _implementation(
        self, impl_id: str, plan: Plan, sql: str, drafted: tuple[str, ...], spec: AssetSpec
    ) -> Implementation:
        return Implementation(
            implementation_id=impl_id,
            asset_id=spec.asset_id,
            spec_version=spec.spec_version,
            plan=plan,
            backend=self._backend.name,
            sql=sql,
            dependencies=dependencies(plan),
            refresh_rule=refresh_rule(spec),
            drafted_steps=drafted,
        )

    def _append(
        self,
        payload: ProposalOpened
        | ImplementationPlanned
        | ImplementationRefreshed
        | VerificationRecorded
        | DeploymentActivated
        | QuestionOpened,
        *,
        causes: tuple[str, ...],
        expected_revision: int | None = None,
    ) -> int:
        return self._registry.append(
            Event(actor=self._actor, authority=self._authority, causes=causes, payload=payload),
            expected_revision=expected_revision,
        )
