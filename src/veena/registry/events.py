"""Events the registry records. Each event carries a typed payload."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from veena.evidence import CheckResult, Grade
from veena.planner.plan import Implementation
from veena.spec.model import AssetSpec


class EventKind(StrEnum):
    """Every kind of event the registry accepts."""

    ASSET_REQUESTED = "asset_requested"
    SPEC_RESOLVED = "spec_resolved"
    COMMITMENT_REGISTERED = "commitment_registered"
    COMMITMENT_RELEASED = "commitment_released"
    QUESTION_OPENED = "question_opened"
    DECISION_RECORDED = "decision_recorded"
    PROPOSAL_OPENED = "proposal_opened"
    IMPLEMENTATION_PLANNED = "implementation_planned"
    IMPLEMENTATION_REFRESHED = "implementation_refreshed"
    VERIFICATION_RECORDED = "verification_recorded"
    DEPLOYMENT_ACTIVATED = "deployment_activated"
    DEPLOYMENT_ROLLED_BACK = "deployment_rolled_back"
    ASSET_RETIRED = "asset_retired"


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True)


class AssetRequested(_Payload):
    """A consumer asked for an asset, in their own words."""

    kind: Literal[EventKind.ASSET_REQUESTED] = EventKind.ASSET_REQUESTED
    asset_id: str
    consumer: str
    request_text: str


class SpecResolved(_Payload):
    """The engine resolved a request into a versioned spec."""

    kind: Literal[EventKind.SPEC_RESOLVED] = EventKind.SPEC_RESOLVED
    spec: AssetSpec


class CommitmentRegistered(_Payload):
    """The platform promised one consumer a service level on one asset."""

    kind: Literal[EventKind.COMMITMENT_REGISTERED] = EventKind.COMMITMENT_REGISTERED
    commitment_id: str
    asset_id: str
    consumer: str
    incorporate_within_seconds: int = Field(ge=0)
    correctness_tolerance: float = Field(default=0.0, ge=0.0)
    schema_stable: bool = True


class CommitmentReleased(_Payload):
    """A consumer no longer depends on the asset."""

    kind: Literal[EventKind.COMMITMENT_RELEASED] = EventKind.COMMITMENT_RELEASED
    commitment_id: str
    reason: str = ""


class QuestionOpened(_Payload):
    """The engine needs a person to choose a meaning."""

    kind: Literal[EventKind.QUESTION_OPENED] = EventKind.QUESTION_OPENED
    question_id: str
    asset_id: str
    question: str
    alternatives: tuple[str, ...] = Field(min_length=2)


class DecisionRecorded(_Payload):
    """A person chose one alternative, with the scope the choice applies to."""

    kind: Literal[EventKind.DECISION_RECORDED] = EventKind.DECISION_RECORDED
    decision_id: str
    question_id: str
    selected: str
    rationale: str
    scope: str


class ProposalOpened(_Payload):
    """The engine wants to change the graph and says exactly how."""

    kind: Literal[EventKind.PROPOSAL_OPENED] = EventKind.PROPOSAL_OPENED
    proposal_id: str
    summary: str
    changes: tuple[str, ...] = Field(min_length=1)
    affected_assets: tuple[str, ...]
    assumed_revision: int = Field(ge=0)


class ImplementationPlanned(_Payload):
    """The planner produced an implementation for a proposal."""

    kind: Literal[EventKind.IMPLEMENTATION_PLANNED] = EventKind.IMPLEMENTATION_PLANNED
    proposal_id: str
    implementation: Implementation


class ImplementationRefreshed(_Payload):
    """A backend materialized an implementation and reported how far the sources had reached."""

    kind: Literal[EventKind.IMPLEMENTATION_REFRESHED] = EventKind.IMPLEMENTATION_REFRESHED
    implementation_id: str
    as_of: datetime
    row_count: int = Field(ge=0)
    source_progress: dict[str, datetime]


class VerificationRecorded(_Payload):
    """Checks ran against an implementation. `grade` names the strongest evidence held."""

    kind: Literal[EventKind.VERIFICATION_RECORDED] = EventKind.VERIFICATION_RECORDED
    verification_id: str
    proposal_id: str
    implementation_id: str
    as_of: datetime
    grade: Grade
    passed: bool
    checks: tuple[CheckResult, ...]
    source_progress: dict[str, datetime] = Field(default_factory=dict)
    uncertainty: str = ""


class DeploymentActivated(_Payload):
    """The coordinator switched active implementations, assuming a registry revision."""

    kind: Literal[EventKind.DEPLOYMENT_ACTIVATED] = EventKind.DEPLOYMENT_ACTIVATED
    deployment_id: str
    proposal_id: str
    verification_id: str
    assumed_revision: int = Field(ge=0)
    activated: dict[str, str] = Field(description="asset_id -> implementation_id")


class DeploymentRolledBack(_Payload):
    """A deployment was reverted. Future reads are fixed; past reads are not."""

    kind: Literal[EventKind.DEPLOYMENT_ROLLED_BACK] = EventKind.DEPLOYMENT_ROLLED_BACK
    deployment_id: str
    reason: str
    affected_consumers: tuple[str, ...]


class AssetRetired(_Payload):
    """No commitment remains and the engine removed the asset's physical work."""

    kind: Literal[EventKind.ASSET_RETIRED] = EventKind.ASSET_RETIRED
    asset_id: str
    reason: str


Payload = Annotated[
    AssetRequested
    | SpecResolved
    | CommitmentRegistered
    | CommitmentReleased
    | QuestionOpened
    | DecisionRecorded
    | ProposalOpened
    | ImplementationPlanned
    | ImplementationRefreshed
    | VerificationRecorded
    | DeploymentActivated
    | DeploymentRolledBack
    | AssetRetired,
    Field(discriminator="kind"),
]


def _now() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    """One fact appended to the registry.

    `recorded_at` is when the registry wrote it. `effective_at` is when it took effect in the
    world, which may be earlier. `causes` names the events or records that led to this one.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    actor: str
    authority: str
    causes: tuple[str, ...] = ()
    recorded_at: datetime = Field(default_factory=_now)
    effective_at: datetime = Field(default_factory=_now)
    payload: Payload

    @property
    def kind(self) -> EventKind:
        """The payload's kind."""
        return self.payload.kind
