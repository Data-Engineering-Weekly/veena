"""Compile a specification into a plan. Deterministic; no model call anywhere."""

from __future__ import annotations

from typing import TYPE_CHECKING

from veena.errors import PlanError, SemanticRefError
from veena.planner.plan import (
    Aggregate,
    Dependency,
    IdentityJoin,
    MeasurePlan,
    Plan,
    PopulationJoin,
    RefreshRule,
    SourceScan,
    Step,
    TimeAttribution,
    WindowStep,
)
from veena.refs import SemanticRef
from veena.spec.model import Aggregation, TimeBucket

if TYPE_CHECKING:
    from veena.semantic.model import SemanticModel
    from veena.spec.model import AssetSpec

ZERO_FILL = {Aggregation.SUM, Aggregation.COUNT}


def compile_plan(spec: AssetSpec, semantic: SemanticModel) -> Plan:
    """Turn a spec into an ordered plan over the semantic model.

    Args:
        spec: The resolved specification.
        semantic: The approved meaning the plan may rely on.

    Returns:
        A plan whose steps are scan, time attribution, window, identity join, aggregate, and,
        when the population keeps inactive members, a population join.

    Raises:
        PlanError: The spec names something the semantic model lacks, or asks for a shape the
            compiler does not support yet.
    """
    if spec.filters:
        msg = f"{spec.ref}: filters are not supported by the planner yet"
        raise PlanError(msg)
    if len(spec.grain.keys) != 1:
        msg = f"{spec.ref}: only a single grain key is supported, got {len(spec.grain.keys)}"
        raise PlanError(msg)

    try:
        entity = semantic.entity(spec.population.entity)
        policy = semantic.identity_policy(spec.identity_policy)
        calculations = [semantic.calculation(m.calculation) for m in spec.measures]
        entity_source = semantic.source(entity.source)
        mapping_source = semantic.source(policy.mapping_source)
    except SemanticRefError as exc:
        msg = f"{spec.ref}: {exc}"
        raise PlanError(msg) from exc

    if policy.entity != spec.population.entity:
        msg = (
            f"{spec.ref}: identity policy {spec.identity_policy} attaches to {policy.entity}, "
            f"not to the population entity {spec.population.entity}"
        )
        raise PlanError(msg)

    fact_sources = {c.source for c in calculations}
    if len(fact_sources) != 1:
        msg = (
            f"{spec.ref}: measures span sources {sorted(fact_sources)}; one fact source is required"
        )
        raise PlanError(msg)
    fact_source_name = fact_sources.pop()
    if fact_source_name != policy.fact_source:
        msg = (
            f"{spec.ref}: calculation on source {fact_source_name!r} is not attached by identity "
            f"policy {spec.identity_policy}, which covers {policy.fact_source!r}"
        )
        raise PlanError(msg)
    fact_source = semantic.source(fact_source_name)

    rules = [r for r in semantic.time_rules if r.source == fact_source_name]
    if not rules:
        msg = f"{spec.ref}: no time rule attributes rows of {fact_source_name!r} to a date"
        raise PlanError(msg)
    if len(rules) > 1:
        names = sorted(f"{r.name}@{r.version}" for r in rules)
        msg = f"{spec.ref}: {len(rules)} time rules cover {fact_source_name!r}: {names}"
        raise PlanError(msg)
    rule = rules[0]
    if entity.key in fact_source.columns:
        msg = (
            f"{spec.ref}: fact source {fact_source_name!r} already has a column named "
            f"{entity.key!r}; the identity join cannot add it"
        )
        raise PlanError(msg)

    measures = tuple(
        MeasurePlan(
            name=m.name,
            calculation=m.calculation,
            expression=c.expression,
            aggregation=m.aggregation,
        )
        for m, c in zip(spec.measures, calculations, strict=True)
    )
    steps: list[Step] = [
        SourceScan(source=fact_source.name, table=fact_source.table, columns=fact_source.columns),
        TimeAttribution(
            time_rule=SemanticRef(name=rule.name, version=rule.version),
            timestamp_column=rule.timestamp_column,
            timezone=rule.timezone,
        ),
        WindowStep(
            complete_calendar_days=spec.window.complete_calendar_days
            if spec.grain.time_bucket is TimeBucket.NONE
            else None,
            history_days=spec.window.history_days
            if spec.grain.time_bucket is not TimeBucket.NONE
            else None,
            timezone=spec.window.timezone,
        ),
        IdentityJoin(
            policy=spec.identity_policy,
            mapping_source=mapping_source.name,
            mapping_table=mapping_source.table,
            fact_key=policy.fact_key,
            mapping_fact_key=policy.mapping_fact_key,
            mapping_entity_key=policy.mapping_entity_key,
            entity_key=entity.key,
            fact_timestamp=policy.fact_timestamp,
            valid_from=policy.valid_from,
            valid_to=policy.valid_to,
        ),
        Aggregate(
            entity_key=entity.key,
            time_bucket=spec.grain.time_bucket,
            timezone=spec.window.timezone,
            measures=measures,
        ),
    ]
    if spec.population.include_inactive:
        steps.append(
            PopulationJoin(
                entity_source=entity_source.name,
                entity_table=entity_source.table,
                entity_key=entity.key,
                zero_fill=tuple(m.name for m in measures),
            )
        )
    return Plan(
        asset_id=spec.asset_id,
        spec_version=spec.spec_version,
        semantic_fingerprint=semantic.fingerprint(),
        steps=tuple(steps),
    )


def dependencies(plan: Plan) -> tuple[Dependency, ...]:
    """Every source a plan reads, with the semantic fingerprint it assumed."""
    names: list[str] = []
    for step in plan.steps:
        match step:
            case SourceScan():
                names.append(step.source)
            case IdentityJoin():
                names.append(step.mapping_source)
            case PopulationJoin():
                names.append(step.entity_source)
            case _:
                pass
    return tuple(
        Dependency(name=n, kind="source", version=plan.semantic_fingerprint) for n in names
    )


def refresh_rule(spec: AssetSpec) -> RefreshRule:
    """Derive when an implementation must do work from the spec's window and service terms.

    The correction lookback equals the window span, because a late fact anywhere in the window
    changes the result.
    """
    return RefreshRule(
        incorporate_within_seconds=int(spec.service.incorporate_within.total_seconds()),
        boundary_timezone=spec.window.timezone,
        roll_at_day_boundary=True,
        correction_lookback_days=spec.window.span_days,
    )


def implementation_id(asset_id: str, spec_version: int, ordinal: int) -> str:
    """A valid SQL identifier naming one implementation of one spec version."""
    return f"{asset_id}__v{spec_version}__i{ordinal}"
