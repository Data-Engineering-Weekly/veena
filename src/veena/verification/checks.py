"""Check builders and the verifier."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veena.calendar import sql_timestamptz, window_bounds
from veena.errors import SemanticRefError, VerificationError
from veena.evidence import CheckResult, Grade, verification_grade
from veena.spec.model import Aggregation, TimeBucket

if TYPE_CHECKING:
    from veena.semantic.model import SemanticModel
    from veena.spec.model import AssetSpec

Execute = Callable[[str], Sequence[tuple[object, ...]]]

_BUCKET = {TimeBucket.DAY: "day", TimeBucket.MONTH: "month"}
_BUCKET_SQL = {
    TimeBucket.DAY: "CAST({ts} AT TIME ZONE '{tz}' AS DATE)",
    TimeBucket.MONTH: "CAST(DATE_TRUNC('month', {ts} AT TIME ZONE '{tz}') AS DATE)",
}
_AGG = {
    Aggregation.SUM: "SUM",
    Aggregation.COUNT: "COUNT",
    Aggregation.MIN: "MIN",
    Aggregation.MAX: "MAX",
    Aggregation.AVG: "AVG",
}
ABSOLUTE_TOLERANCE = 0.005


class Check(BaseModel):
    """One generated query and how to judge its rows."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    grade: Grade
    structural: bool
    sql: str
    judge: str
    uncertainty: str = ""


class VerificationOutcome(BaseModel):
    """Everything verification concluded about one relation."""

    model_config = ConfigDict(frozen=True)

    relation: str
    as_of: datetime
    passed: bool
    grade: Grade
    checks: tuple[CheckResult, ...]
    uncertainty: str


def build_checks(
    spec: AssetSpec, semantic: SemanticModel, relation: str, *, as_of: datetime
) -> tuple[Check, ...]:
    """Generate the checks a spec implies.

    Args:
        spec: The specification the relation claims to implement.
        semantic: The approved definitions to reconcile against.
        relation: Table or view name holding the candidate output.
        as_of: Instant that fixes the window.

    Returns:
        Uniqueness and population checks (structural), then one reconciliation per measure and
        one conservation check per re-aggregable measure (value checks).

    Raises:
        VerificationError: The spec names definitions the semantic model lacks.
    """
    try:
        entity = semantic.entity(spec.population.entity)
        policy = semantic.identity_policy(spec.identity_policy)
        entity_source = semantic.source(entity.source)
        mapping = semantic.source(policy.mapping_source)
        fact = semantic.source(policy.fact_source)
        calculations = {m.name: semantic.calculation(m.calculation) for m in spec.measures}
    except SemanticRefError as exc:
        msg = f"cannot verify {spec.ref}: {exc}"
        raise VerificationError(msg) from exc
    rules = [r for r in semantic.time_rules if r.source == fact.name]
    if len(rules) != 1:
        msg = f"cannot verify {spec.ref}: {len(rules)} time rules cover {fact.name!r}"
        raise VerificationError(msg)
    rule = rules[0]

    key = entity.key
    bucket = _BUCKET.get(spec.grain.time_bucket)
    bucket_expr = (
        _BUCKET_SQL[spec.grain.time_bucket].format(
            ts=rule.timestamp_column, tz=spec.window.timezone
        )
        if bucket
        else None
    )
    group_cols = [key] + ([bucket] if bucket else [])
    start, end = window_bounds(as_of, spec.window.span_days, spec.window.timezone)
    in_window = (
        f"{rule.timestamp_column} >= {sql_timestamptz(start)} "
        f"AND {rule.timestamp_column} < {sql_timestamptz(end)}"
    )

    checks: list[Check] = [
        Check(
            name="unique_grain",
            description=f"one row per {', '.join(group_cols)}",
            grade=Grade.TEST,
            structural=True,
            sql=(f"SELECT count(*) - count(DISTINCT ({', '.join(group_cols)})) FROM {relation}"),
            judge="zero",
        )
    ]
    if spec.population.include_inactive and bucket is None:
        checks.append(
            Check(
                name="population_preserved",
                description=f"every {entity.name} in {entity_source.name} appears exactly once",
                grade=Grade.TEST,
                structural=True,
                sql=(
                    f"SELECT (SELECT count(*) FROM {entity_source.table}) - "
                    f"(SELECT count(DISTINCT {key}) FROM {relation}) + "
                    f"(SELECT count(*) FROM {relation} r WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {entity_source.table} e WHERE e.{key} = r.{key}))"
                ),
                judge="zero",
            )
        )
    else:
        checks.append(
            Check(
                name="population_known",
                description=f"every {key} in the output is a known {entity.name}",
                grade=Grade.TEST,
                structural=True,
                sql=(
                    f"SELECT count(*) FROM {relation} r WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {entity_source.table} e WHERE e.{key} = r.{key})"
                ),
                judge="zero",
            )
        )

    # Reference attribution written from the identity policy, independent of any candidate.
    attributed = (
        f"SELECT m.{policy.mapping_entity_key} AS {key}, f.*\n"
        f"    FROM {fact.table} f\n"
        f"    JOIN {mapping.table} m\n"
        f"      ON m.{policy.mapping_fact_key} = f.{policy.fact_key}\n"
        f"     AND f.{policy.fact_timestamp} >= m.{policy.valid_from}\n"
        f"     AND (m.{policy.valid_to} IS NULL "
        f"OR f.{policy.fact_timestamp} < m.{policy.valid_to})\n"
        f"    WHERE {in_window}"
    )
    for measure in spec.measures:
        calc = calculations[measure.name]
        agg = _AGG.get(measure.aggregation)
        if agg is None:
            checks.append(
                Check(
                    name=f"reconcile_{measure.name}",
                    description=(
                        f"{measure.name} uses {measure.aggregation}, no reconciliation rule"
                    ),
                    grade=Grade.SAMPLE,
                    structural=False,
                    sql="SELECT 0",
                    judge="skip",
                    uncertainty=f"no reconciliation for {measure.aggregation}",
                )
            )
            continue
        ref_select = ", ".join(
            [key, *([f"{bucket_expr} AS {bucket}"] if bucket and bucket_expr else [])]
        )
        ref_group = ", ".join([key, *([bucket_expr] if bucket_expr else [])])
        reference = (
            f"SELECT {ref_select}, {agg}({calc.expression}) AS expected\n"
            f"  FROM ({attributed}) a\n  GROUP BY {ref_group}"
        )
        join_on = " AND ".join(f"r.{c} = x.{c}" for c in group_cols)
        zero = "0" if measure.aggregation in {Aggregation.SUM, Aggregation.COUNT} else "NULL"
        checks.append(
            Check(
                name=f"reconcile_{measure.name}",
                description=(
                    f"{measure.name} equals {agg}({calc.name}@{calc.version}) attributed by "
                    f"{policy.name}@{policy.version}, per {', '.join(group_cols)}"
                ),
                grade=Grade.RECONCILIATION,
                structural=False,
                sql=(
                    f"WITH expected AS (\n{reference}\n)\n"
                    f"SELECT r.{key}, r.{measure.name}, COALESCE(x.expected, {zero}) AS expected\n"
                    f"FROM {relation} r\nFULL OUTER JOIN expected x ON {join_on}\n"
                    f"WHERE abs(COALESCE(r.{measure.name}, 0) - COALESCE(x.expected, 0)) > "
                    f"{ABSOLUTE_TOLERANCE}\nORDER BY 1"
                ),
                judge="no_rows",
                uncertainty=f"absolute tolerance {ABSOLUTE_TOLERANCE} {calc.unit}",
            )
        )
        if measure.aggregation in {Aggregation.SUM, Aggregation.COUNT}:
            checks.append(
                Check(
                    name=f"conserve_{measure.name}",
                    description=(
                        f"total {measure.name} equals {agg}({calc.name}@{calc.version}) over "
                        f"{fact.name} in the window, so every fact counts exactly once"
                    ),
                    grade=Grade.RECONCILIATION,
                    structural=False,
                    sql=(
                        f"SELECT (SELECT COALESCE({agg}({measure.name}), 0) FROM {relation}) - "
                        f"(SELECT COALESCE({agg}({calc.expression}), 0) FROM {fact.table} "
                        f"WHERE {in_window})"
                    ),
                    judge="zero",
                    uncertainty=(
                        "facts whose key maps to no entity in the window would be missing from "
                        "both sides of a per-entity reconciliation; this check catches them"
                    ),
                )
            )
    return tuple(checks)


