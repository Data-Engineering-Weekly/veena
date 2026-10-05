"""The plan model. A plan names what to compute; a backend decides how to say it in SQL.

No field here may be specific to one backend.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from veena.refs import SemanticRef
from veena.spec.model import Aggregation, TimeBucket

EVENT_AT = "event_at"
BUCKET_COLUMN = {TimeBucket.DAY: "day", TimeBucket.MONTH: "month"}


class _Step(BaseModel):
    model_config = ConfigDict(frozen=True)


class SourceScan(_Step):
    """Read every row of one trusted source."""

    kind: Literal["source_scan"] = "source_scan"
    source: str
    table: str
    columns: tuple[str, ...]


class TimeAttribution(_Step):
    """Name the timestamp that attributes each fact to a date, as the column `event_at`."""

    kind: Literal["time_attribution"] = "time_attribution"
    time_rule: SemanticRef
    timestamp_column: str
    timezone: str


class WindowStep(_Step):
    """Keep facts whose `event_at` falls in the trailing complete-day window.

    Exactly one of `complete_calendar_days` or `history_days` is set.
    """

    kind: Literal["window"] = "window"
    complete_calendar_days: int | None = None
    history_days: int | None = None
    timezone: str

    @property
    def days(self) -> int:
        """Number of complete days the window spans."""
        return self.complete_calendar_days or self.history_days or 0


class IdentityJoin(_Step):
    """Attach each fact to an entity instance through the mapping source.

    `respect_validity` is always true for plans the compiler produces. It exists so a test can
    build the naive join the article describes.
    """

    kind: Literal["identity_join"] = "identity_join"
    policy: SemanticRef
    mapping_source: str = ""
    mapping_table: str = ""
    fact_key: str
    mapping_fact_key: str = ""
    mapping_entity_key: str = ""
    entity_key: str
    fact_timestamp: str
    valid_from: str = ""
    valid_to: str = ""
    respect_validity: bool = True
    direct: bool = Field(
        default=False, description="The fact carries the entity key in fact_key; no join is needed."
    )


class AssetScan(_Step):
    """Read a published asset through one of its implementations instead of a source."""

    kind: Literal["asset_scan"] = "asset_scan"
    asset_id: str
    implementation_id: str
    columns: tuple[str, ...]


class BucketWindow(_Step):
    """Keep bucket rows whose date column falls in the trailing complete-day window."""

    kind: Literal["bucket_window"] = "bucket_window"
    bucket_column: str
    complete_calendar_days: int
    timezone: str


class RollupMeasure(BaseModel):
    """One measure re-aggregated from a finer grain."""

    model_config = ConfigDict(frozen=True)

    name: str
    aggregation: Aggregation


class Rollup(_Step):
    """Re-aggregate already-aggregated rows up to the entity key."""

    kind: Literal["rollup"] = "rollup"
    entity_key: str
    measures: tuple[RollupMeasure, ...]


class MeasurePlan(BaseModel):
    """One output measure: an approved expression and how to aggregate it."""

    model_config = ConfigDict(frozen=True)

    name: str
    calculation: SemanticRef
    expression: str
    aggregation: Aggregation


class Aggregate(_Step):
    """Group by the entity key and optional time bucket, then aggregate each measure."""

    kind: Literal["aggregate"] = "aggregate"
    entity_key: str
    time_bucket: TimeBucket
    timezone: str
    measures: tuple[MeasurePlan, ...]


class PopulationJoin(_Step):
    """Left join the entity source so members with no facts appear with zero-valued measures."""

    kind: Literal["population_join"] = "population_join"
    entity_source: str
    entity_table: str
    entity_key: str
    zero_fill: tuple[str, ...]
    carry: tuple[str, ...] = Field(default=(), description="Non-measure columns to keep.")


Step = Annotated[
    SourceScan
    | TimeAttribution
    | WindowStep
    | IdentityJoin
    | Aggregate
    | PopulationJoin
    | AssetScan
    | BucketWindow
    | Rollup,
    Field(discriminator="kind"),
]


class Dependency(BaseModel):
    """An edge from an implementation to something it reads, with the version it assumed."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Literal["source", "asset"]
    version: str


