"""Turn confirmed drafts into resolved specifications."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from veena.errors import ResolutionError
from veena.importer.infer import OPEN
from veena.policy import WorkspaceDefaults
from veena.refs import OriginRef, PolicyRef, SemanticRef
from veena.registry.events import DecisionRecorded, Event, SpecResolved
from veena.spec.model import (
    Aggregation,
    AssetSpec,
    Grain,
    Measure,
    Population,
    ServiceRequirements,
    TimeBucket,
    Window,
)

if TYPE_CHECKING:
    from veena.registry.registry import Registry
    from veena.semantic.model import SemanticModel


def build_spec_from_draft(
    asset_id: str,
    draft: dict[str, str],
    semantic: SemanticModel,
    *,
    origin: str,
    defaults: WorkspaceDefaults | None = None,
    spec_version: int = 1,
) -> AssetSpec:
    """Build a spec from a fully filled draft.

    Raises:
        ResolutionError: A slot is open or names something the semantic model lacks.
    """
    defaults = defaults or WorkspaceDefaults()
    open_slots = sorted(s for s, v in draft.items() if v == OPEN)
    if open_slots:
        msg = f"{asset_id}: draft has open slots {open_slots}"
        raise ResolutionError(msg)
    try:
        entity = next(e for e in semantic.entities if e.name == draft["population.entity"])
        calc = next(c for c in semantic.calculations if c.name == draft["measures.calculation"])
        policy = next(p for p in semantic.identity_policies if p.name == draft["identity_policy"])
    except (StopIteration, KeyError) as exc:
        msg = f"{asset_id}: draft names a definition the semantic model lacks: {exc}"
        raise ResolutionError(msg) from exc
    bucket = TimeBucket(draft.get("grain.time_bucket", "none"))
    source = OriginRef.model_validate(origin)
    policy_origin = OriginRef.model_validate(defaults.ref)
    if bucket is TimeBucket.NONE:
        window = Window(
            complete_calendar_days=int(draft["window.complete_calendar_days"]),
            timezone=defaults.timezone,
        )
    else:
        window = Window(history_days=int(draft["window.history_days"]), timezone=defaults.timezone)
    provenance = {
        "population.entity": source,
        "measures.calculation": source,
        "identity_policy": source,
        "grain.time_bucket": source,
        "population.include_inactive": source,
        "window.timezone": policy_origin,
        "service.incorporate_within": policy_origin,
        "service.access_policy": policy_origin,
        "service.budget_policy": policy_origin,
    }
    provenance[
        "window.complete_calendar_days" if bucket is TimeBucket.NONE else "window.history_days"
    ] = source
    entity_ref = SemanticRef(name=entity.name, version=entity.version)
    return AssetSpec(
        asset_id=asset_id,
        spec_version=spec_version,
        population=Population(
            entity=entity_ref,
            include_inactive=draft.get("population.include_inactive", "true") == "true",
        ),
        grain=Grain(keys=(entity_ref,), time_bucket=bucket),
        measures=(
            Measure(
                name=calc.name,
                calculation=SemanticRef(name=calc.name, version=calc.version),
                aggregation=Aggregation.SUM,
            ),
        ),
        window=window,
        identity_policy=SemanticRef(name=policy.name, version=policy.version),
        provenance=provenance,
        service=ServiceRequirements(
            incorporate_within=timedelta(seconds=defaults.incorporate_within_seconds),
            access_policy=PolicyRef.model_validate(defaults.access_policy),
            budget_policy=PolicyRef.model_validate(defaults.budget_policy),
        ),
    )


def confirm_drafts(
    registry: Registry,
    semantic: SemanticModel,
    *,
    above: float,
    actor: str,
    authority: str,
    defaults: WorkspaceDefaults | None = None,
    only: set[str] | None = None,
) -> list[str]:
    """Confirm every open inferred draft whose weakest slot is at or above `above`.

    A draft already answered `confirm_draft` by a person is resolved too. Each confirmation
    records a `DecisionRecorded` scoped to the asset and then a `SpecResolved`.

    Returns:
        Asset ids that received a spec.
    """
    state = registry.state
    resolved: list[str] = []
    for question in sorted(state.questions.values(), key=lambda q: q.seq):
        if question.slot != "inferred_spec":
            continue
        if only is not None and question.asset_id not in only:
            continue
        asset = state.assets.get(question.asset_id)
        if asset is None or asset.spec is not None:
            continue
        if any(v == OPEN for v in question.draft.values()):
            continue
        answered = question.decision_id is not None
        if answered and state.decisions[question.decision_id or ""].selected != "confirm_draft":
            continue
        weakest = min(question.draft_probabilities.values(), default=0.0)
        if not answered and weakest < above:
            continue
        decision_id = question.decision_id
        if decision_id is None:
            decision_id = f"{question.question_id}_d1"
            registry.append(
                Event(
                    actor=actor,
                    authority=authority,
                    causes=(f"question:{question.question_id}",),
                    payload=DecisionRecorded(
                        decision_id=decision_id,
                        question_id=question.question_id,
                        selected="confirm_draft",
                        rationale=f"every slot at or above {above:.2f}; weakest {weakest:.2f}",
                        scope=f"asset:{question.asset_id}",
                    ),
                )
            )
        spec = build_spec_from_draft(
            question.asset_id,
            question.draft,
            semantic,
            origin=f"decision:{decision_id}",
            defaults=defaults,
        )
        registry.append(
            Event(
                actor=actor,
                authority=authority,
                causes=(f"decision:{decision_id}",),
                payload=SpecResolved(spec=spec),
            )
        )
        resolved.append(question.asset_id)
    return resolved
