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
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    BACKFILL_PROGRESSED = "backfill_progressed"
    INTERFACE_SWITCHED = "interface_switched"
    IMPLEMENTATION_PLANNED = "implementation_planned"
    IMPLEMENTATION_REFRESHED = "implementation_refreshed"
    VERIFICATION_RECORDED = "verification_recorded"
    DEPLOYMENT_ACTIVATED = "deployment_activated"
    DEPLOYMENT_ROLLED_BACK = "deployment_rolled_back"
    ASSET_RETIRED = "asset_retired"
    CLASSIFICATION_PRODUCED = "classification_produced"
    DRAFT_PRODUCED = "draft_produced"
    REQUEST_REFUSED = "request_refused"
    ASSET_OBSERVED = "asset_observed"
    USAGE_OBSERVED = "usage_observed"
    REFRESH_COST_OBSERVED = "refresh_cost_observed"
    UNMANAGED_READ_OBSERVED = "unmanaged_read_observed"
    COMMITMENT_PROPOSED = "commitment_proposed"


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
    """The engine needs a person to choose a meaning.

    `alternatives` are the structured choices. `question` is prose a person reads; it may be
    drafted by a model and never replaces the alternatives. `evidence` has one entry per
    alternative. `slot` names the requirement in doubt, when there is one.
    """

    kind: Literal[EventKind.QUESTION_OPENED] = EventKind.QUESTION_OPENED
    question_id: str
    asset_id: str
    question: str
    alternatives: tuple[str, ...] = Field(min_length=2)
    slot: str = ""
    consumer: str = ""
    evidence: tuple[str, ...] = ()
    draft: dict[str, str] = Field(
        default_factory=dict, description="Slot -> inferred value, or 'open' for a gap."
    )
    draft_probabilities: dict[str, float] = Field(default_factory=dict)


class ClassificationProduced(_Payload):
    """A classifier picked from a closed set. Recorded before the engine acts on it."""

    kind: Literal[EventKind.CLASSIFICATION_PRODUCED] = EventKind.CLASSIFICATION_PRODUCED
    site: str
    slot: str
    asset_id: str
    model_id: str
    input_hash: str
    value: str
    probability: float = Field(ge=0.0, le=1.0)
    alternatives: dict[str, float] = Field(default_factory=dict)


class AssetObserved(_Payload):
    """An asset that already exists in a warehouse, imported as observed state.

    Nothing about its meaning is assumed; the spec arrives later as a confirmed draft.
    """

    kind: Literal[EventKind.ASSET_OBSERVED] = EventKind.ASSET_OBSERVED
    asset_id: str
    origin: str = Field(description="Where it came from, for example dbt:model.shoply.fct_orders")
    relation: str = Field(description="Fully qualified table or view name")
    layer: str = ""
    materialized: str = ""
    owner: str = ""
    description: str = ""
    columns: dict[str, str] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()
    checksum: str = ""
    sql: str = ""
    depends_on_assets: tuple[str, ...] = ()
    depends_on_sources: tuple[str, ...] = ()


class UsageObserved(_Payload):
    """Reads of one asset by one consumer over an observation window."""

    kind: Literal[EventKind.USAGE_OBSERVED] = EventKind.USAGE_OBSERVED
    asset_id: str
    consumer: str
    role: str = ""
    queries: int = Field(ge=0)
    bytes_scanned: int = Field(ge=0)
    credits: float = Field(ge=0.0)
    first_seen: datetime
    last_seen: datetime
    runs_per_day: float = Field(ge=0.0)
    distinct_hours: int = Field(ge=0, le=24)
    reader_class: str = ""
    reader_probability: float | None = None


class RefreshCostObserved(_Payload):
    """What it cost to rebuild one asset over the observation window, measured from history."""

    kind: Literal[EventKind.REFRESH_COST_OBSERVED] = EventKind.REFRESH_COST_OBSERVED
    asset_id: str
    runs: int = Field(ge=0)
    bytes_scanned: int = Field(ge=0)
    credits: float = Field(ge=0.0)
    window_start: datetime
    window_end: datetime


class UnmanagedReadObserved(_Payload):
    """A consumer read an internal relation directly instead of a published interface."""

    kind: Literal[EventKind.UNMANAGED_READ_OBSERVED] = EventKind.UNMANAGED_READ_OBSERVED
    asset_id: str
    consumer: str
    queries: int = Field(ge=0)
    last_seen: datetime
    reason: str