class RefreshRule(BaseModel):
    """When the implementation must do work, derived from the spec."""

    model_config = ConfigDict(frozen=True)

    incorporate_within_seconds: int = Field(ge=0)
    boundary_timezone: str
    roll_at_day_boundary: bool
    correction_lookback_days: int = Field(ge=0)


class Plan(BaseModel):
    """An ordered list of steps that computes one asset."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    spec_version: int
    semantic_fingerprint: str
    steps: tuple[Step, ...] = Field(min_length=1)

    @property
    def output_columns(self) -> tuple[str, ...]:
        """Column names the final step produces."""
        columns: tuple[str, ...] = ()
        for step in self.steps:
            columns = step_output_columns(step, columns)
        return columns

    def step[T: _Step](self, kind: type[T]) -> T:
        """The single step of a type.

        Raises:
            KeyError: The plan has no step of that type.
        """
        for step in self.steps:
            if isinstance(step, kind):
                return step
        msg = f"plan for {self.asset_id} has no {kind.__name__} step"
        raise KeyError(msg)


class Implementation(BaseModel):
    """A plan rendered for one backend, plus what it depends on and when it refreshes."""

    model_config = ConfigDict(frozen=True)

    implementation_id: str
    asset_id: str
    spec_version: int
    plan: Plan
    backend: str
    sql: str
    dependencies: tuple[Dependency, ...]
    refresh_rule: RefreshRule
    drafted_steps: tuple[str, ...] = ()


def step_output_columns(step: Step, inputs: tuple[str, ...]) -> tuple[str, ...]:
    """Column names a step produces given its input columns."""
    match step:
        case SourceScan():
            return step.columns
        case TimeAttribution():
            return (*inputs, EVENT_AT)
        case WindowStep():
            return inputs
        case IdentityJoin():
            if step.direct and step.fact_key == step.entity_key:
                return inputs
            return (*inputs, step.entity_key)
        case Aggregate():
            bucket = BUCKET_COLUMN.get(step.time_bucket)
            keys = (step.entity_key,) if bucket is None else (step.entity_key, bucket)
            return (*keys, *(m.name for m in step.measures))
        case PopulationJoin():
            return (step.entity_key, *step.carry, *step.zero_fill)
        case AssetScan():
            return step.columns
        case BucketWindow():
            return inputs
        case Rollup():
            return (step.entity_key, *(m.name for m in step.measures))


def describe_plan(plan: Plan) -> str:
    """One line per step, for people."""
    lines = [f"plan {plan.asset_id}@{plan.spec_version} (semantic {plan.semantic_fingerprint})"]
    for index, step in enumerate(plan.steps, start=1):
        lines.append(f"  {index}. {_describe_step(step)}")
    lines.append(f"  -> {', '.join(plan.output_columns)}")
    return "\n".join(lines)


def _describe_step(step: Step) -> str:
    match step:
        case SourceScan():
            return f"scan {step.source} ({step.table})"
        case TimeAttribution():
            return f"attribute time by {step.time_rule}: {step.timestamp_column} as {EVENT_AT}"
        case WindowStep():
            what = "complete days" if step.complete_calendar_days else "days of history"
            return f"window trailing {step.days} {what} in {step.timezone}"
        case IdentityJoin():
            if step.direct:
                return f"take {step.entity_key} directly from the fact's {step.fact_key}"
            how = "at fact time" if step.respect_validity else "ignoring validity"
            return f"join {step.mapping_source} on {step.fact_key} {how} -> {step.entity_key}"
        case Aggregate():
            measures = ", ".join(
                f"{m.aggregation}({m.calculation}) as {m.name}" for m in step.measures
            )
            bucket = (
                f" and {BUCKET_COLUMN[step.time_bucket]}"
                if step.time_bucket in BUCKET_COLUMN
                else ""
            )
            return f"aggregate by {step.entity_key}{bucket}: {measures}"
        case PopulationJoin():
            return f"left join {step.entity_source} so every {step.entity_key} appears"
        case AssetScan():
            return f"scan asset {step.asset_id} via {step.implementation_id}"
        case BucketWindow():
            return (
                f"window {step.bucket_column} over trailing {step.complete_calendar_days} "
                f"complete days in {step.timezone}"
            )
        case Rollup():
            measures = ", ".join(f"{m.aggregation}({m.name})" for m in step.measures)
            return f"roll up by {step.entity_key}: {measures}"
