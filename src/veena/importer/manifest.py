"""Read the parts of a dbt manifest that veena uses, into typed records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from veena.errors import VeenaError

if TYPE_CHECKING:
    from pathlib import Path


class ManifestError(VeenaError):
    """A manifest or history file cannot be read."""


class DbtModel(BaseModel):
    """One dbt model node."""

    model_config = ConfigDict(frozen=True)

    unique_id: str
    name: str
    relation: str
    database: str
    schema_name: str
    materialized: str
    owner: str = ""
    description: str = ""
    columns: dict[str, str] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()
    checksum: str = ""
    raw_code: str = ""
    compiled_code: str = ""
    depends_on_models: tuple[str, ...] = ()
    depends_on_sources: tuple[str, ...] = ()
    meta: dict[str, Any] = Field(default_factory=dict)


class DbtSource(BaseModel):
    """One dbt source table."""

    model_config = ConfigDict(frozen=True)

    unique_id: str
    name: str
    source_name: str
    relation: str
    columns: dict[str, str] = Field(default_factory=dict)
    description: str = ""


class DbtExposure(BaseModel):
    """One dbt exposure: a downstream consumer the project knows about."""

    model_config = ConfigDict(frozen=True)

    unique_id: str
    name: str
    type: str
    owner_name: str = ""
    owner_email: str = ""
    description: str = ""
    url: str = ""
    depends_on_models: tuple[str, ...] = ()
    meta: dict[str, Any] = Field(default_factory=dict)


class DbtEntity(BaseModel):
    """A MetricFlow entity on a semantic model."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    expr: str


class DbtMeasure(BaseModel):
    """A MetricFlow measure."""

    model_config = ConfigDict(frozen=True)

    name: str
    agg: str
    expr: str
    description: str = ""


class DbtDimension(BaseModel):
    """A MetricFlow dimension."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    expr: str
    granularity: str = ""


class DbtSemanticModel(BaseModel):
    """A MetricFlow semantic model."""

    model_config = ConfigDict(frozen=True)

    name: str
    model: str
    description: str = ""
    agg_time_dimension: str = ""
    entities: tuple[DbtEntity, ...] = ()
    dimensions: tuple[DbtDimension, ...] = ()
    measures: tuple[DbtMeasure, ...] = ()


class DbtMetric(BaseModel):
    """A MetricFlow metric."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    description: str = ""
    measure: str = ""
    numerator: str = ""
    denominator: str = ""


class DbtProject(BaseModel):
    """What veena reads from a manifest."""

    model_config = ConfigDict(frozen=True)

    project_name: str
    adapter_type: str
    generated_at: datetime
    models: dict[str, DbtModel]
    sources: dict[str, DbtSource]
    exposures: dict[str, DbtExposure]
    semantic_models: dict[str, DbtSemanticModel]
    metrics: dict[str, DbtMetric]

    def model_by_relation(self) -> dict[str, DbtModel]:
        """Models keyed by lower-cased fully qualified relation name."""
        return {m.relation.lower(): m for m in self.models.values()}


