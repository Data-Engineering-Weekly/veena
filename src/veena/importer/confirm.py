"""Turn confirmed drafts into resolved specifications."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import ValidationError

from veena.errors import ResolutionError
from veena.importer.infer import DEFAULT_INCLUDE_INACTIVE, OPEN
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
        ResolutionError: A slot is open, names something the semantic model lacks, or holds a
            value the spec cannot take.
    """
    defaults = defaults or WorkspaceDefaults()
    open_slots = sorted(s for s, v in draft.items() if v == OPEN)
    if open_slots:
        msg = f"{asset_id}: draft has open slots {open_slots}"
        raise ResolutionError(msg)
    entity_name = draft.get("population.entity", "")
    calc_name = draft.get("measures.calculation", "")
    policy_name = draft.get("identity_policy", "")
    entity = next((e for e in semantic.entities if e.name == entity_name), None)
    calc = next((c for c in semantic.calculations if c.name == calc_name), None)
    policy = next((p for p in semantic.identity_policies if p.name == policy_name), None)
    for label, value, found in (
        ("entity", entity_name, entity),
        ("calculation", calc_name, calc),
        ("identity policy", policy_name, policy),
    ):
        if found is None:
            msg = f"{asset_id}: draft names {label} {value!r}, which the semantic model lacks"
            raise ResolutionError(msg)
    assert entity is not None and calc is not None and policy is not None
    try:
        bucket = TimeBucket(draft.get("grain.time_bucket", "none"))
    except ValueError as exc:
        msg = f"{asset_id}: draft has time bucket {draft.get('grain.time_bucket')!r}"
        raise ResolutionError(msg) from exc
    days_key = (
        "window.complete_calendar_days" if bucket is TimeBucket.NONE else "window.history_days"
    )
    try:
        days = int(draft[days_key])
    except (KeyError, ValueError) as exc:
        msg = f"{asset_id}: draft needs an integer {days_key}, got {draft.get(days_key)!r}"
        raise ResolutionError(msg) from exc
    include_inactive = (
        draft.get("population.include_inactive", DEFAULT_INCLUDE_INACTIVE).lower() == "true"
    )
    source = OriginRef.model_validate(origin)
    policy_origin = OriginRef.model_validate(defaults.ref)
    provenance = {
        "population.entity": source,
        "measures.calculation": source,
        "identity_policy": source,
        "grain.time_bucket": source,
        "population.include_inactive": source,
        days_key: source,
        "window.timezone": policy_origin,
        "service.incorporate_within": policy_origin,
        "service.access_policy": policy_origin,
        "service.budget_policy": policy_origin,
    }
    entity_ref = SemanticRef(name=entity.name, version=entity.version)
    try:
        window = (
            Window(complete_calendar_days=days, timezone=defaults.timezone)
            if bucket is TimeBucket.NONE
            else Window(history_days=days, timezone=defaults.timezone)
        )
        return AssetSpec(
            asset_id=asset_id,
            spec_version=spec_version,
            population=Population(entity=entity_ref, include_inactive=include_inactive),
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
    except ValidationError as exc:
        msg = f"{asset_id}: draft does not make a valid specification: {exc.errors()[0]['msg']}"
        raise ResolutionError(msg) from exc


def confirm_drafts(
    registry: Registry,
    semantic: SemanticModel,
    *,
    above: float,
    actor: str = "engine",
    authority: str = "policy:resolution@1",
    defaults: WorkspaceDefaults | None = None,
    only: set[str] | None = None,
) -> list[str]:
    """Confirm every open inferred draft whose weakest slot is at or above `above`.

    A draft already answered `confirm_draft` by a person is resolved too. The spec is built
    first; only then are the `DecisionRecorded` (for threshold confirmations) and `SpecResolved`
    appended, so a draft that cannot build leaves no trace of approval.

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
        decision_id = question.decision_id or f"{question.question_id}_d1"
        spec = build_spec_from_draft(
            question.asset_id,
            question.draft,
            semantic,
            origin=f"decision:{decision_id}",
            defaults=defaults,
        )
        if question.decision_id is None:
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
