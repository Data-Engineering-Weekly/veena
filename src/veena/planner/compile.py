"""Compile a specification into a plan. Deterministic; no model call anywhere."""

from __future__ import annotations

from typing import TYPE_CHECKING

from veena.errors import PlanError, SemanticRefError
from veena.planner.plan import (
    BUCKET_COLUMN,
    Aggregate,
    AssetScan,
    BucketWindow,
    Dependency,
    IdentityJoin,
    MeasurePlan,
    Plan,
    PopulationJoin,
    RefreshRule,
    Rollup,
    RollupMeasure,
    SourceScan,
    Step,
    TimeAttribution,
    WindowStep,
)
from veena.refs import SemanticRef
from veena.spec.model import Aggregation, TimeBucket
from veena.spec.subsumption import Operation, Verdict, check

if TYPE_CHECKING:
    from veena.semantic.model import SemanticModel
    from veena.spec.model import AssetSpec

ZERO_FILL = {Aggregation.SUM, Aggregation.COUNT}
_ROLLUP = {
    Aggregation.SUM: Aggregation.SUM,
    Aggregation.COUNT: Aggregation.SUM,
    Aggregation.MIN: Aggregation.MIN,
    Aggregation.MAX: Aggregation.MAX,
}


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
        mapping_source = (
            semantic.source(policy.mapping_source) if policy.kind == "point_in_time" else None
        )
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
    direct = policy.kind == "direct"
    if not direct and entity.key in fact_source.columns:
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
            mapping_source=mapping_source.name if mapping_source else "",
            mapping_table=mapping_source.table if mapping_source else "",
            fact_key=policy.fact_key,
            mapping_fact_key=policy.mapping_fact_key,
            mapping_entity_key=policy.mapping_entity_key,
            entity_key=entity.key,
            fact_timestamp=policy.fact_timestamp,
            valid_from=policy.valid_from,
            valid_to=policy.valid_to,
            direct=direct,
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


def compile_from_provider(
    spec: AssetSpec,
    provider: AssetSpec,
    provider_implementation: str,
    semantic: SemanticModel,
) -> Plan:
    """Build the consumer's plan on top of a provider asset that subsumption says can serve it.

    Args:
        spec: The consumer specification.
        provider: The provider's specification.
        provider_implementation: Implementation id the plan reads; recorded as the assumed version.
        semantic: The approved meaning, used to resolve the entity key and source.

    Returns:
        A plan that scans the provider, windows its buckets, rolls up, and joins the population.

    Raises:
        PlanError: Subsumption says the provider cannot serve, or needs an operation the
            compiler does not support yet.
    """
    result = check(provider, spec)
    if result.verdict is Verdict.CANNOT:
        msg = f"{provider.ref} cannot serve {spec.ref}: " + "; ".join(result.reasons)
        raise PlanError(msg)
    unsupported = set(result.operations) - {Operation.WINDOW, Operation.POPULATION_JOIN}
    if unsupported:
        msg = f"{spec.ref} from {provider.ref} needs {sorted(unsupported)}, not supported yet"
        raise PlanError(msg)
    if provider.grain.time_bucket not in BUCKET_COLUMN:
        msg = f"{provider.ref} has no time bucket to window on"
        raise PlanError(msg)
    if spec.window.complete_calendar_days is None:
        msg = f"{spec.ref} is not a windowed spec"
        raise PlanError(msg)
    try:
        entity = semantic.entity(spec.population.entity)
        entity_source = semantic.source(entity.source)
    except SemanticRefError as exc:
        msg = f"{spec.ref}: {exc}"
        raise PlanError(msg) from exc

    by_key = {(m.calculation, m.aggregation): m for m in provider.measures}
    rollups: list[RollupMeasure] = []
    for measure in spec.measures:
        source_measure = by_key[(measure.calculation, measure.aggregation)]
        rollups.append(
            RollupMeasure(name=source_measure.name, aggregation=_ROLLUP[measure.aggregation])
        )
    bucket = BUCKET_COLUMN[provider.grain.time_bucket]
    columns = (entity.key, bucket, *(m.name for m in provider.measures))
    steps: list[Step] = [
        AssetScan(
            asset_id=provider.asset_id, implementation_id=provider_implementation, columns=columns
        ),
        BucketWindow(
            bucket_column=bucket,
            complete_calendar_days=spec.window.complete_calendar_days,
            timezone=spec.window.timezone,
        ),
        Rollup(entity_key=entity.key, measures=tuple(rollups)),
    ]
    if spec.population.include_inactive:
        steps.append(
            PopulationJoin(
                entity_source=entity_source.name,
                entity_table=entity_source.table,
                entity_key=entity.key,
                zero_fill=tuple(m.name for m in rollups),
            )
        )
    return Plan(
        asset_id=spec.asset_id,
        spec_version=spec.spec_version,
        semantic_fingerprint=semantic.fingerprint(),
        steps=tuple(steps),
    )


def dependencies(plan: Plan) -> tuple[Dependency, ...]:
    """Every source and asset a plan reads, with the version it assumed."""
    deps: list[Dependency] = []
    for step in plan.steps:
        match step:
            case SourceScan():
                deps.append(
                    Dependency(name=step.source, kind="source", version=plan.semantic_fingerprint)
                )
            case AssetScan():
                deps.append(
                    Dependency(name=step.asset_id, kind="asset", version=step.implementation_id)
                )
            case IdentityJoin() if not step.direct:
                deps.append(
                    Dependency(
                        name=step.mapping_source, kind="source", version=plan.semantic_fingerprint
                    )
                )
            case PopulationJoin():
                deps.append(
                    Dependency(
                        name=step.entity_source, kind="source", version=plan.semantic_fingerprint
                    )
                )
            case _:
                pass
    return tuple(deps)


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
