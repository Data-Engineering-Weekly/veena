"""A SQL backend over any DB-API-style connection, with dialect hooks for each engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import sqlglot
from sqlglot import exp

from veena.backends.protocol import DraftStepRequest, SourceProgress
from veena.calendar import last_complete_day, window_bounds
from veena.errors import BackendError, PlanError
from veena.planner.plan import (
    BUCKET_COLUMN,
    EVENT_AT,
    Aggregate,
    AssetScan,
    BucketWindow,
    IdentityJoin,
    Plan,
    PopulationJoin,
    Rollup,
    SourceScan,
    Step,
    TimeAttribution,
    WindowStep,
    step_output_columns,
)
from veena.spec.model import Aggregation, TimeBucket

if TYPE_CHECKING:
    from collections.abc import Sequence

    from veena.models.protocol import Drafter

_AGG_SQL = {
    Aggregation.SUM: "SUM({e})",
    Aggregation.COUNT: "COUNT({e})",
    Aggregation.MIN: "MIN({e})",
    Aggregation.MAX: "MAX({e})",
    Aggregation.AVG: "AVG({e})",
    Aggregation.COUNT_DISTINCT: "COUNT(DISTINCT {e})",
}


class Cursor(Protocol):
    """The slice of a DB-API cursor the backend uses."""

    description: Any

    def fetchall(self) -> Sequence[tuple[Any, ...]]:
        """Return every row."""
        ...

    def fetchone(self) -> tuple[Any, ...] | None:
        """Return one row or None."""
        ...


class Connection(Protocol):
    """The slice of a DB-API connection the backend uses."""

    def execute(self, sql: str, /) -> Any:
        """Run one statement; the result has fetchall, fetchone, and description."""
        ...


class SqlBackend:
    """Renders plans and runs them on a connection. Subclasses supply the dialect.

    `unsupported` lists step kinds this instance refuses to render itself, so a drafter can be
    exercised. The compiler never needs it; tests do.
    """

    name = "sql"
    dialect = "duckdb"
    atomic_ddl = True

    def __init__(
        self,
        conn: Connection,
        *,
        drafter: Drafter | None = None,
        unsupported: frozenset[str] = frozenset(),
    ) -> None:
        """Wrap an open connection."""
        self._conn = conn
        self._drafter = drafter
        self._unsupported = unsupported

    # ---- dialect hooks ----

    def ts_literal(self, value: datetime) -> str:
        """A timezone-aware timestamp literal."""
        return f"TIMESTAMPTZ '{value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')}+00'"

    def local_timestamp(self, expression: str, timezone: str) -> str:
        """Convert a timestamp expression to wall-clock time in `timezone`."""
        return f"{expression} AT TIME ZONE '{timezone}'"

    def bucket(self, step_bucket: TimeBucket, expression: str, timezone: str) -> str:
        """Truncate a timestamp to a date bucket."""
        local = self.local_timestamp(expression, timezone)
        if step_bucket is TimeBucket.DAY:
            return f"CAST({local} AS DATE)"
        return f"CAST(DATE_TRUNC('month', {local}) AS DATE)"

    def create_table(self, name: str, sql: str) -> str:
        """DDL that creates or replaces a table from a query."""
        return f"CREATE OR REPLACE TABLE {name} AS {sql}"

    def create_view(self, name: str, relation: str) -> str:
        """DDL that creates or repoints a view."""
        return f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {relation}"

    # ---- Backend protocol ----

    def render(self, plan: Plan, *, as_of: datetime) -> tuple[str, tuple[str, ...]]:
        """Render a plan as a chain of CTEs, one per step."""
        ctes: list[str] = []
        drafted: list[str] = []
        columns: tuple[str, ...] = ()
        previous = ""
        allowed = _allowed_tables(plan)
        for index, step in enumerate(plan.steps):
            name = f"s{index}"
            outputs = step_output_columns(step, columns)
            if step.kind in self._unsupported:
                body = self._draft_step(step, previous, columns, outputs, allowed)
                drafted.append(step.kind)
            else:
                body = self._render_step(step, previous, as_of)
            ctes.append(f"{name} AS (\n{body}\n)")
            columns = outputs
            previous = name
        sql = "WITH\n" + ",\n".join(ctes) + f"\nSELECT * FROM {previous}"
        return sql, tuple(drafted)

    def materialize(self, implementation_id: str, sql: str, plan: Plan) -> SourceProgress:
        """Create or replace the table for an implementation and measure source progress."""
        try:
            self._conn.execute(self.create_table(implementation_id, sql))
            row = self._conn.execute(f"SELECT count(*) FROM {implementation_id}").fetchone()
        except Exception as exc:
            msg = f"materialize {implementation_id}: {exc}"
            raise BackendError(msg) from exc
        return SourceProgress(positions=self._progress(plan), row_count=int(row[0]) if row else 0)

    def publish(self, asset_id: str, implementation_id: str) -> None:
        """Create or repoint the asset's view in one statement."""
        try:
            self._conn.execute(self.create_view(asset_id, implementation_id))
        except Exception as exc:
            msg = f"publish {asset_id} -> {implementation_id}: {exc}"
            raise BackendError(msg) from exc

    def unpublish(self, asset_id: str) -> None:
        """Drop the asset's view if it exists."""
        try:
            self._conn.execute(f"DROP VIEW IF EXISTS {asset_id}")
        except Exception as exc:
            msg = f"unpublish {asset_id}: {exc}"
            raise BackendError(msg) from exc

    @property
    def atomic_publish(self) -> bool:
        """Whether DDL runs inside a transaction on this engine."""
        return self.atomic_ddl

    def publish_many(self, switches: dict[str, str]) -> None:
        """Repoint every view, in one transaction when the engine allows it."""
        if not self.atomic_ddl:
            for asset_id, implementation_id in switches.items():
                self.publish(asset_id, implementation_id)
            return
        self._conn.execute("BEGIN")
        try:
            for asset_id, implementation_id in switches.items():
                self._conn.execute(self.create_view(asset_id, implementation_id))
            self._conn.execute("COMMIT")
        except Exception as exc:
            self._conn.execute("ROLLBACK")
            msg = f"publish_many {switches}: {exc}"
            raise BackendError(msg) from exc

    def execute(self, sql: str) -> Sequence[tuple[object, ...]]:
        """Run a query and return all rows."""
        try:
            rows: Sequence[tuple[object, ...]] = self._conn.execute(sql).fetchall()
        except Exception as exc:
            msg = f"query failed: {exc}"
            raise BackendError(msg) from exc
        return rows

    def measure_rows_processed(self, plan: Plan, *, as_of: datetime) -> int:
        """Count the rows leaving the plan's window step by running the plan up to it."""
        cut = next(
            (i for i, s in enumerate(plan.steps) if isinstance(s, WindowStep | BucketWindow)),
            len(plan.steps) - 1,
        )
        prefix = plan.model_copy(update={"steps": plan.steps[: cut + 1]})
        sql, _ = self.render(prefix, as_of=as_of)
        row = self._conn.execute(f"SELECT count(*) FROM ({sql})").fetchone()
        return int(row[0]) if row else 0

    def source_bounds(self, table: str, timestamp_column: str) -> tuple[datetime, datetime] | None:
        """Oldest and newest timestamps in a source, in UTC."""
        row = self._conn.execute(
            f"SELECT min({timestamp_column}), max({timestamp_column}) FROM {table}"
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        oldest: datetime = row[0]
        newest: datetime = row[1]
        return oldest.astimezone(UTC), newest.astimezone(UTC)

    # ---- rendering ----

    def _progress(self, plan: Plan) -> dict[str, datetime]:
        try:
            scan = plan.step(SourceScan)
            attribution = plan.step(TimeAttribution)
        except KeyError:
            return {}
        row = self._conn.execute(
            f"SELECT max({attribution.timestamp_column}) FROM {scan.table}"
        ).fetchone()
        if row is None or row[0] is None:
            return {}
        position: datetime = row[0]
        return {scan.source: position.astimezone(UTC)}

    def _render_step(self, step: Step, previous: str, as_of: datetime) -> str:
        match step:
            case SourceScan():
                return f"  SELECT {', '.join(step.columns)} FROM {step.table}"
            case TimeAttribution():
                return f"  SELECT *, {step.timestamp_column} AS {EVENT_AT} FROM {previous}"
            case WindowStep():
                start, end = window_bounds(as_of, step.days, step.timezone)
                return (
                    f"  SELECT * FROM {previous}\n"
                    f"  WHERE {EVENT_AT} >= {self.ts_literal(start)} "
                    f"AND {EVENT_AT} < {self.ts_literal(end)}"
                )
            case IdentityJoin() if step.direct:
                if step.fact_key == step.entity_key:
                    return f"  SELECT * FROM {previous}"
                return f"  SELECT f.*, f.{step.fact_key} AS {step.entity_key} FROM {previous} f"
            case IdentityJoin():
                on = f"m.{step.mapping_fact_key} = f.{step.fact_key}"
                if step.respect_validity:
                    on += (
                        f"\n    AND f.{step.fact_timestamp} >= m.{step.valid_from}"
                        f"\n    AND (m.{step.valid_to} IS NULL "
                        f"OR f.{step.fact_timestamp} < m.{step.valid_to})"
                    )
                return (
                    f"  SELECT f.*, m.{step.mapping_entity_key} AS {step.entity_key}\n"
                    f"  FROM {previous} f\n  JOIN {step.mapping_table} m ON {on}"
                )
            case Aggregate():
                keys = [step.entity_key]
                selects = [step.entity_key]
                if step.time_bucket in BUCKET_COLUMN:
                    bucket = self.bucket(step.time_bucket, EVENT_AT, step.timezone)
                    selects.append(f"{bucket} AS {BUCKET_COLUMN[step.time_bucket]}")
                    keys.append(bucket)
                for m in step.measures:
                    agg = _AGG_SQL[m.aggregation].format(e=f"({m.expression})")
                    selects.append(f"{agg} AS {m.name}")
                return (
                    f"  SELECT {', '.join(selects)}\n  FROM {previous}\n"
                    f"  GROUP BY {', '.join(keys)}"
                )
            case AssetScan():
                return f"  SELECT {', '.join(step.columns)} FROM {step.implementation_id}"
            case BucketWindow():
                end_day = last_complete_day(as_of, step.timezone) + timedelta(days=1)
                start_day = end_day - timedelta(days=step.complete_calendar_days)
                return (
                    f"  SELECT * FROM {previous}\n"
                    f"  WHERE {step.bucket_column} >= DATE '{start_day.isoformat()}' "
                    f"AND {step.bucket_column} < DATE '{end_day.isoformat()}'"
                )
            case Rollup():
                selects = [step.entity_key] + [
                    f"{_AGG_SQL[m.aggregation].format(e=m.name)} AS {m.name}" for m in step.measures
                ]
                return (
                    f"  SELECT {', '.join(selects)}\n  FROM {previous}\n"
                    f"  GROUP BY {step.entity_key}"
                )
            case PopulationJoin():
                selects = [f"c.{step.entity_key} AS {step.entity_key}"]
                selects += [f"a.{col}" for col in step.carry]
                selects += [f"COALESCE(a.{col}, 0) AS {col}" for col in step.zero_fill]
                return (
                    f"  SELECT {', '.join(selects)}\n  FROM {step.entity_table} c\n"
                    f"  LEFT JOIN {previous} a ON a.{step.entity_key} = c.{step.entity_key}"
                )

    def _draft_step(
        self,
        step: Step,
        previous: str,
        columns: tuple[str, ...],
        outputs: tuple[str, ...],
        allowed: tuple[str, ...],
    ) -> str:
        if self._drafter is None:
            msg = f"backend {self.name} cannot render step {step.kind} and no drafter is configured"
            raise PlanError(msg)
        request = DraftStepRequest(
            backend=self.name,
            step=step,
            input_relation=previous,
            input_columns=columns,
            output_columns=outputs,
            allowed_tables=(previous, *allowed) if previous else allowed,
        )
        draft = self._drafter.draft(request)
        _check_draft_tables(draft, request, self.dialect)
        self._check_draft_columns(draft, request)
        return "  " + draft.strip()

    def _check_draft_columns(self, draft: str, request: DraftStepRequest) -> None:
        if not request.input_relation:
            probe = f"SELECT * FROM ({draft}) WHERE false"
        else:
            # The input CTE does not exist on its own, so probe the draft over an empty copy.
            probe = (
                f"WITH {request.input_relation} AS "
                f"(SELECT {', '.join(f'NULL AS {c}' for c in request.input_columns)} WHERE false) "
                f"SELECT * FROM ({draft}) WHERE false"
            )
        try:
            cursor = self._conn.execute(probe)
        except Exception as exc:
            msg = f"drafted step {request.step.kind} does not run on {self.name}: {exc}"
            raise PlanError(msg) from exc
        produced = tuple(str(d[0]) for d in (cursor.description or ()))
        if produced != request.output_columns:
            msg = (
                f"drafted step {request.step.kind} produces columns {list(produced)}, "
                f"plan expects {list(request.output_columns)}"
            )
            raise PlanError(msg)


def _allowed_tables(plan: Plan) -> tuple[str, ...]:
    names: list[str] = []
    for step in plan.steps:
        match step:
            case SourceScan():
                names.append(step.table)
            case AssetScan():
                names.append(step.implementation_id)
            case IdentityJoin() if not step.direct:
                names.append(step.mapping_table)
            case PopulationJoin():
                names.append(step.entity_table)
            case _:
                pass
    return tuple(names)


def _check_draft_tables(draft: str, request: DraftStepRequest, dialect: str) -> None:
    try:
        tree = sqlglot.parse_one(draft, read=dialect)
    except sqlglot.errors.ParseError as exc:
        msg = f"drafted step {request.step.kind} does not parse: {exc}"
        raise PlanError(msg) from exc
    referenced = {t.name for t in tree.find_all(exp.Table)}
    extra = referenced - set(request.allowed_tables)
    if extra:
        msg = (
            f"drafted step {request.step.kind} reads {sorted(extra)}, "
            f"which the plan does not declare"
        )
        raise PlanError(msg)
