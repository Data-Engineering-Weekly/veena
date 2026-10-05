"""Projections of the event log into the records people and the engine read.

`RegistryState.apply` is the only writer. It refuses any event that would leave the state
inconsistent, so a stored log always replays. Every rejection is a `RegistryError` that names
the event sequence and the rule.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from veena.errors import RegistryError
from veena.evidence import CheckResult, Grade
from veena.registry.events import (
    AssetObserved,
    AssetRequested,
    AssetRetired,
    BackfillProgressed,
    ClassificationProduced,
    CommitmentProposed,
    CommitmentRegistered,
    CommitmentReleased,
    DecisionRecorded,
    DeploymentActivated,
    DeploymentRolledBack,
    DraftProduced,
    ImplementationPlanned,
    ImplementationRefreshed,
    InterfaceSwitched,
    ProposalApproved,
    ProposalOpened,
    ProposalRejected,
    QuestionOpened,
    RefreshCostObserved,
    RequestRefused,
    SpecResolved,
    UnmanagedReadObserved,
    UsageObserved,
    VerificationRecorded,
)
from veena.spec.model import AssetSpec

if TYPE_CHECKING:
    from veena.planner.plan import Implementation
    from veena.registry.store import StoredEvent


class AssetStatus(StrEnum):
    """Where an asset is in its life."""

    REQUESTED = "requested"
    OBSERVED = "observed"
    SPECIFIED = "specified"
    ACTIVE = "active"
    RETIRED = "retired"


class ProposalStatus(StrEnum):
    """Where a proposal is."""

    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTIVATED = "activated"


class Asset(BaseModel):
    """Current view of one asset."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    status: AssetStatus
    requested_by: str = ""
    request_text: str = ""
    request_seq: int = 0
    refused: str = ""
    observed: AssetObserved | None = None
    spec: AssetSpec | None = None
    active_implementation: str | None = None
    commitments: tuple[str, ...] = ()
    implementations: tuple[str, ...] = ()
    source_progress: dict[str, datetime] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = Field(
        default_factory=tuple, description="Assets the active implementation reads."
    )
    history: tuple[int, ...] = Field(default_factory=tuple, description="seq of every event")


class Commitment(BaseModel):
    """Current view of one commitment."""

    model_config = ConfigDict(frozen=True)

    commitment_id: str
    asset_id: str
    consumer: str
    incorporate_within_seconds: int
    correctness_tolerance: float
    schema_stable: bool
    released: bool = False
    seq: int


class Question(BaseModel):
    """An open or answered question for a person."""

    model_config = ConfigDict(frozen=True)

    question_id: str
    asset_id: str
    question: str
    alternatives: tuple[str, ...]
    slot: str = ""
    consumer: str = ""
    evidence: tuple[str, ...] = ()
    draft: dict[str, str] = Field(default_factory=dict)
    draft_probabilities: dict[str, float] = Field(default_factory=dict)
    decision_id: str | None = None
    seq: int


class Decision(BaseModel):
    """A recorded human choice."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    question_id: str
    selected: str
    rationale: str
    scope: str
    actor: str
    authority: str
    seq: int


class Proposal(BaseModel):
    """A graph change and what has happened to it so far."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    summary: str
    changes: tuple[str, ...]
    affected_assets: tuple[str, ...]
    assumed_revision: int
    kind_of_change: str = "direct"
    status: ProposalStatus = ProposalStatus.OPEN
    reason: str = ""
    alternatives: tuple[str, ...] = ()
    cost_current_rows: int | None = None
    cost_proposed_rows: int | None = None
    evidence_required: tuple[str, ...] = ()
    shared_asset: str | None = None
    dependents: tuple[str, ...] = ()
    providers: dict[str, str] = Field(default_factory=dict)
    assumed_active: dict[str, str | None] = Field(default_factory=dict)
    assumed_commitments: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    implementations: tuple[str, ...] = ()
    verifications: tuple[str, ...] = ()
    switched: dict[str, str] = Field(default_factory=dict)
    deployment_id: str | None = None
    seq: int