def load_manifest(path: Path) -> DbtProject:
    """Read a manifest.json file.

    Raises:
        ManifestError: The file is missing or not a dbt manifest.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"cannot read manifest {path}: {exc}"
        raise ManifestError(msg) from exc
    return parse_manifest(raw)


def parse_manifest(raw: Any) -> DbtProject:
    """Parse an already-decoded manifest dict.

    Raises:
        ManifestError: Required sections are missing.
    """
    if not isinstance(raw, dict) or "nodes" not in raw or "metadata" not in raw:
        msg = "manifest needs 'metadata' and 'nodes'"
        raise ManifestError(msg)
    metadata = raw["metadata"]
    models: dict[str, DbtModel] = {}
    for uid, node in raw["nodes"].items():
        if node.get("resource_type") != "model":
            continue
        depends = node.get("depends_on", {}).get("nodes", [])
        models[node["name"]] = DbtModel(
            unique_id=uid,
            name=node["name"],
            relation=node.get("relation_name")
            or f"{node.get('database')}.{node.get('schema')}.{node.get('alias') or node['name']}",
            database=str(node.get("database", "")),
            schema_name=str(node.get("schema", "")),
            materialized=str(node.get("config", {}).get("materialized", "")),
            owner=str(
                node.get("meta", {}).get("owner")
                or node.get("config", {}).get("meta", {}).get("owner")
                or ""
            ),
            description=str(node.get("description", "")),
            columns={c: str(v.get("data_type") or "") for c, v in node.get("columns", {}).items()},
            tags=tuple(node.get("tags", [])),
            checksum=str(node.get("checksum", {}).get("checksum", "")),
            raw_code=str(node.get("raw_code", "")),
            compiled_code=str(node.get("compiled_code") or node.get("raw_code", "")),
            depends_on_models=tuple(d.split(".")[-1] for d in depends if d.startswith("model.")),
            depends_on_sources=tuple(d.split(".")[-1] for d in depends if d.startswith("source.")),
            meta=dict(node.get("meta", {})),
        )
    sources: dict[str, DbtSource] = {}
    for uid, src in raw.get("sources", {}).items():
        sources[src["name"]] = DbtSource(
            unique_id=uid,
            name=src["name"],
            source_name=str(src.get("source_name", "")),
            relation=src.get("relation_name")
            or f"{src.get('database')}.{src.get('schema')}.{src.get('identifier') or src['name']}",
            columns={c: str(v.get("data_type") or "") for c, v in src.get("columns", {}).items()},
            description=str(src.get("description", "")),
        )
    exposures: dict[str, DbtExposure] = {}
    for uid, exp in raw.get("exposures", {}).items():
        owner = exp.get("owner", {}) or {}
        exposures[exp["name"]] = DbtExposure(
            unique_id=uid,
            name=exp["name"],
            type=str(exp.get("type", "")),
            owner_name=str(owner.get("name", "")),
            owner_email=str(owner.get("email", "")),
            description=str(exp.get("description", "")),
            url=str(exp.get("url") or ""),
            depends_on_models=tuple(
                d.split(".")[-1]
                for d in exp.get("depends_on", {}).get("nodes", [])
                if d.startswith("model.")
            ),
            meta=dict(exp.get("meta", {})),
        )
    semantic_models: dict[str, DbtSemanticModel] = {}
    for sm in raw.get("semantic_models", {}).values():
        model_ref = str(sm.get("model", ""))
        model_name = model_ref[len("ref('") : -2] if model_ref.startswith("ref('") else model_ref
        semantic_models[sm["name"]] = DbtSemanticModel(
            name=sm["name"],
            model=model_name,
            description=str(sm.get("description", "")),
            agg_time_dimension=str((sm.get("defaults") or {}).get("agg_time_dimension", "")),
            entities=tuple(
                DbtEntity(name=e["name"], type=e["type"], expr=str(e.get("expr") or e["name"]))
                for e in sm.get("entities", [])
            ),
            dimensions=tuple(
                DbtDimension(
                    name=d["name"],
                    type=d["type"],
                    expr=str(d.get("expr") or d["name"]),
                    granularity=str((d.get("type_params") or {}).get("time_granularity", "")),
                )
                for d in sm.get("dimensions", [])
            ),
            measures=tuple(
                DbtMeasure(
                    name=m["name"],
                    agg=str(m["agg"]),
                    expr=str(m.get("expr") or m["name"]),
                    description=str(m.get("description", "")),
                )
                for m in sm.get("measures", [])
            ),
        )
    metrics: dict[str, DbtMetric] = {}
    for mt in raw.get("metrics", {}).values():
        params = mt.get("type_params") or {}
        metrics[mt["name"]] = DbtMetric(
            name=mt["name"],
            type=str(mt.get("type", "")),
            description=str(mt.get("description", "")),
            measure=str((params.get("measure") or {}).get("name", "")),
            numerator=str((params.get("numerator") or {}).get("name", "")),
            denominator=str((params.get("denominator") or {}).get("name", "")),
        )
    generated = metadata.get("generated_at")
    try:
        generated_at = datetime.fromisoformat(str(generated)) if generated else datetime.now(UTC)
    except ValueError:
        generated_at = datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    return DbtProject(
        project_name=str(metadata.get("project_name", "")),
        adapter_type=str(metadata.get("adapter_type", "")),
        generated_at=generated_at,
        models=models,
        sources=sources,
        exposures=exposures,
        semantic_models=semantic_models,
        metrics=metrics,
    )
