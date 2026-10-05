"""Turn MetricFlow semantic models into veena's semantic model, recording every gap."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veena.refs import SemanticRef
from veena.semantic.model import (
    Calculation,
    Entity,
    IdentityPolicy,
    SemanticModel,
    Source,
    TimeRule,
)

if TYPE_CHECKING:
    from veena.importer.manifest import DbtProject

_ADDITIVE = {"sum", "count", "sum_boolean"}
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Gap(BaseModel):
    """Something MetricFlow says that the semantic model cannot hold, and the asset it concerns."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    text: str


class SemanticImport(BaseModel):
    """The imported model plus everything that could not be represented."""

    model_config = ConfigDict(frozen=True)

    model: SemanticModel
    gaps: tuple[Gap, ...]


def semantic_from_project(project: DbtProject) -> SemanticImport:
    """Map semantic models to sources, entities, calculations, time rules, and identity policies.

    Primary entities become entities on the model's relation. Additive measures become
    calculations. The aggregation time dimension becomes the time rule. A foreign entity on a
    model with measures becomes a direct identity policy: the fact already carries the key.
    Anything else is a gap for a person.
    """
    gaps: list[Gap] = []
    sources: list[Source] = []
    entities: list[Entity] = []
    calculations: list[Calculation] = []
    time_rules: list[TimeRule] = []
    policies: list[IdentityPolicy] = []
    primary_entities: dict[str, str] = {}

    for sm in project.semantic_models.values():
        node = project.models.get(sm.model)
        if node is None:
            gaps.append(
                Gap(
                    asset_id=sm.model,
                    text=f"semantic model {sm.name} refers to model {sm.model}, "
                    "which is not in the manifest",
                )
            )
            continue
        primary = next((e for e in sm.entities if e.type == "primary"), None)
        if primary is None:
            gaps.append(
                Gap(asset_id=sm.model, text=f"semantic model {sm.name} has no primary entity")
            )
            continue
        if not _COLUMN.match(primary.expr):
            gaps.append(
                Gap(
                    asset_id=sm.model,
                    text=f"entity {primary.name} on {sm.name} has expr {primary.expr!r}, which is "
                    "not a column name; veena keys entities by a column",
                )
            )
            continue
        columns = tuple(node.columns) or (primary.expr,)
        sources.append(
            Source(name=sm.model, table=node.relation, primary_key=(primary.expr,), columns=columns)
        )
        if primary.name in primary_entities:
            gaps.append(
                Gap(
                    asset_id=sm.model,
                    text=f"entity {primary.name} is primary on both "
                    f"{primary_entities[primary.name]} "
                    f"and {sm.model}; keeping the first",
                )
            )
        else:
            primary_entities[primary.name] = sm.model
            entities.append(
                Entity(
                    name=primary.name,
                    version=1,
                    source=sm.model,
                    key=primary.expr,
                    description=sm.description or node.description,
                )
            )
        time_column = ""
        if sm.measures:
            if not sm.agg_time_dimension:
                gaps.append(
                    Gap(
                        asset_id=sm.model,
                        text=f"semantic model {sm.name} has measures but no agg_time_dimension",
                    )
                )
            else:
                dim = next((d for d in sm.dimensions if d.name == sm.agg_time_dimension), None)
                time_column = dim.expr if dim else sm.agg_time_dimension
                time_rules.append(
                    TimeRule(
                        name=f"{sm.model}_{sm.agg_time_dimension}",
                        version=1,
                        source=sm.model,
                        timestamp_column=time_column,
                        timezone="UTC",
                    )
                )
        for measure in sm.measures:
            if measure.agg not in _ADDITIVE:
                gaps.append(
                    Gap(
                        asset_id=sm.model,
                        text=f"measure {measure.name} on {sm.name} uses agg {measure.agg}; veena "
                        "carries additive calculations only",
                    )
                )
                continue
            if measure.agg == "sum":
                expression = measure.expr
            elif measure.agg == "count":
                expression = f"CASE WHEN {measure.expr} IS NOT NULL THEN 1 ELSE 0 END"
            else:
                expression = f"CASE WHEN {measure.expr} THEN 1 ELSE 0 END"
            lowered = f"{measure.name} {measure.expr} {measure.description}".lower()
            unit = (
                "USD"
                if "usd" in lowered or "amount" in lowered or "revenue" in lowered
                else ("count" if measure.agg != "sum" else "units")
            )
            calculations.append(
                Calculation(
                    name=measure.name,
                    version=1,
                    source=sm.model,
                    expression=expression,
                    unit=unit,
                    description=measure.description,
                )
            )

    # Direct identity policies: a fact's foreign entity is a key some other model owns.
    for sm in project.semantic_models.values():
        if not sm.measures or sm.model not in {s.name for s in sources}:
            continue
        rule = next((r for r in time_rules if r.source == sm.model), None)
        for foreign in (e for e in sm.entities if e.type == "foreign"):
            if not _COLUMN.match(foreign.expr):
                gaps.append(
                    Gap(
                        asset_id=sm.model,
                        text=f"entity {foreign.name} on {sm.name} has expr {foreign.expr!r}, "
                        "which is not a column name",
                    )
                )
                continue
            if foreign.name not in primary_entities:
                gaps.append(
                    Gap(
                        asset_id=sm.model,
                        text=f"{sm.name} references entity {foreign.name}, "
                        "which no semantic model owns",
                    )
                )
                continue
            if rule is None:
                continue
            policies.append(
                IdentityPolicy(
                    name=f"{sm.model}_{foreign.name}_direct",
                    version=1,
                    kind="direct",
                    entity=SemanticRef(name=foreign.name, version=1),
                    fact_source=sm.model,
                    fact_key=foreign.expr,
                    fact_timestamp=rule.timestamp_column,
                    description=(
                        f"{sm.model} carries {foreign.name} as {foreign.expr}; "
                        "attribution happened upstream of this model"
                    ),
                )
            )
    for metric in project.metrics.values():
        if metric.type != "simple":
            owner_model = _metric_model(project, metric.numerator or metric.measure)
            gaps.append(
                Gap(
                    asset_id=owner_model,
                    text=f"metric {metric.name} is a {metric.type}; veena specs carry additive "
                    "measures, so this stays a derived definition for a person to place",
                )
            )
        elif metric.measure and metric.measure not in {c.name for c in calculations}:
            gaps.append(
                Gap(
                    asset_id=_metric_model(project, metric.measure),
                    text=f"metric {metric.name} uses measure {metric.measure}, "
                    "which was not imported",
                )
            )
    model = SemanticModel(
        sources=tuple(sources),
        entities=tuple(entities),
        calculations=tuple(calculations),
        time_rules=tuple(time_rules),
        identity_policies=tuple(policies),
    )
    return SemanticImport(model=model, gaps=tuple(gaps))


def _metric_model(project: DbtProject, name: str) -> str:
    """The dbt model behind a measure or simple metric name, or the first semantic model's."""
    for sm in project.semantic_models.values():
        if any(m.name == name for m in sm.measures):
            return sm.model
    metric = project.metrics.get(name)
    if metric is not None and metric.measure:
        return _metric_model(project, metric.measure)
    first = next(iter(project.semantic_models.values()), None)
    return first.model if first else ""
