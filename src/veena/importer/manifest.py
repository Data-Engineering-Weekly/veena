"""Read the parts of a dbt manifest that veena uses, into typed records.

Models, seeds, and snapshots are physical relations and become `DbtModel` records. Ephemeral
models have no relation and are flagged. Versioned models are keyed `name_vN` so every version
is kept. Sources are keyed `source_name.table` because table names repeat across sources.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from veena.errors import ManifestError

if TYPE_CHECKING:
    from pathlib import Path

_PHYSICAL = frozenset({"model", "seed", "snapshot"})


class DbtModel(BaseModel):
    """One dbt model, seed, or snapshot node."""

    model_config = ConfigDict(frozen=True)

    unique_id: str
    name: str
    key: str = Field(description="Registry asset id: the name, or name_vN for a versioned model")
    resource_type: str = "model"
    version: str = ""
    relation: str
    database: str
    schema_name: str
    materialized: str
    ephemeral: bool = False
    owner: str = ""
    description: str = ""
    columns: dict[str, str] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()
    checksum: str = ""
    raw_code: str = ""
    compiled_code: str = ""
    depends_on_models: tuple[str, ...] = Field(default_factory=tuple, description="Model keys")
    depends_on_sources: tuple[str, ...] = Field(
        default_factory=tuple, description="Source keys, source_name.table"
    )
    meta: dict[str, Any] = Field(default_factory=dict)


class DbtSource(BaseModel):
    """One dbt source table."""

    model_config = ConfigDict(frozen=True)

    unique_id: str
    name: str
    source_name: str
    key: str
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
    model: str = Field(description="Model key")
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
        """Physical models keyed by lower-cased fully qualified relation name."""
        return {m.relation.lower(): m for m in self.models.values() if not m.ephemeral}


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
        ManifestError: Required sections are missing or a node is malformed.
    """
    if not isinstance(raw, dict) or "nodes" not in raw or "metadata" not in raw:
        msg = "manifest needs 'metadata' and 'nodes'"
        raise ManifestError(msg)
    metadata = raw["metadata"]
    nodes: dict[str, Any] = raw["nodes"]
    keys = _node_keys(nodes)
    source_keys = {
        uid: f"{src.get('source_name', 'source')}.{src.get('name', uid.split('.')[-1])}"
        for uid, src in raw.get("sources", {}).items()
    }

    models: dict[str, DbtModel] = {}
    for uid, node in nodes.items():
        if node.get("resource_type") not in _PHYSICAL:
            continue
        try:
            models[keys[uid]] = _model(uid, node, keys, source_keys)
        except (KeyError, TypeError, AttributeError) as exc:
            msg = f"manifest node {uid} is malformed: {exc!r}"
            raise ManifestError(msg) from exc
    sources: dict[str, DbtSource] = {}
    for uid, src in raw.get("sources", {}).items():
        try:
            key = source_keys[uid]
            sources[key] = DbtSource(
                unique_id=uid,
                name=str(src["name"]),
                source_name=str(src.get("source_name", "")),
                key=key,
                relation=_relation(src, str(src.get("identifier") or src["name"])),
                columns={
                    c: str((v or {}).get("data_type") or "")
                    for c, v in (src.get("columns") or {}).items()
                },
                description=str(src.get("description") or ""),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            msg = f"manifest source {uid} is malformed: {exc!r}"
            raise ManifestError(msg) from exc
    exposures: dict[str, DbtExposure] = {}
    for uid, exp in raw.get("exposures", {}).items():
        try:
            owner = exp.get("owner") or {}
            exposures[str(exp["name"])] = DbtExposure(
                unique_id=uid,
                name=str(exp["name"]),
                type=str(exp.get("type") or ""),
                owner_name=str(owner.get("name") or ""),
                owner_email=str(owner.get("email") or ""),
                description=str(exp.get("description") or ""),
                url=str(exp.get("url") or ""),
                depends_on_models=tuple(
                    keys[d] for d in (exp.get("depends_on") or {}).get("nodes", []) if d in keys
                ),
                meta=dict(exp.get("meta") or {}),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            msg = f"manifest exposure {uid} is malformed: {exc!r}"
            raise ManifestError(msg) from exc
    semantic_models: dict[str, DbtSemanticModel] = {}
    for uid, sm in raw.get("semantic_models", {}).items():
        try:
            semantic_models[str(sm["name"])] = _semantic_model(sm, keys)
        except (KeyError, TypeError, AttributeError) as exc:
            msg = f"manifest semantic model {uid} is malformed: {exc!r}"
            raise ManifestError(msg) from exc
    metrics: dict[str, DbtMetric] = {}
    for uid, mt in raw.get("metrics", {}).items():
        try:
            params = mt.get("type_params") or {}
            metrics[str(mt["name"])] = DbtMetric(
                name=str(mt["name"]),
                type=str(mt.get("type") or ""),
                description=str(mt.get("description") or ""),
                measure=_named(params.get("measure")),
                numerator=_named(params.get("numerator")),
                denominator=_named(params.get("denominator")),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            msg = f"manifest metric {uid} is malformed: {exc!r}"
            raise ManifestError(msg) from exc
    generated = metadata.get("generated_at")
    try:
        generated_at = datetime.fromisoformat(str(generated)) if generated else datetime.now(UTC)
    except ValueError:
        generated_at = datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    return DbtProject(
        project_name=str(metadata.get("project_name") or ""),
        adapter_type=str(metadata.get("adapter_type") or ""),
        generated_at=generated_at,
        models=models,
        sources=sources,
        exposures=exposures,
        semantic_models=semantic_models,
        metrics=metrics,
    )


def _node_keys(nodes: dict[str, Any]) -> dict[str, str]:
    """Registry asset id per physical node: the name, or name_vN when versioned."""
    keys: dict[str, str] = {}
    for uid, node in nodes.items():
        if node.get("resource_type") not in _PHYSICAL:
            continue
        name = str(node.get("name") or uid.split(".")[-1])
        version = node.get("version")
        keys[uid] = f"{name}_v{version}" if version not in (None, "") else name
    return keys


def _relation(node: dict[str, Any], alias: str) -> str:
    relation = node.get("relation_name")
    if relation:
        return str(relation).replace('"', "")
    database = node.get("database")
    schema = node.get("schema")
    parts = [str(p) for p in (database, schema, alias) if p]
    return ".".join(parts)


def _model(
    uid: str, node: dict[str, Any], keys: dict[str, str], source_keys: dict[str, str]
) -> DbtModel:
    config = node.get("config") or {}
    materialized = str(config.get("materialized") or node.get("resource_type") or "")
    ephemeral = materialized == "ephemeral"
    depends = (node.get("depends_on") or {}).get("nodes", []) or []
    meta = dict(node.get("meta") or {})
    owner = str(meta.get("owner") or (config.get("meta") or {}).get("owner") or "")
    name = str(node.get("name") or uid.split(".")[-1])
    return DbtModel(
        unique_id=uid,
        name=name,
        key=keys[uid],
        resource_type=str(node.get("resource_type") or "model"),
        version=str(node.get("version") or ""),
        relation="" if ephemeral else _relation(node, str(node.get("alias") or name)),
        database=str(node.get("database") or ""),
        schema_name=str(node.get("schema") or ""),
        materialized=materialized,
        ephemeral=ephemeral,
        owner=owner,
        description=str(node.get("description") or ""),
        columns={
            c: str((v or {}).get("data_type") or "") for c, v in (node.get("columns") or {}).items()
        },
        tags=tuple(node.get("tags") or []),
        checksum=str((node.get("checksum") or {}).get("checksum") or ""),
        raw_code=str(node.get("raw_code") or node.get("raw_sql") or ""),
        compiled_code=str(
            node.get("compiled_code") or node.get("compiled_sql") or node.get("raw_code") or ""
        ),
        depends_on_models=tuple(keys[d] for d in depends if d in keys),
        depends_on_sources=tuple(source_keys[d] for d in depends if d in source_keys),
        meta=meta,
    )


def _semantic_model(sm: dict[str, Any], keys: dict[str, str]) -> DbtSemanticModel:
    model_key = ""
    relation = sm.get("node_relation") or {}
    depends = (sm.get("depends_on") or {}).get("nodes", []) or []
    for dep in depends:
        if dep in keys:
            model_key = keys[dep]
            break
    if not model_key:
        model_key = str(relation.get("alias") or _ref_name(str(sm.get("model") or "")))
    return DbtSemanticModel(
        name=str(sm["name"]),
        model=model_key,
        description=str(sm.get("description") or ""),
        agg_time_dimension=str((sm.get("defaults") or {}).get("agg_time_dimension") or ""),
        entities=tuple(
            DbtEntity(
                name=str(e["name"]),
                type=str(e.get("type") or ""),
                expr=str(e.get("expr") or e["name"]),
            )
            for e in sm.get("entities") or []
        ),
        dimensions=tuple(
            DbtDimension(
                name=str(d["name"]),
                type=str(d.get("type") or ""),
                expr=str(d.get("expr") or d["name"]),
                granularity=str((d.get("type_params") or {}).get("time_granularity") or ""),
            )
            for d in sm.get("dimensions") or []
        ),
        measures=tuple(
            DbtMeasure(
                name=str(m["name"]),
                agg=str(m.get("agg") or ""),
                expr=str(m.get("expr") or m["name"]),
                description=str(m.get("description") or ""),
            )
            for m in sm.get("measures") or []
        ),
    )


def _ref_name(text: str) -> str:
    """The model name inside `ref('x')`, `ref("x")`, `ref('pkg', 'x')`, or `ref('x', v=2)`."""
    inner = text.strip()
    if inner.startswith("ref(") and inner.endswith(")"):
        inner = inner[4:-1]
    parts = [p.strip().strip("'\"") for p in inner.split(",")]
    names = [p for p in parts if p and "=" not in p]
    return names[-1] if names else inner


def _named(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value)