class Verification(BaseModel):
    """Evidence recorded for a proposal."""

    model_config = ConfigDict(frozen=True)

    verification_id: str
    proposal_id: str
    implementation_id: str
    grade: Grade
    passed: bool
    checks: tuple[CheckResult, ...]
    source_progress: dict[str, datetime]
    uncertainty: str
    seq: int


class Deployment(BaseModel):
    """What the coordinator activated and whether it still stands."""

    model_config = ConfigDict(frozen=True)

    deployment_id: str
    proposal_id: str
    verification_id: str
    verifications: dict[str, str] = Field(default_factory=dict)
    assumed_revision: int
    activated: dict[str, str]
    replaced: dict[str, str | None] = Field(default_factory=dict)
    staged: bool = False
    rolled_back: bool = False
    rollback_reason: str = ""
    effective_at: datetime
    seq: int


class ModelCall(BaseModel):
    """A recorded classifier or drafter call."""

    model_config = ConfigDict(frozen=True)

    seq: int
    event_payload: ClassificationProduced | DraftProduced


class RegistryState:
    """Mutable projection rebuilt by replaying events in order."""

    def __init__(self) -> None:
        """Start with nothing projected."""
        self.revision = 0
        self.assets: dict[str, Asset] = {}
        self.commitments: dict[str, Commitment] = {}
        self.questions: dict[str, Question] = {}
        self.decisions: dict[str, Decision] = {}
        self.proposals: dict[str, Proposal] = {}
        self.implementations: dict[str, Implementation] = {}
        self.verifications: dict[str, Verification] = {}
        self.deployments: dict[str, Deployment] = {}
        self.classifications: list[ModelCall] = []
        self.drafts: list[ModelCall] = []
        self.usage: dict[tuple[str, str], UsageObserved] = {}
        self.refresh_costs: dict[str, RefreshCostObserved] = {}
        self.unmanaged_reads: dict[tuple[str, str], UnmanagedReadObserved] = {}
        self.commitment_proposals: dict[tuple[str, str], CommitmentProposed] = {}

    def copy(self) -> RegistryState:
        """A shallow copy. Every projected value is frozen, so sharing them is safe."""
        other = RegistryState()
        other.revision = self.revision
        other.assets = dict(self.assets)
        other.commitments = dict(self.commitments)
        other.questions = dict(self.questions)
        other.decisions = dict(self.decisions)
        other.proposals = dict(self.proposals)
        other.implementations = dict(self.implementations)
        other.verifications = dict(self.verifications)
        other.deployments = dict(self.deployments)
        other.classifications = list(self.classifications)
        other.drafts = list(self.drafts)
        other.usage = dict(self.usage)
        other.refresh_costs = dict(self.refresh_costs)
        other.unmanaged_reads = dict(self.unmanaged_reads)
        other.commitment_proposals = dict(self.commitment_proposals)
        return other

    def dependents_of(self, asset_id: str) -> tuple[str, ...]:
        """Assets whose active implementation reads `asset_id`."""
        return tuple(sorted(a.asset_id for a in self.assets.values() if asset_id in a.depends_on))

    def latest_deployment_for(self, asset_id: str) -> Deployment | None:
        """The newest deployment that activated an implementation of the asset."""
        candidates = [
            d for d in self.deployments.values() if asset_id in d.activated and not d.rolled_back
        ]
        return max(candidates, key=lambda d: d.seq) if candidates else None

    def apply(self, stored: StoredEvent) -> None:
        """Fold one stored event into the projection.

        Raises:
            RegistryError: The event refers to a missing record, duplicates an id, or breaks a
                rule the projection enforces.
        """
        if stored.seq <= self.revision:
            msg = f"event seq {stored.seq} is not after revision {self.revision}"
            raise RegistryError(msg)
        event = stored.event
        seq = stored.seq
        payload = event.payload
        match payload:
            case AssetRequested():
                asset = self.assets.get(payload.asset_id)
                if asset is None:
                    asset = Asset(asset_id=payload.asset_id, status=AssetStatus.REQUESTED)
                self.assets[payload.asset_id] = _touch(
                    asset.model_copy(
                        update={
                            "requested_by": payload.consumer,
                            "request_text": payload.request_text,
                            "request_seq": seq,
                        }
                    ),
                    seq,
                )
            case RequestRefused():
                asset = self._asset(payload.asset_id, seq)
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(update={"refused": payload.reason}), seq
                )
            case AssetObserved():
                asset = self.assets.get(payload.asset_id)
                if asset is None:
                    asset = Asset(asset_id=payload.asset_id, status=AssetStatus.OBSERVED)
                status = asset.status
                if status is AssetStatus.REQUESTED:
                    status = AssetStatus.OBSERVED
                update: dict[str, object] = {"observed": payload, "status": status}
                if asset.active_implementation is None:
                    update["depends_on"] = payload.depends_on_assets
                self.assets[payload.asset_id] = _touch(asset.model_copy(update=update), seq)
            case SpecResolved():
                asset = self._asset(payload.spec.asset_id, seq)
                if asset.spec is not None and payload.spec.spec_version <= asset.spec.spec_version:
                    msg = (
                        f"event {seq}: spec version {payload.spec.spec_version} for "
                        f"{asset.asset_id} is not after {asset.spec.spec_version}"
                    )
                    raise RegistryError(msg)
                status = asset.status
                if status in {AssetStatus.REQUESTED, AssetStatus.OBSERVED, AssetStatus.SPECIFIED}:
                    status = AssetStatus.SPECIFIED
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(update={"spec": payload.spec, "status": status}), seq
                )
            case CommitmentRegistered():
                asset = self._asset(payload.asset_id, seq)
                self._not_retired(asset, seq, "register a commitment on")
                self._fresh(self.commitments, payload.commitment_id, "commitment", seq)
                self.commitments[payload.commitment_id] = Commitment(
                    commitment_id=payload.commitment_id,
                    asset_id=payload.asset_id,
                    consumer=payload.consumer,
                    incorporate_within_seconds=payload.incorporate_within_seconds,
                    correctness_tolerance=payload.correctness_tolerance,
                    schema_stable=payload.schema_stable,
                    seq=seq,
                )
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(
                        update={"commitments": (*asset.commitments, payload.commitment_id)}
                    ),
                    seq,
                )
            case CommitmentReleased():
                commitment = self._require(
                    self.commitments, payload.commitment_id, "commitment", seq
                )
                if commitment.released:
                    msg = f"event {seq}: commitment {commitment.commitment_id} is already released"
                    raise RegistryError(msg)
                self.commitments[commitment.commitment_id] = commitment.model_copy(
                    update={"released": True}
                )
                asset = self._asset(commitment.asset_id, seq)
                remaining = tuple(c for c in asset.commitments if c != commitment.commitment_id)
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(update={"commitments": remaining}), seq
                )
            case QuestionOpened():
                self._asset(payload.asset_id, seq)
                self._fresh(self.questions, payload.question_id, "question", seq)
                self.questions[payload.question_id] = Question(
                    question_id=payload.question_id,
                    asset_id=payload.asset_id,
                    question=payload.question,
                    alternatives=payload.alternatives,
                    slot=payload.slot,
                    consumer=payload.consumer,
                    evidence=payload.evidence,
                    draft=dict(payload.draft),
                    draft_probabilities=dict(payload.draft_probabilities),
                    seq=seq,
                )
            case DecisionRecorded():
                question = self._require(self.questions, payload.question_id, "question", seq)
                self._fresh(self.decisions, payload.decision_id, "decision", seq)
                if question.decision_id is not None:
                    msg = (
                        f"event {seq}: question {question.question_id} is already decided by "
                        f"{question.decision_id}"
                    )
                    raise RegistryError(msg)
                if payload.selected not in question.alternatives:
                    msg = (
                        f"event {seq}: decision {payload.decision_id} selected "
                        f"{payload.selected!r}, not one of {list(question.alternatives)}"
                    )
                    raise RegistryError(msg)
                self.decisions[payload.decision_id] = Decision(
                    decision_id=payload.decision_id,
                    question_id=payload.question_id,
                    selected=payload.selected,
                    rationale=payload.rationale,
                    scope=payload.scope,
                    actor=event.actor,
                    authority=event.authority,
                    seq=seq,
                )
                self.questions[question.question_id] = question.model_copy(
                    update={"decision_id": payload.decision_id}
                )
            case ProposalOpened():
                self._fresh(self.proposals, payload.proposal_id, "proposal", seq)
                for asset_id in payload.affected_assets:
                    self._not_retired(self._asset(asset_id, seq), seq, "propose a change to")
                self.proposals[payload.proposal_id] = Proposal(
                    proposal_id=payload.proposal_id,
                    summary=payload.summary,
                    changes=payload.changes,
                    affected_assets=payload.affected_assets,
                    assumed_revision=payload.assumed_revision,
                    kind_of_change=payload.kind_of_change,
                    alternatives=payload.alternatives,
                    cost_current_rows=payload.cost_current_rows,
                    cost_proposed_rows=payload.cost_proposed_rows,
                    evidence_required=payload.evidence_required,
                    shared_asset=payload.shared_asset,
                    dependents=payload.dependents,
                    providers=dict(payload.providers),
                    assumed_active=dict(payload.assumed_active),
                    assumed_commitments=dict(payload.assumed_commitments),
                    seq=seq,
                )
            case ProposalApproved():
                proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
                if proposal.status is not ProposalStatus.OPEN:
                    msg = f"event {seq}: proposal {proposal.proposal_id} is {proposal.status}"
                    raise RegistryError(msg)
                self.proposals[proposal.proposal_id] = proposal.model_copy(
                    update={"status": ProposalStatus.APPROVED, "reason": payload.reason}
                )
            case ProposalRejected():
                proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
                if proposal.status in {ProposalStatus.ACTIVATED, ProposalStatus.REJECTED}:
                    msg = f"event {seq}: proposal {proposal.proposal_id} is {proposal.status}"
                    raise RegistryError(msg)
                self.proposals[proposal.proposal_id] = proposal.model_copy(
                    update={"status": ProposalStatus.REJECTED, "reason": payload.reason}
                )
            case ImplementationPlanned():
                proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
                impl = payload.implementation
                asset = self._asset(impl.asset_id, seq)
                self._not_retired(asset, seq, "plan an implementation of")
                if asset.spec is None:
                    msg = f"event {seq}: asset {asset.asset_id} has no specification to implement"
                    raise RegistryError(msg)
                if impl.spec_version != asset.spec.spec_version:
                    msg = (
                        f"event {seq}: implementation {impl.implementation_id} is for spec version "
                        f"{impl.spec_version}; {asset.asset_id} is at {asset.spec.spec_version}"
                    )
                    raise RegistryError(msg)
                self._fresh(self.implementations, impl.implementation_id, "implementation", seq)
                if impl.asset_id not in proposal.affected_assets:
                    msg = (
                        f"event {seq}: proposal {proposal.proposal_id} does not affect "
                        f"{impl.asset_id}"
                    )
                    raise RegistryError(msg)
                self.implementations[impl.implementation_id] = impl
                self.proposals[proposal.proposal_id] = proposal.model_copy(
                    update={"implementations": (*proposal.implementations, impl.implementation_id)}
                )
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(
                        update={"implementations": (*asset.implementations, impl.implementation_id)}
                    ),
                    seq,
                )
            case ImplementationRefreshed():
                impl = self._require(
                    self.implementations, payload.implementation_id, "implementation", seq
                )
                asset = self._asset(impl.asset_id, seq)
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(update={"source_progress": dict(payload.source_progress)}),
                    seq,
                )
            case BackfillProgressed():
                self._require(
                    self.implementations, payload.implementation_id, "implementation", seq
                )
            case VerificationRecorded():
                proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
                self._fresh(self.verifications, payload.verification_id, "verification", seq)
                if payload.implementation_id not in proposal.implementations:
                    msg = (
                        f"event {seq}: implementation {payload.implementation_id} is not part of "
                        f"proposal {proposal.proposal_id}"
                    )
                    raise RegistryError(msg)
                self.verifications[payload.verification_id] = Verification(
                    verification_id=payload.verification_id,
                    proposal_id=payload.proposal_id,
                    implementation_id=payload.implementation_id,
                    grade=payload.grade,
                    passed=payload.passed,
                    checks=payload.checks,
                    source_progress=dict(payload.source_progress),
                    uncertainty=payload.uncertainty,
                    seq=seq,
                )
                self.proposals[proposal.proposal_id] = proposal.model_copy(
                    update={"verifications": (*proposal.verifications, payload.verification_id)}
                )
            case InterfaceSwitched():
                proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
                if proposal.status not in {ProposalStatus.APPROVED, ProposalStatus.ACTIVATED}:
                    msg = f"event {seq}: proposal {proposal.proposal_id} is {proposal.status}"
                    raise RegistryError(msg)
                asset = self._asset(payload.asset_id, seq)
                self._not_retired(asset, seq, "switch the interface of")
                self._implementation_of(asset, payload.implementation_id, seq)
                self.proposals[proposal.proposal_id] = proposal.model_copy(
                    update={
                        "switched": {
                            **proposal.switched,
                            payload.asset_id: payload.implementation_id,
                        }
                    }
                )
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(update={"active_implementation": payload.implementation_id}),
                    seq,
                )
            case DeploymentActivated():
                self._apply_deployment(payload, event.effective_at, seq)
            case DeploymentRolledBack():
                self._apply_rollback(payload, seq)
            case AssetRetired():
                asset = self._asset(payload.asset_id, seq)
                if asset.commitments:
                    msg = (
                        f"event {seq}: asset {asset.asset_id} still has commitments "
                        f"{list(asset.commitments)}"
                    )
                    raise RegistryError(msg)
                dependents = self.dependents_of(asset.asset_id)
                if dependents:
                    msg = f"event {seq}: asset {asset.asset_id} is still read by {list(dependents)}"
                    raise RegistryError(msg)
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(
                        update={
                            "status": AssetStatus.RETIRED,
                            "active_implementation": None,
                            "depends_on": (),
                        }
                    ),
                    seq,
                )
            case ClassificationProduced():
                self.classifications.append(ModelCall(seq=seq, event_payload=payload))
            case DraftProduced():
                self.drafts.append(ModelCall(seq=seq, event_payload=payload))
            case UsageObserved():
                self._asset(payload.asset_id, seq)
                self.usage[(payload.asset_id, payload.consumer)] = payload
            case RefreshCostObserved():
                self._asset(payload.asset_id, seq)
                self.refresh_costs[payload.asset_id] = payload
            case UnmanagedReadObserved():
                self._asset(payload.asset_id, seq)
                self.unmanaged_reads[(payload.asset_id, payload.consumer)] = payload
            case CommitmentProposed():
                self._asset(payload.asset_id, seq)
                self.commitment_proposals[(payload.asset_id, payload.consumer)] = payload
        self.revision = seq

    # ---- deployments ----

    def _apply_deployment(
        self, payload: DeploymentActivated, effective_at: datetime, seq: int
    ) -> None:
        proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
        self._fresh(self.deployments, payload.deployment_id, "deployment", seq)
        if proposal.status is not ProposalStatus.APPROVED:
            msg = (
                f"event {seq}: proposal {proposal.proposal_id} is {proposal.status}; only an "
                "approved proposal can be activated"
            )
            raise RegistryError(msg)
        cited = {**payload.verifications}
        if payload.verification_id:
            cited.setdefault("", payload.verification_id)
        for asset_id, implementation in payload.activated.items():
            asset = self._asset(asset_id, seq)
            self._not_retired(asset, seq, "activate")
            if asset_id not in proposal.affected_assets:
                msg = f"event {seq}: proposal {proposal.proposal_id} does not affect {asset_id}"
                raise RegistryError(msg)
            impl = self._implementation_of(asset, implementation, seq)
            if asset.spec is None or impl.spec_version != asset.spec.spec_version:
                msg = (
                    f"event {seq}: {implementation} implements spec version {impl.spec_version}; "
                    f"{asset_id} is at {asset.spec.spec_version if asset.spec else 'none'}"
                )
                raise RegistryError(msg)
            verification_id = payload.verifications.get(asset_id, payload.verification_id)
            verification = self._require(self.verifications, verification_id, "verification", seq)
            if not verification.passed:
                msg = (
                    f"event {seq}: deployment {payload.deployment_id} cites verification "
                    f"{verification.verification_id}, which did not pass"
                )
                raise RegistryError(msg)
            if verification.proposal_id != proposal.proposal_id:
                msg = (
                    f"event {seq}: verification {verification.verification_id} belongs to "
                    f"proposal {verification.proposal_id}, not {proposal.proposal_id}"
                )
                raise RegistryError(msg)
            if verification.implementation_id != implementation:
                msg = (
                    f"event {seq}: verification {verification.verification_id} is for "
                    f"{verification.implementation_id}, not {implementation}"
                )
                raise RegistryError(msg)
        self.deployments[payload.deployment_id] = Deployment(
            deployment_id=payload.deployment_id,
            proposal_id=payload.proposal_id,
            verification_id=payload.verification_id,
            verifications=dict(payload.verifications),
            assumed_revision=payload.assumed_revision,
            activated=dict(payload.activated),
            replaced=dict(payload.replaced),
            staged=payload.staged,
            effective_at=effective_at,
            seq=seq,
        )
        self.proposals[proposal.proposal_id] = proposal.model_copy(
            update={"deployment_id": payload.deployment_id, "status": ProposalStatus.ACTIVATED}
        )
        for asset_id, implementation in payload.activated.items():
            asset = self.assets[asset_id]
            reads = tuple(
                d.name
                for d in self.implementations[implementation].dependencies
                if d.kind == "asset"
            )
            self.assets[asset_id] = _touch(
                asset.model_copy(
                    update={
                        "active_implementation": implementation,
                        "status": AssetStatus.ACTIVE,
                        "depends_on": reads,
                    }
                ),
                seq,
            )

    def _apply_rollback(self, payload: DeploymentRolledBack, seq: int) -> None:
        deployment = self._require(self.deployments, payload.deployment_id, "deployment", seq)
        if deployment.rolled_back:
            msg = f"event {seq}: deployment {deployment.deployment_id} is already rolled back"
            raise RegistryError(msg)
        for asset_id, implementation in deployment.activated.items():
            asset = self._asset(asset_id, seq)
            self._not_retired(asset, seq, "roll back")
            if asset.active_implementation != implementation:
                msg = (
                    f"event {seq}: {asset_id} is at {asset.active_implementation}, not "
                    f"{implementation}; only the latest deployment of an asset can be rolled back"
                )
                raise RegistryError(msg)
        self.deployments[deployment.deployment_id] = deployment.model_copy(
            update={"rolled_back": True, "rollback_reason": payload.reason}
        )
        for asset_id in deployment.activated:
            asset = self.assets[asset_id]
            restored = payload.restored.get(asset_id, deployment.replaced.get(asset_id))
            reads: tuple[str, ...] = ()
            if restored is not None and restored in self.implementations:
                reads = tuple(
                    d.name for d in self.implementations[restored].dependencies if d.kind == "asset"
                )
            self.assets[asset_id] = _touch(
                asset.model_copy(
                    update={
                        "active_implementation": restored,
                        "status": AssetStatus.ACTIVE if restored else AssetStatus.SPECIFIED,
                        "depends_on": reads,
                    }
                ),
                seq,
            )

    # ---- helpers ----

    def _asset(self, asset_id: str, seq: int) -> Asset:
        return self._require(self.assets, asset_id, "asset", seq)

    def _implementation_of(self, asset: Asset, implementation_id: str, seq: int) -> Implementation:
        if implementation_id not in asset.implementations:
            msg = f"event {seq}: {implementation_id} is not an implementation of {asset.asset_id}"
            raise RegistryError(msg)
        return self.implementations[implementation_id]

    @staticmethod
    def _not_retired(asset: Asset, seq: int, action: str) -> None:
        if asset.status is AssetStatus.RETIRED:
            msg = f"event {seq}: cannot {action} retired asset {asset.asset_id}"
            raise RegistryError(msg)

    @staticmethod
    def _fresh[T](table: dict[str, T], key: str, kind: str, seq: int) -> None:
        if key in table:
            msg = f"event {seq}: {kind} {key!r} already exists"
            raise RegistryError(msg)

    @staticmethod
    def _require[T](table: dict[str, T], key: str, kind: str, seq: int) -> T:
        try:
            return table[key]
        except KeyError:
            msg = f"event {seq} refers to unknown {kind} {key!r}"
            raise RegistryError(msg) from None


def _touch(asset: Asset, seq: int) -> Asset:
    return asset.model_copy(update={"history": (*asset.history, seq)})
