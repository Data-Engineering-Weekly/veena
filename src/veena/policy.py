"""Workspace policies the engine reads before it acts. People own these values."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