def verify(
    spec: AssetSpec,
    semantic: SemanticModel,
    relation: str,
    execute: Execute,
    *,
    as_of: datetime,
) -> VerificationOutcome:
    """Build and run every check for a relation.

    Args:
        spec: The specification the relation claims to implement.
        semantic: The approved definitions.
        relation: Table or view holding the candidate output.
        execute: Runs one SQL string and returns rows.
        as_of: Instant that fixes the window.

    Returns:
        Pass or fail, the grade of evidence held, and every check result.
    """
    results = tuple(
        _run(check, execute) for check in build_checks(spec, semantic, relation, as_of=as_of)
    )
    passed = all(r.passed for r in results)
    uncertain = [r.uncertainty for r in results if r.uncertainty]
    return VerificationOutcome(
        relation=relation,
        as_of=as_of,
        passed=passed,
        grade=verification_grade(results),
        checks=results,
        uncertainty="; ".join(dict.fromkeys(uncertain)),
    )


def _run(check: Check, execute: Execute) -> CheckResult:
    if check.judge == "skip":
        return CheckResult(
            name=check.name,
            description=check.description,
            grade=check.grade,
            structural=check.structural,
            passed=True,
            detail="not checked",
            uncertainty=check.uncertainty,
        )
    rows = execute(check.sql)
    if check.judge == "zero":
        value = rows[0][0] if rows and rows[0] else None
        passed = value is not None and float(str(value)) == 0.0
        detail = f"difference {value}"
    else:
        passed = len(rows) == 0
        if rows:
            sample = ", ".join(f"{r[0]}: got {r[1]}, expected {r[2]}" for r in rows[:5])
            total = sum(abs(float(str(r[1] or 0)) - float(str(r[2] or 0))) for r in rows)
            detail = f"{len(rows)} keys differ, total discrepancy {total:.2f}; {sample}"
        else:
            detail = "all keys match"
    return CheckResult(
        name=check.name,
        description=check.description,
        grade=check.grade,
        structural=check.structural,
        passed=passed,
        detail=detail,
        uncertainty=check.uncertainty,
    )
