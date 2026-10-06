"""Workspace policies the engine reads before it acts. People own these values."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CauseClass(StrEnum):
    """The closed set of causes the triage classifier picks from for a failed check."""

    SOURCE_LAG = "source_lag"
    IDENTITY_MAPPING_CHANGE = "identity_mapping_change"
    SCHEMA_DRIFT = "schema_drift"
    LATE_CORRECTIONS = "late_corrections"
    CALCULATION_DRIFT = "calculation_drift"
    UNKNOWN = "unknown"


class RepairStrategy(StrEnum):
    """What the engine does about a failure. A policy table maps each cause class to one."""

    RE_PLAN_JOIN = "re_plan_join"
    REBUILD_WITH_LOOKBACK = "rebuild_with_lookback"
    WAIT_FOR_SOURCE = "wait_for_source"
    ESCALATE = "escalate"


class ReorganizationPolicy(BaseModel):
    """When the engine may reshape the graph instead of building directly.

    `min_benefit_rows` is the smallest saving in fact rows processed per refresh that justifies a
    shared intermediate. `max_reorganizations_per_period` caps how often any one asset may be
    rewired inside `period_days`. `require_approval` makes every share proposal wait for a
    person.
    """

    model_config = ConfigDict(frozen=True)

    ref: str = "policy:reorganization@1"
    autonomy: Literal["shadow", "assisted", "autonomous"] = Field(
        default="assisted",
        description=(
            "shadow records proposals and activates nothing; assisted activates under the other "
            "knobs; autonomous activates without approval."
        ),
    )
    min_benefit_rows: int = Field(default=1, ge=0)
    max_reorganizations_per_period: int = Field(default=1, ge=0)
    period_days: int = Field(default=30, ge=1)
    require_approval: bool = False


class RepairPolicy(BaseModel):
    """How a triaged failure turns into action. People own the table and the threshold.

    A cause below `min_probability`, or `unknown`, escalates to a person whatever the table says.
    """

    model_config = ConfigDict(frozen=True)

    ref: str = "policy:repair@1"
    min_probability: float = Field(default=0.7, ge=0.0, le=1.0)
    strategies: dict[CauseClass, RepairStrategy] = Field(
        default_factory=lambda: {
            CauseClass.SOURCE_LAG: RepairStrategy.WAIT_FOR_SOURCE,
            CauseClass.IDENTITY_MAPPING_CHANGE: RepairStrategy.RE_PLAN_JOIN,
            CauseClass.LATE_CORRECTIONS: RepairStrategy.REBUILD_WITH_LOOKBACK,
            CauseClass.SCHEMA_DRIFT: RepairStrategy.ESCALATE,
            CauseClass.CALCULATION_DRIFT: RepairStrategy.ESCALATE,
            CauseClass.UNKNOWN: RepairStrategy.ESCALATE,
        }
    )

    def strategy(self, cause: CauseClass, probability: float) -> RepairStrategy:
        """The strategy for one triage result, applying the threshold and the unknown rule."""
        if cause is CauseClass.UNKNOWN or probability < self.min_probability:
            return RepairStrategy.ESCALATE
        return self.strategies.get(cause, RepairStrategy.ESCALATE)


class RetentionPolicy(BaseModel):
    """How long physical work is kept after the last commitment on an asset ends.

    `quiet_days` is how long observed reads keep an asset from retiring even without a
    commitment. `keep_materialization_days` gates dropping a table; an asset whose last refresh
    processed at least `expensive_rows` waits `keep_expensive_days` instead, because rebuilding it
    costs more. `source_snapshot_days` is how long a deployment's source positions can be
    reproduced from the warehouse.
    """

    model_config = ConfigDict(frozen=True)

    ref: str = "policy:retention@1"
    quiet_days: int = Field(default=30, ge=0)
    keep_materialization_days: int = Field(default=30, ge=0)
    expensive_rows: int = Field(default=1_000_000, ge=0)
    keep_expensive_days: int = Field(default=90, ge=0)
    source_snapshot_days: int = Field(default=30, ge=0)


class WorkspaceDefaults(BaseModel):
    """Service terms a request may leave unstated. Each fill is recorded with policy provenance."""

    model_config = ConfigDict(frozen=True)

    ref: str = "policy:workspace_defaults@1"
    incorporate_within_seconds: int = Field(default=300, ge=0)
    fastest_incorporate_within_seconds: int = Field(
        default=300,
        ge=0,
        description="Freshness the budget policy can afford; faster asks a person.",
    )
    access_policy: str = "policy:workspace_access@1"
    budget_policy: str = "policy:workspace_budget@1"
    timezone: str = "UTC"


class ResolutionPolicy(BaseModel):
    """Thresholds that turn a classifier's probability into an action. People tune these."""

    model_config = ConfigDict(frozen=True)

    ref: str = "policy:resolution@1"
    default_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    slot_thresholds: dict[str, float] = Field(default_factory=dict)
    judge_mismatch_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    injection_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    max_draft_retries: int = Field(default=1, ge=0)

    def threshold(self, slot: str) -> float:
        """Threshold for one slot, falling back to the default."""
        return self.slot_thresholds.get(slot, self.default_threshold)


__all__ = [
    "CauseClass",
    "ReorganizationPolicy",
    "RepairPolicy",
    "RepairStrategy",
    "ResolutionPolicy",
    "RetentionPolicy",
    "WorkspaceDefaults",
]
