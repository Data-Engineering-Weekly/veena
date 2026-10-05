"""Projections of the event log into the records people and the engine read."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from veena.errors import RegistryError
from veena.evidence import CheckResult, Grade
from veena.registry.events import (
    AssetRequested,
    AssetRetired,
    CommitmentRegistered,
    CommitmentReleased,
    DecisionRecorded,
    DeploymentActivated,
    DeploymentRolledBack,
    ImplementationPlanned,
    ImplementationRefreshed,
    ProposalOpened,
    QuestionOpened,
    SpecResolved,
    VerificationRecorded,
)
from veena.spec.model import AssetSpec

if TYPE_CHECKING:
    from veena.planner.plan import Implementation
    from veena.registry.store import StoredEvent


class AssetStatus(StrEnum):
    """Where an asset is in its life."""

    REQUESTED = "requested"
    SPECIFIED = "specified"
    ACTIVE = "active"
    RETIRED = "retired"


class Asset(BaseModel):
    """Current view of one asset."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    status: AssetStatus
    spec: AssetSpec | None = None
    active_implementation: str | None = None
    commitments: tuple[str, ...] = ()
    implementations: tuple[str, ...] = ()
    source_progress: dict[str, datetime] = Field(default_factory=dict)
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
    implementations: tuple[str, ...] = ()
    verifications: tuple[str, ...] = ()
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
    assumed_revision: int
    activated: dict[str, str]
    rolled_back: bool = False
    rollback_reason: str = ""
    seq: int


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

    def apply(self, stored: StoredEvent) -> None:
        """Fold one stored event into the projection.

        Raises:
            RegistryError: The event refers to a missing record, or arrives out of order.
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
                self.assets[payload.asset_id] = _touch(asset, seq)
            case SpecResolved():
                asset = self._asset(payload.spec.asset_id, seq)
                status = asset.status
                if status in {AssetStatus.REQUESTED, AssetStatus.SPECIFIED}:
                    status = AssetStatus.SPECIFIED
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(update={"spec": payload.spec, "status": status}), seq
                )
            case CommitmentRegistered():
                asset = self._asset(payload.asset_id, seq)
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
                self.questions[payload.question_id] = Question(
                    question_id=payload.question_id,
                    asset_id=payload.asset_id,
                    question=payload.question,
                    alternatives=payload.alternatives,
                    seq=seq,
                )
            case DecisionRecorded():
                question = self._require(self.questions, payload.question_id, "question", seq)
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
                for asset_id in payload.affected_assets:
                    self._asset(asset_id, seq)
                self.proposals[payload.proposal_id] = Proposal(
                    proposal_id=payload.proposal_id,
                    summary=payload.summary,
                    changes=payload.changes,
                    affected_assets=payload.affected_assets,
                    assumed_revision=payload.assumed_revision,
                    seq=seq,
                )
            case ImplementationPlanned():
                proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
                impl = payload.implementation
                asset = self._asset(impl.asset_id, seq)
                if impl.implementation_id in asset.implementations:
                    msg = f"event {seq}: implementation {impl.implementation_id} already exists"
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
                    asset.model_copy(update={"source_progress": dict(payload.source_progress)}), seq
                )
            case VerificationRecorded():
                proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
                self._require(
                    self.implementations, payload.implementation_id, "implementation", seq
                )
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
            case DeploymentActivated():
                proposal = self._require(self.proposals, payload.proposal_id, "proposal", seq)
                verification = self._require(
                    self.verifications, payload.verification_id, "verification", seq
                )
                if not verification.passed:
                    msg = (
                        f"event {seq}: deployment {payload.deployment_id} cites verification "
                        f"{verification.verification_id}, which did not pass"
                    )
                    raise RegistryError(msg)
                self.deployments[payload.deployment_id] = Deployment(
                    deployment_id=payload.deployment_id,
                    proposal_id=payload.proposal_id,
                    verification_id=payload.verification_id,
                    assumed_revision=payload.assumed_revision,
                    activated=dict(payload.activated),
                    seq=seq,
                )
                self.proposals[proposal.proposal_id] = proposal.model_copy(
                    update={"deployment_id": payload.deployment_id}
                )
                for asset_id, implementation in payload.activated.items():
                    asset = self._asset(asset_id, seq)
                    if implementation not in asset.implementations:
                        msg = (
                            f"event {seq}: {implementation} is not an implementation of {asset_id}"
                        )
                        raise RegistryError(msg)
                    self.assets[asset_id] = _touch(
                        asset.model_copy(
                            update={
                                "active_implementation": implementation,
                                "status": AssetStatus.ACTIVE,
                            }
                        ),
                        seq,
                    )
            case DeploymentRolledBack():
                deployment = self._require(
                    self.deployments, payload.deployment_id, "deployment", seq
                )
                self.deployments[deployment.deployment_id] = deployment.model_copy(
                    update={"rolled_back": True, "rollback_reason": payload.reason}
                )
                for asset_id in deployment.activated:
                    asset = self._asset(asset_id, seq)
                    self.assets[asset_id] = _touch(asset, seq)
            case AssetRetired():
                asset = self._asset(payload.asset_id, seq)
                if asset.commitments:
                    msg = (
                        f"event {seq}: asset {asset.asset_id} still has commitments "
                        f"{list(asset.commitments)}"
                    )
                    raise RegistryError(msg)
                self.assets[asset.asset_id] = _touch(
                    asset.model_copy(
                        update={"status": AssetStatus.RETIRED, "active_implementation": None}
                    ),
                    seq,
                )
        self.revision = seq

    def _asset(self, asset_id: str, seq: int) -> Asset:
        return self._require(self.assets, asset_id, "asset", seq)

    @staticmethod
    def _require[T](table: dict[str, T], key: str, kind: str, seq: int) -> T:
        try:
            return table[key]
        except KeyError:
            msg = f"event {seq} refers to unknown {kind} {key!r}"
            raise RegistryError(msg) from None


def _touch(asset: Asset, seq: int) -> Asset:
    return asset.model_copy(update={"history": (*asset.history, seq)})