class CommitmentProposed(_Payload):
    """The engine thinks a recurring reader should hold a commitment, and asks."""

    kind: Literal[EventKind.COMMITMENT_PROPOSED] = EventKind.COMMITMENT_PROPOSED
    asset_id: str
    consumer: str
    reader_class: str
    probability: float = Field(ge=0.0, le=1.0)
    suggested_incorporate_within_seconds: int = Field(ge=0)
    rationale: str


class RequestRefused(_Payload):
    """The engine will not resolve a request, and why. The request text stays on record."""

    kind: Literal[EventKind.REQUEST_REFUSED] = EventKind.REQUEST_REFUSED
    asset_id: str
    consumer: str
    reason: str


class DraftProduced(_Payload):
    """A drafter wrote text. Recorded before anyone reads it."""

    kind: Literal[EventKind.DRAFT_PRODUCED] = EventKind.DRAFT_PRODUCED
    site: str
    asset_id: str
    model_id: str
    input_hash: str
    text: str


class DecisionRecorded(_Payload):
    """A person chose one alternative, with the scope the choice applies to."""

    kind: Literal[EventKind.DECISION_RECORDED] = EventKind.DECISION_RECORDED
    decision_id: str
    question_id: str
    selected: str
    rationale: str
    scope: str


class ProposalOpened(_Payload):
    """The engine wants to change the graph and says exactly how.

    `kind_of_change` is `direct` (build one asset from sources), `share` (create a shared
    intermediate and rewire dependents), or `repair`. `alternatives` lists what subsumption
    considered. Costs are fact rows processed per refresh, measured on the backend.
    """

    kind: Literal[EventKind.PROPOSAL_OPENED] = EventKind.PROPOSAL_OPENED
    proposal_id: str
    summary: str
    changes: tuple[str, ...] = Field(min_length=1)
    affected_assets: tuple[str, ...]
    assumed_revision: int = Field(ge=0)
    kind_of_change: Literal["direct", "share", "repair", "consolidate"] = "direct"
    alternatives: tuple[str, ...] = ()
    cost_current_rows: int | None = None
    cost_proposed_rows: int | None = None
    evidence_required: tuple[str, ...] = ()
    shared_asset: str | None = None
    dependents: tuple[str, ...] = ()


class ProposalApproved(_Payload):
    """A person or policy allowed the proposal to proceed."""

    kind: Literal[EventKind.PROPOSAL_APPROVED] = EventKind.PROPOSAL_APPROVED
    proposal_id: str
    reason: str = ""


class ProposalRejected(_Payload):
    """The proposal will not proceed, and why."""

    kind: Literal[EventKind.PROPOSAL_REJECTED] = EventKind.PROPOSAL_REJECTED
    proposal_id: str
    reason: str


class BackfillProgressed(_Payload):
    """A backfill moved a source position forward for an implementation."""

    kind: Literal[EventKind.BACKFILL_PROGRESSED] = EventKind.BACKFILL_PROGRESSED
    implementation_id: str
    source: str
    position_from: datetime
    position_to: datetime
    rows: int = Field(ge=0)


class InterfaceSwitched(_Payload):
    """One interface moved during a staged migration, before the deployment completed."""

    kind: Literal[EventKind.INTERFACE_SWITCHED] = EventKind.INTERFACE_SWITCHED
    proposal_id: str
    asset_id: str
    implementation_id: str


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
    replaced: dict[str, str | None] = Field(
        default_factory=dict, description="asset_id -> previous implementation_id, or None"
    )
    staged: bool = False


class DeploymentRolledBack(_Payload):
    """A deployment was reverted. Future reads are fixed; past reads are not."""

    kind: Literal[EventKind.DEPLOYMENT_ROLLED_BACK] = EventKind.DEPLOYMENT_ROLLED_BACK
    deployment_id: str
    reason: str
    affected_consumers: tuple[str, ...]
    interval_start: datetime
    interval_end: datetime
    restored: dict[str, str | None] = Field(
        default_factory=dict, description="asset_id -> implementation_id the interface went back to"
    )


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
    | ProposalApproved
    | ProposalRejected
    | BackfillProgressed
    | InterfaceSwitched
    | ImplementationPlanned
    | ImplementationRefreshed
    | VerificationRecorded
    | DeploymentActivated
    | DeploymentRolledBack
    | AssetRetired
    | ClassificationProduced
    | DraftProduced
    | RequestRefused
    | AssetObserved
    | UsageObserved
    | RefreshCostObserved
    | UnmanagedReadObserved
    | CommitmentProposed,
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
