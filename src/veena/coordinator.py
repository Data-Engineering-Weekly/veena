"""The coordinator: propose, implement, verify, deploy, with the revision guard.

A proposal is `direct` when it builds one asset from sources or from an existing provider, and
`share` when it creates a shared intermediate and rewires dependents behind their interfaces.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from veena.calendar import window_bounds
from veena.errors import CoordinationError, PolicyError, RevisionConflictError
from veena.overlap import derive_shared, find_overlap, shared_asset_id
from veena.planner.compile import (
    compile_from_provider,
    compile_plan,
    dependencies,
    implementation_id,
    refresh_rule,
)
from veena.planner.plan import Implementation, Plan, SourceScan, TimeAttribution
from veena.policy import ReorganizationPolicy
from veena.registry.events import (
    AssetRequested,
    BackfillProgressed,
    DeploymentActivated,
    DeploymentRolledBack,
    Event,
    ImplementationPlanned,
    ImplementationRefreshed,
    InterfaceSwitched,
    Payload,
    ProposalApproved,
    ProposalOpened,
    ProposalRejected,
    QuestionOpened,
    SpecResolved,
    VerificationRecorded,
)
from veena.registry.state import AssetStatus, ProposalStatus
from veena.spec.model import TimeBucket
from veena.verification.checks import VerificationOutcome, equivalence, verify

if TYPE_CHECKING:
    from veena.backends.protocol import Backend
    from veena.registry.registry import Registry
    from veena.registry.state import Asset, Proposal
    from veena.semantic.model import SemanticModel
    from veena.spec.model import AssetSpec

Planner = Callable[["AssetSpec", "SemanticModel", int], Plan]
ChangeKind = Literal["direct", "share", "repair", "consolidate"]


def default_planner(spec: AssetSpec, semantic: SemanticModel, attempt: int) -> Plan:
    """The deterministic compiler. It ignores the attempt number because it has one answer."""
    del attempt
    return compile_plan(spec, semantic)


class ProposalHandle(BaseModel):
    """What the coordinator knows about one proposal in flight.

    `assets` lists every affected asset with the shared asset first, so dependents are built
    after what they read. `providers` maps a dependent to the asset its plan scans.
    """

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    kind_of_change: ChangeKind
    as_of: datetime
    assumed_revision: int
    assets: tuple[str, ...]
    shared_asset: str | None = None
    providers: dict[str, str] = Field(default_factory=dict)
    commitments: dict[str, tuple[str, ...]]
    implementations: dict[str, str] = Field(default_factory=dict)
    verifications: dict[str, str] = Field(default_factory=dict)
    outcomes: dict[str, VerificationOutcome] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Whether every affected asset has a passing verification."""
        return all(a in self.outcomes and self.outcomes[a].passed for a in self.assets)

    @property
    def primary(self) -> str:
        """The asset whose request opened this proposal."""
        return self.assets[-1]


