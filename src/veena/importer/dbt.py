"""Record a dbt project as observed state: assets, dependencies, and exposure commitments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

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

    Re-importing the same manifest changes nothing: a model with an unchanged checksum is
    skipped, and an existing commitment is kept.
    """
    defaults = defaults or WorkspaceDefaults()
    state = registry.state
    added = updated = unchanged = 0
    authority = f"import:{project.project_name}"
    for model in project.models.values():
        existing = state.assets.get(model.name)
        if existing is not None and existing.observed is not None:
            if existing.observed.checksum == model.checksum:
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
                payload=AssetObserved(
                    asset_id=model.name,
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
                    depends_on_assets=tuple(
                        d for d in model.depends_on_models if d in project.models
                    ),
                    depends_on_sources=model.depends_on_sources,
                ),
            )
        )
    commitments_added = commitments_unchanged = 0
    state = registry.state
    for exposure in project.exposures.values():
        consumer = f"exposure:{exposure.name}"
        sla_minutes = exposure.meta.get("sla_minutes")
        within = int(sla_minutes) * 60 if sla_minutes else defaults.incorporate_within_seconds
        for model_name in exposure.depends_on_models:
            if model_name not in state.assets:
                continue
            commitment_id = f"{model_name}:{consumer}"
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
                        asset_id=model_name,
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
        commitments_added=commitments_added,
        commitments_unchanged=commitments_unchanged,
    )
