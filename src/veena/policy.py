"""Workspace policies the engine reads before it acts. People own these values."""

from __future__ import annotations

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
    min_benefit_rows: int = Field(default=1, ge=0)
    max_reorganizations_per_period: int = Field(default=1, ge=0)
    period_days: int = Field(default=30, ge=1)
    require_approval: bool = False