class ActivationOutcome(BaseModel):
    """How an activation run ended."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    proposal_id: str | None
    kind_of_change: ChangeKind | None
    activated: bool
    attempts: int
    pending_approval: bool = False
    deployment_id: str | None = None
    question_id: str | None = None
    outcomes: dict[str, VerificationOutcome] = Field(default_factory=dict)

    @property
    def last_outcome(self) -> VerificationOutcome | None:
        """The verification of the requested asset, if one ran."""
        return self.outcomes.get(self.asset_id)


class Coordinator:
    """Runs the change loop against one registry, one backend, and one semantic model."""

    def __init__(
        self,
        registry: Registry,
        backend: Backend,
        semantic: SemanticModel,
        *,
        planner: Planner = default_planner,
        repair_bound: int = 2,
        policy: ReorganizationPolicy | None = None,
        actor: str = "engine",
        authority: str = "policy:routine_activation@1",
    ) -> None:
        """Bind what the loop needs.

        Args:
            registry: Where every step is recorded.
            backend: Renders, materializes, publishes, measures, and answers verification queries.
            semantic: Approved definitions for planning and verification.
            planner: Produces a plan per attempt for source-fed assets. Tests inject a naive one.
            repair_bound: Maximum verification attempts before escalating with a question.
            policy: When the engine may reorganize. Defaults to `ReorganizationPolicy()`.
            actor: Recorded on every event the coordinator writes.
            authority: Recorded on every event the coordinator writes.
        """
        self._registry = registry
        self._backend = backend
        self._semantic = semantic
        self._planner = planner
        self._repair_bound = max(1, repair_bound)
        self._policy = policy or ReorganizationPolicy()
        self._actor = actor
        self._authority = authority

    # ---- entry points -------------------------------------------------------------------

    def activate(self, asset_id: str, *, as_of: datetime) -> ActivationOutcome:
        """Decide how to build the asset, then run the loop unless a person must approve first.

        Raises:
            PolicyError: The workspace runs in shadow autonomy; proposals are recorded only.
        """
        proposed = self.propose(asset_id, as_of=as_of)
        if self._policy.autonomy == "shadow":
            proposal_id = proposed if isinstance(proposed, str) else proposed.proposal_id
            msg = (
                f"{self._policy.ref} sets autonomy to shadow: recorded {proposal_id} and will not "
                "implement, verify, or deploy"
            )
            raise PolicyError(msg)
        if isinstance(proposed, str):
            return ActivationOutcome(
                asset_id=asset_id,
                proposal_id=None,
                kind_of_change=None,
                activated=False,
                attempts=0,
                question_id=proposed,
            )
        status = self._registry.state.proposals[proposed.proposal_id].status
        if status is ProposalStatus.OPEN:
            return ActivationOutcome(
                asset_id=asset_id,
                proposal_id=proposed.proposal_id,
                kind_of_change=proposed.kind_of_change,
                activated=False,
                attempts=0,
                pending_approval=True,
            )
        return self.run(proposed)

    def run(self, handle: ProposalHandle) -> ActivationOutcome:
        """Implement, verify, and deploy, re-planning up to the repair bound."""
        for attempt in range(1, self._repair_bound + 1):
            handle = self.implement(handle, attempt=attempt)
            handle = self.verify(handle)
            if handle.passed:
                deployment_id = self.deploy(handle)
                return ActivationOutcome(
                    asset_id=handle.primary,
                    proposal_id=handle.proposal_id,
                    kind_of_change=handle.kind_of_change,
                    activated=True,
                    attempts=attempt,
                    deployment_id=deployment_id,
                    outcomes=handle.outcomes,
                )
        question_id = self._escalate(handle)
        return ActivationOutcome(
            asset_id=handle.primary,
            proposal_id=handle.proposal_id,
            kind_of_change=handle.kind_of_change,
            activated=False,
            attempts=self._repair_bound,
            question_id=question_id,
            outcomes=handle.outcomes,
        )

    def approve(self, proposal_id: str, *, as_of: datetime, reason: str = "") -> ActivationOutcome:
        """Record approval of an open proposal and run it.

        Raises:
            PolicyError: The workspace runs in shadow autonomy.
        """
        if self._policy.autonomy == "shadow":
            msg = f"{self._policy.ref} sets autonomy to shadow: nothing is activated"
            raise PolicyError(msg)
        proposal = self._proposal(proposal_id)
        if proposal.status is not ProposalStatus.OPEN:
            msg = f"proposal {proposal_id} is {proposal.status}, not open"
            raise CoordinationError(msg)
        self._append(
            ProposalApproved(proposal_id=proposal_id, reason=reason),
            causes=(f"proposal:{proposal_id}",),
        )
        return self.run(self.resume(proposal_id, as_of=as_of))

    def reject(self, proposal_id: str, *, reason: str) -> None:
        """Record that an open or approved proposal will not proceed.

        Raises:
            CoordinationError: The proposal is already activated or rejected.
        """
        proposal = self._proposal(proposal_id)
        if proposal.status in {ProposalStatus.ACTIVATED, ProposalStatus.REJECTED}:
            msg = f"proposal {proposal_id} is {proposal.status}"
            raise CoordinationError(msg)
        self._append(
            ProposalRejected(proposal_id=proposal_id, reason=reason),
            causes=(f"proposal:{proposal_id}",),
        )

    def resume(self, proposal_id: str, *, as_of: datetime) -> ProposalHandle:
        """Rebuild a handle for a recorded proposal so the loop can continue."""
        proposal = self._proposal(proposal_id)
        state = self._registry.state
        providers: dict[str, str] = {}
        if proposal.shared_asset:
            providers = dict.fromkeys(proposal.dependents, proposal.shared_asset)
        return ProposalHandle(
            proposal_id=proposal_id,
            kind_of_change="share" if proposal.shared_asset else "direct",
            as_of=as_of,
            assumed_revision=proposal.assumed_revision,
            assets=proposal.affected_assets,
            shared_asset=proposal.shared_asset,
            providers=providers,
            commitments={
                a: tuple(sorted(state.assets[a].commitments)) for a in proposal.affected_assets
            },
        )

    def rollback(self, deployment_id: str, *, reason: str, as_of: datetime) -> None:
        """Repoint every interface the deployment switched back to what it replaced."""
        state = self._registry.state
        deployment = state.deployments.get(deployment_id)
        if deployment is None:
            msg = f"unknown deployment {deployment_id!r}"
            raise CoordinationError(msg)
        if deployment.rolled_back:
            msg = f"deployment {deployment_id} is already rolled back"
            raise CoordinationError(msg)
        for asset_id, previous in deployment.replaced.items():
            if previous is None:
                self._backend.unpublish(asset_id)
            else:
                self._backend.publish(asset_id, previous)
        consumers = sorted(
            {
                state.commitments[c].consumer
                for a in deployment.activated
                for c in state.assets[a].commitments
            }
        )
        self._append(
            DeploymentRolledBack(
                deployment_id=deployment_id,
                reason=reason,
                affected_consumers=tuple(consumers),
                interval_start=deployment.effective_at,
                interval_end=as_of,
                restored=dict(deployment.replaced),
            ),
            causes=(f"deployment:{deployment_id}",),
        )

    # ---- propose ------------------------------------------------------------------------

    def propose(self, asset_id: str, *, as_of: datetime) -> ProposalHandle | str:
        """Open a proposal for the asset: from a provider, as a shared intermediate, or direct.

        Returns:
            A handle, or a question id when the engine cannot proceed without a person.

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
        spec = asset.spec
        active = [
            a
            for a in state.assets.values()
            if a.status is AssetStatus.ACTIVE and a.spec is not None and a.asset_id != asset_id
        ]
        report = find_overlap(spec, [a.spec for a in active if a.spec is not None])

        provider = report.provider
        if provider is not None:
            provider_asset = next(a for a in active if a.spec and a.spec.ref == provider.provider)
            return self._open_direct(
                spec, as_of, alternatives=report.considered, provider=provider_asset
            )

        for other in active:
            if other.spec is None or other.spec.grain.time_bucket is not TimeBucket.NONE:
                continue
            outcome = self._try_share(spec, other, as_of, alternatives=report.considered)
            if outcome is not None:
                return outcome
        return self._open_direct(spec, as_of, alternatives=report.considered)

    def _open_direct(
        self,
        spec: AssetSpec,
        as_of: datetime,
        *,
        alternatives: tuple[str, ...],
        provider: Asset | None = None,
    ) -> ProposalHandle:
        proposal_id = self._next_id(spec.asset_id, "p")
        revision = self._registry.revision
        if provider is not None and provider.spec is not None and provider.active_implementation:
            summary = f"implement {spec.ref} from {provider.spec.ref}"
            providers = {spec.asset_id: provider.asset_id}
        else:
            summary = f"implement {spec.ref} directly from approved sources"
            providers = {}
        self._append(
            ProposalOpened(
                proposal_id=proposal_id,
                summary=summary,
                changes=(f"create implementation of {spec.ref}",),
                affected_assets=(spec.asset_id,),
                assumed_revision=revision,
                kind_of_change="direct",
                alternatives=alternatives,
                evidence_required=(f"verification of {spec.asset_id}",),
            ),
            causes=(f"asset:{spec.asset_id}",),
        )
        self._append(
            ProposalApproved(proposal_id=proposal_id, reason="direct build under routine policy"),
            causes=(f"proposal:{proposal_id}",),
        )
        return ProposalHandle(
            proposal_id=proposal_id,
            kind_of_change="direct",
            as_of=as_of,
            assumed_revision=revision,
            assets=(spec.asset_id,),
            providers=providers,
            commitments={spec.asset_id: self._commitments(spec.asset_id)},
        )

    def _try_share(
        self, spec: AssetSpec, other: Asset, as_of: datetime, *, alternatives: tuple[str, ...]
    ) -> ProposalHandle | str | None:
        if other.spec is None or other.active_implementation is None:
            return None
        shared_id = shared_asset_id(other.spec, spec)
        proposal_id = self._next_id(shared_id, "p")
        derived = derive_shared(
            other.spec, spec, asset_id=shared_id, origin=f"proposal:{proposal_id}"
        )
        if derived is None:
            return None

        # History must exist before anything is built (R3.8).
        shared_plan = compile_plan(derived, self._semantic)
        scan = shared_plan.step(SourceScan)
        attribution = shared_plan.step(TimeAttribution)
        bounds = self._backend.source_bounds(scan.table, attribution.timestamp_column)
        start, _ = window_bounds(as_of, derived.window.span_days, derived.window.timezone)
        if bounds is None or bounds[0] > start:
            oldest = bounds[0].isoformat() if bounds else "none"
            return self._question(
                spec.asset_id,
                f"{spec.ref} needs {derived.window.span_days} days of {scan.source} history "
                f"from {start.date().isoformat()}, but the oldest record is {oldest}. "
                "How should the engine proceed?",
                ("accept_shorter_history", "load_more_history", "abandon_request"),
                causes=(f"asset:{spec.asset_id}",),
            )

        # Cost is measured on the backend, never estimated. The shared candidate is built once
        # so the dependents' windows can be counted against real rows.
        shared_impl_id = implementation_id(shared_id, 1, 1)
        shared_sql, _ = self._backend.render(shared_plan, as_of=as_of)
        self._backend.materialize(shared_impl_id, shared_sql, shared_plan)
        current_rows = self._backend.measure_rows_processed(
            self._registry.state.implementations[other.active_implementation].plan, as_of=as_of
        ) + self._backend.measure_rows_processed(compile_plan(spec, self._semantic), as_of=as_of)
        proposed_rows = self._backend.measure_rows_processed(shared_plan, as_of=as_of)
        for dependent in (other.spec, spec):
            proposed_rows += self._backend.measure_rows_processed(
                compile_from_provider(dependent, derived, shared_impl_id, self._semantic),
                as_of=as_of,
            )
        benefit = current_rows - proposed_rows

        revision = self._registry.revision
        affected = (shared_id, other.asset_id, spec.asset_id)
        self._ensure_asset(derived, proposal_id)
        self._append(
            ProposalOpened(
                proposal_id=proposal_id,
                summary=(f"create {derived.ref} and serve {other.spec.ref} and {spec.ref} from it"),
                changes=(
                    f"create {derived.ref} with {derived.window.span_days} days of history",
                    f"rewire {other.spec.ref} to read {shared_id}",
                    f"create {spec.ref} reading {shared_id}",
                ),
                affected_assets=affected,
                assumed_revision=revision,
                kind_of_change="share",
                alternatives=alternatives,
                cost_current_rows=current_rows,
                cost_proposed_rows=proposed_rows,
                evidence_required=(
                    *(f"verification of {a}" for a in affected),
                    f"equivalence of {other.asset_id} at matching inputs",
                ),
                shared_asset=shared_id,
                dependents=(other.asset_id, spec.asset_id),
            ),
            causes=(f"asset:{spec.asset_id}", f"asset:{other.asset_id}"),
        )

        rejection = self._policy_rejection(other.asset_id, benefit, as_of)
        if rejection is not None:
            self._append(
                ProposalRejected(proposal_id=proposal_id, reason=rejection),
                causes=(f"proposal:{proposal_id}",),
            )
            return None
        if not self._policy.require_approval:
            self._append(
                ProposalApproved(
                    proposal_id=proposal_id,
                    reason=f"benefit {benefit} rows >= minimum {self._policy.min_benefit_rows} "
                    f"under {self._policy.ref}",
                ),
                causes=(f"proposal:{proposal_id}",),
            )
        return ProposalHandle(
            proposal_id=proposal_id,
            kind_of_change="share",
            as_of=as_of,
            assumed_revision=revision,
            assets=affected,
            shared_asset=shared_id,
            providers={other.asset_id: shared_id, spec.asset_id: shared_id},
            commitments={a: self._commitments(a) for a in affected},
        )

    def _policy_rejection(self, rewired_asset: str, benefit: int, as_of: datetime) -> str | None:
        if benefit < self._policy.min_benefit_rows:
            return (
                f"expected benefit {benefit} rows per refresh is below the minimum "
                f"{self._policy.min_benefit_rows} in {self._policy.ref}"
            )
        since = as_of - timedelta(days=self._policy.period_days)
        state = self._registry.state
        recent = sum(
            1
            for d in state.deployments.values()
            if rewired_asset in d.activated
            and d.effective_at >= since
            and state.proposals[d.proposal_id].kind_of_change == "share"
        )
        if recent >= self._policy.max_reorganizations_per_period:
            return (
                f"{rewired_asset} was reorganized {recent} time(s) in the last "
                f"{self._policy.period_days} days; the cap is "
                f"{self._policy.max_reorganizations_per_period} in {self._policy.ref}"
            )
        return None

    def _ensure_asset(self, derived: AssetSpec, proposal_id: str) -> None:
        if derived.asset_id in self._registry.state.assets:
            return
        self._append(
            AssetRequested(
                asset_id=derived.asset_id,
                consumer=self._actor,
                request_text=f"shared intermediate derived by {proposal_id}",
            ),
            causes=(f"proposal:{proposal_id}",),
        )
        self._append(SpecResolved(spec=derived), causes=(f"proposal:{proposal_id}",))

    # ---- implement, verify, deploy -----------------------------------------------------

    def implement(self, handle: ProposalHandle, *, attempt: int = 1) -> ProposalHandle:
        """Plan, render, record, and materialize one implementation per affected asset."""
        implementations = dict(handle.implementations)
        for asset_id in handle.assets:
            asset = self._registry.state.assets[asset_id]
            spec = asset.spec
            if spec is None:
                msg = f"asset {asset_id!r} lost its specification"
                raise CoordinationError(msg)
            provider_id = handle.providers.get(asset_id)
            if provider_id is None:
                plan = self._planner(spec, self._semantic, attempt)
            else:
                provider_spec = self._registry.state.assets[provider_id].spec
                provider_impl = (
                    implementations.get(provider_id)
                    or self._registry.state.assets[provider_id].active_implementation
                )
                if provider_spec is None or provider_impl is None:
                    msg = f"provider {provider_id!r} has no implementation to read"
                    raise CoordinationError(msg)
                plan = compile_from_provider(spec, provider_spec, provider_impl, self._semantic)
            sql, drafted = self._backend.render(plan, as_of=handle.as_of)
            impl_id = implementation_id(
                spec.asset_id, spec.spec_version, len(asset.implementations) + 1
            )
            self._append(
                ImplementationPlanned(
                    proposal_id=handle.proposal_id,
                    implementation=Implementation(
                        implementation_id=impl_id,
                        asset_id=spec.asset_id,
                        spec_version=spec.spec_version,
                        plan=plan,
                        backend=self._backend.name,
                        sql=sql,
                        dependencies=dependencies(plan),
                        refresh_rule=refresh_rule(spec),
                        drafted_steps=drafted,
                    ),
                ),
                causes=(f"proposal:{handle.proposal_id}",),
            )
            progress = self._backend.materialize(impl_id, sql, plan)
            if asset_id == handle.shared_asset:
                self._record_backfill(impl_id, plan, spec, handle.as_of, progress.positions)
            self._append(
                ImplementationRefreshed(
                    implementation_id=impl_id,
                    as_of=handle.as_of,
                    row_count=progress.row_count,
                    source_progress=progress.positions,
                ),
                causes=(f"implementation:{impl_id}",),
            )
            implementations[asset_id] = impl_id
        return handle.model_copy(
            update={"implementations": implementations, "verifications": {}, "outcomes": {}}
        )

    def _record_backfill(
        self,
        impl_id: str,
        plan: Plan,
        spec: AssetSpec,
        as_of: datetime,
        positions: dict[str, datetime],
    ) -> None:
        scan = plan.step(SourceScan)
        start, _ = window_bounds(as_of, spec.window.span_days, spec.window.timezone)
        reached = positions.get(scan.source)
        if reached is None:
            return
        rows = self._backend.execute(f"SELECT count(*) FROM {impl_id}")
        self._append(
            BackfillProgressed(
                implementation_id=impl_id,
                source=scan.source,
                position_from=start,
                position_to=reached,
                rows=int(str(rows[0][0])) if rows else 0,
            ),
            causes=(f"implementation:{impl_id}",),
        )

    def verify(self, handle: ProposalHandle) -> ProposalHandle:
        """Run verification for every affected asset and record the evidence."""
        state = self._registry.state
        verifications = dict(handle.verifications)
        outcomes = dict(handle.outcomes)
        for asset_id in handle.assets:
            impl_id = handle.implementations.get(asset_id)
            asset = state.assets[asset_id]
            if impl_id is None or asset.spec is None:
                msg = f"proposal {handle.proposal_id} has no implementation of {asset_id} to verify"
                raise CoordinationError(msg)
            outcome = verify(
                asset.spec, self._semantic, impl_id, self._backend.execute, as_of=handle.as_of
            )
            checks = list(outcome.checks)
            previous = asset.active_implementation
            if previous is not None and previous != impl_id:
                checks.append(
                    equivalence(
                        previous,
                        impl_id,
                        tuple(self._key_columns(asset.spec)),
                        tuple(m.name for m in asset.spec.measures),
                        self._backend.execute,
                    )
                )
            uncertainty = outcome.uncertainty
            provider_id = handle.providers.get(asset_id)
            if not outcome.passed and provider_id is not None:
                provider_outcome = outcomes.get(provider_id)
                if provider_outcome is not None and not provider_outcome.passed:
                    cause = f"dependency {provider_id} failed its own verification"
                else:
                    cause = f"suspected cause: dependency {provider_id} or the rollup from it"
                checks = [
                    c.model_copy(update={"detail": f"{c.detail}; {cause}"}) if not c.passed else c
                    for c in checks
                ]
                uncertainty = f"{uncertainty}; {cause}" if uncertainty else cause
            outcome = outcome.model_copy(
                update={
                    "checks": tuple(checks),
                    "passed": all(c.passed for c in checks),
                    "uncertainty": uncertainty,
                }
            )
            count = sum(
                1
                for v in self._registry.state.verifications.values()
                if v.proposal_id == handle.proposal_id
            )
            verification_id = f"{handle.proposal_id}_v{count + 1}"
            self._append(
                VerificationRecorded(
                    verification_id=verification_id,
                    proposal_id=handle.proposal_id,
                    implementation_id=impl_id,
                    as_of=handle.as_of,
                    grade=outcome.grade,
                    passed=outcome.passed,
                    checks=outcome.checks,
                    source_progress=state.assets[asset_id].source_progress,
                    uncertainty=outcome.uncertainty,
                ),
                causes=(f"implementation:{impl_id}",),
            )
            verifications[asset_id] = verification_id
            outcomes[asset_id] = outcome
        return handle.model_copy(update={"verifications": verifications, "outcomes": outcomes})

    def deploy(self, handle: ProposalHandle) -> str:
        """Switch every affected interface and record one deployment, guarded by commitments.

        Raises:
            CoordinationError: Verification is missing or failed for an affected asset.
            RevisionConflictError: A commitment changed on an affected asset since the proposal.
        """
        if not handle.passed:
            missing = [a for a in handle.assets if a not in handle.outcomes]
            failed = [
                a for a in handle.assets if a in handle.outcomes and not handle.outcomes[a].passed
            ]
            msg = (
                f"proposal {handle.proposal_id} is not verified: missing {missing}, failed {failed}"
            )
            raise CoordinationError(msg)
        state = self._registry.state
        for asset_id in handle.assets:
            if self._commitments(asset_id) != handle.commitments.get(asset_id, ()):
                raise RevisionConflictError(handle.assumed_revision, self._registry.revision)
        replaced = {a: state.assets[a].active_implementation for a in handle.assets}
        switches = {a: handle.implementations[a] for a in handle.assets}
        deployment_id = self._next_id(handle.shared_asset or handle.primary, "d")
        staged = not self._backend.atomic_publish
        if staged:
            for asset_id, impl_id in switches.items():
                self._backend.publish(asset_id, impl_id)
                self._append(
                    InterfaceSwitched(
                        proposal_id=handle.proposal_id, asset_id=asset_id, implementation_id=impl_id
                    ),
                    causes=(f"proposal:{handle.proposal_id}",),
                )
        else:
            self._backend.publish_many(switches)
        self._append(
            DeploymentActivated(
                deployment_id=deployment_id,
                proposal_id=handle.proposal_id,
                verification_id=handle.verifications[handle.primary],
                assumed_revision=handle.assumed_revision,
                activated=switches,
                replaced=replaced,
                staged=staged,
            ),
            causes=tuple(f"verification:{v}" for v in handle.verifications.values()),
            expected_revision=self._registry.revision,
        )
        return deployment_id

    # ---- helpers ------------------------------------------------------------------------

    def _escalate(self, handle: ProposalHandle) -> str:
        failed = [
            f"{a}/{c.name}: {c.detail}"
            for a, o in handle.outcomes.items()
            for c in o.checks
            if not c.passed
        ]
        summary = "; ".join(failed) or "no verification ran"
        return self._question(
            handle.primary,
            f"{self._repair_bound} implementation attempt(s) failed verification for "
            f"{handle.proposal_id}. Last failure: {summary}. How should the engine proceed?",
            ("revise_definitions", "allow_more_attempts", "abandon_request"),
            causes=(f"proposal:{handle.proposal_id}",),
        )

    def _question(
        self,
        asset_id: str,
        question: str,
        alternatives: tuple[str, ...],
        *,
        causes: tuple[str, ...],
    ) -> str:
        question_id = self._next_id(asset_id, "q")
        self._append(
            QuestionOpened(
                question_id=question_id,
                asset_id=asset_id,
                question=question,
                alternatives=alternatives,
            ),
            causes=causes,
        )
        return question_id

    def _proposal(self, proposal_id: str) -> Proposal:
        proposal = self._registry.state.proposals.get(proposal_id)
        if proposal is None:
            msg = f"unknown proposal {proposal_id!r}"
            raise CoordinationError(msg)
        return proposal

    def _commitments(self, asset_id: str) -> tuple[str, ...]:
        asset = self._registry.state.assets.get(asset_id)
        return tuple(sorted(asset.commitments)) if asset else ()

    def _next_id(self, stem: str, kind: str) -> str:
        state = self._registry.state
        existing: set[str] = set()
        if kind == "p":
            existing = set(state.proposals)
        elif kind == "d":
            existing = set(state.deployments)
        elif kind == "q":
            existing = set(state.questions)
        n = 1
        while f"{stem}_{kind}{n}" in existing:
            n += 1
        return f"{stem}_{kind}{n}"

    def _key_columns(self, spec: AssetSpec) -> list[str]:
        keys = [self._semantic.entity(k).key for k in spec.grain.keys]
        if spec.grain.time_bucket is TimeBucket.DAY:
            keys.append("day")
        elif spec.grain.time_bucket is TimeBucket.MONTH:
            keys.append("month")
        return keys

    def _append(
        self, payload: Payload, *, causes: tuple[str, ...], expected_revision: int | None = None
    ) -> int:
        return self._registry.append(
            Event(actor=self._actor, authority=self._authority, causes=causes, payload=payload),
            expected_revision=expected_revision,
        )
