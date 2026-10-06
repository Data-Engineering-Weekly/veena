"""Record a dbt project as observed state: assets, dependencies, and exposure commitments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veena.errors import ManifestError
from veena.policy import WorkspaceDefaults
from veena.registry.events import AssetObserved, CommitmentRegistered, Event

if TYPE_CHECKING:
    from veena.importer.manifest import DbtProject
    from veena.registry.registry import Registry


class ImportReport(BaseModel):
    """What one import run changed."""

    model_config = ConfigDict(frozen=True)

    project: str
    assets_added: int
    assets_updated: int
    assets_unchanged: int
    assets_skipped: int = 0
    missing: tuple[str, ...] = ()
    commitments_added: int
    commitments_unchanged: int


def import_project(
    registry: Registry,
    project: DbtProject,
    *,
    defaults: WorkspaceDefaults | None = None,
    actor: str = "importer:dbt",
) -> ImportReport:
    """Record every model as an observed asset and every exposure as commitments.

    Re-importing the same manifest changes nothing: a model whose observed record is identical
    is skipped, and an existing commitment is kept. Ephemeral models have no relation and are
    skipped. Models seen before from this project but absent now are reported as missing.
    """
    defaults = defaults or WorkspaceDefaults()
    state = registry.state
    added = updated = unchanged = skipped = 0
    authority = f"import:{project.project_name}"
    origin_prefix = (
        f"dbt:{project.models[next(iter(project.models))].unique_id.split('.')[0]}."
        if project.models
        else "dbt:"
    )
    for model in project.models.values():
        if model.ephemeral:
            skipped += 1
            continue
        payload = AssetObserved(
            asset_id=model.key,
            origin=f"dbt:{model.unique_id}",
            relation=model.relation,
            layer=model.schema_name,
            materialized=model.materialized,
            owner=model.owner,
            description=model.description,
            columns=model.columns,
            tags=model.tags,
            checksum=model.checksum,
            sql=model.compiled_code,
            depends_on_assets=tuple(d for d in model.depends_on_models if d in project.models),
            depends_on_sources=model.depends_on_sources,
        )
        existing = state.assets.get(model.key)
        if existing is not None and existing.observed is not None:
            if existing.observed.model_dump() == payload.model_dump():
                unchanged += 1
                continue
            updated += 1
        else:
            added += 1
        registry.append(
            Event(
                actor=actor,
                authority=authority,
                causes=(f"dbt:{model.unique_id}",),
                payload=payload,
            )
        )
    seen_origins = {f"dbt:{m.unique_id}" for m in project.models.values()}
    missing = tuple(
        sorted(
            a.asset_id
            for a in state.assets.values()
            if a.observed is not None
            and a.observed.origin.startswith(origin_prefix)
            and a.observed.origin not in seen_origins
        )
    )
    commitments_added = commitments_unchanged = 0
    state = registry.state
    for exposure in project.exposures.values():
        consumer = f"exposure:{exposure.name}"
        sla_minutes = exposure.meta.get("sla_minutes")
        if sla_minutes is None:
            within = defaults.incorporate_within_seconds
        else:
            try:
                within = int(sla_minutes) * 60
            except (TypeError, ValueError) as exc:
                msg = (
                    f"exposure {exposure.name} has meta.sla_minutes {sla_minutes!r}, not an integer"
                )
                raise ManifestError(msg) from exc
        for model_key in exposure.depends_on_models:
            if model_key not in state.assets:
                continue
            commitment_id = f"{model_key}:{consumer}"
            if commitment_id in state.commitments:
                commitments_unchanged += 1
                continue
            registry.append(
                Event(
                    actor=actor,
                    authority=authority,
                    causes=(f"dbt:{exposure.unique_id}",),
                    payload=CommitmentRegistered(
                        commitment_id=commitment_id,
                        asset_id=model_key,
                        consumer=consumer,
                        incorporate_within_seconds=within,
                    ),
                )
            )
            commitments_added += 1
    return ImportReport(
        project=project.project_name,
        assets_added=added,
        assets_updated=updated,
        assets_unchanged=unchanged,
        assets_skipped=skipped,
        missing=missing,
        commitments_added=commitments_added,
        commitments_unchanged=commitments_unchanged,
    )
