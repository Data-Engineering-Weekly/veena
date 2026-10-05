"""Pydantic models for the specification IR."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from veena.refs import OriginRef, PolicyRef, SemanticRef

_IDENT = r"^[a-z][a-z0-9_]*$"


class Aggregation(StrEnum):
    """How a measure combines rows."""

    SUM = "sum"
    COUNT = "count"
    MIN = "min"
    MAX = "max"
    AVG = "avg"
    COUNT_DISTINCT = "count_distinct"

    @property
    def reaggregable(self) -> bool:
        """Whether partial results at a finer grain can be combined into the coarser grain."""
        return self in {Aggregation.SUM, Aggregation.COUNT, Aggregation.MIN, Aggregation.MAX}


class TimeBucket(StrEnum):
    """The time component of an asset's grain.

    `NONE` means the asset has no time dimension: each row already covers the whole window.
    """

    NONE = "none"
    DAY = "day"
    MONTH = "month"


class Population(BaseModel):
    """Which entity instances appear as rows."""

    model_config = ConfigDict(frozen=True)

    entity: SemanticRef
    include_inactive: bool = Field(
        default=True,
        description="Keep members with no qualifying activity, with zero-valued measures.",
    )


class Grain(BaseModel):
    """One row per combination of these keys and this time bucket."""

    model_config = ConfigDict(frozen=True)

    keys: tuple[SemanticRef, ...] = Field(min_length=1)
    time_bucket: TimeBucket = TimeBucket.NONE


class Measure(BaseModel):
    """A named aggregation of an approved calculation."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=_IDENT)
    calculation: SemanticRef
    aggregation: Aggregation


class Window(BaseModel):
    """How much time the asset covers.

    A windowed asset sets `complete_calendar_days`: the trailing N complete days before the
    current day in `timezone`. A time-bucketed asset sets `history_days`: how far back it
    retains buckets. Both may be set; `history_days` then bounds the backfill.
    """

    model_config = ConfigDict(frozen=True)

    complete_calendar_days: int | None = Field(default=None, ge=1)
    history_days: int | None = Field(default=None, ge=1)
    timezone: str = "UTC"

    @model_validator(mode="after")
    def _at_least_one(self) -> Window:
        if self.complete_calendar_days is None and self.history_days is None:
            msg = "window needs complete_calendar_days or history_days"
            raise ValueError(msg)
        return self

    @property
    def span_days(self) -> int:
        """Days of source history the asset needs."""
        return max(self.complete_calendar_days or 0, self.history_days or 0)


class Filter(BaseModel):
    """A predicate on an attribute that every row must satisfy."""

    model_config = ConfigDict(frozen=True)

    attribute: SemanticRef
    op: Literal["eq", "in"]
    values: tuple[str, ...] = Field(min_length=1)


class ServiceRequirements(BaseModel):
    """What the asset promises operationally."""

    model_config = ConfigDict(frozen=True)

    incorporate_within: timedelta = Field(
        description=(
            "Maximum delay between source data becoming available and the asset reflecting it."
        )
    )
    access_policy: PolicyRef
    budget_policy: PolicyRef


class AssetSpec(BaseModel):
    """The versioned meaning of one asset.

    `provenance` maps a requirement path, for example `window.complete_calendar_days`, to the
    message, decision, or policy it came from.
    """

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(pattern=_IDENT)
    spec_version: int = Field(default=1, ge=1)
    population: Population
    grain: Grain
    measures: tuple[Measure, ...] = Field(min_length=1)
    window: Window
    filters: tuple[Filter, ...] = ()
    identity_policy: SemanticRef
    provenance: dict[str, OriginRef] = Field(default_factory=dict)
    service: ServiceRequirements

    @model_validator(mode="after")
    def _consistent(self) -> AssetSpec:
        if self.population.entity not in self.grain.keys:
            msg = (
                f"population entity {self.population.entity} must be one of the grain keys "
                f"{[str(k) for k in self.grain.keys]}"
            )
            raise ValueError(msg)
        names = [m.name for m in self.measures]
        if len(names) != len(set(names)):
            msg = f"measure names must be unique, got {names}"
            raise ValueError(msg)
        if self.grain.time_bucket is TimeBucket.NONE and self.window.complete_calendar_days is None:
            msg = "an asset without a time bucket must set window.complete_calendar_days"
            raise ValueError(msg)
        if self.grain.time_bucket is not TimeBucket.NONE and self.window.history_days is None:
            msg = "a time-bucketed asset must set window.history_days"
            raise ValueError(msg)
        return self

    @property
    def ref(self) -> str:
        """The asset's identity plus spec version, for example `customer_spend_30d@1`."""
        return f"{self.asset_id}@{self.spec_version}"
