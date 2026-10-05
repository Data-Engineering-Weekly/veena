"""Infer a draft specification for each observed asset and record it as a question.

The draft is never a resolved spec. A person confirms it, or `confirm_drafts` does when every
slot is filled with a probability above the policy threshold.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veena.models.protocol import closed_set, input_hash
from veena.registry.events import ClassificationProduced, DraftProduced, Event, QuestionOpened
from veena.registry.state import AssetStatus
from veena.resolver import SITE_RESOLVE, SlotContext

if TYPE_CHECKING:
    from collections.abc import Iterable

    from veena.models.protocol import Classifier, Drafter
    from veena.registry.registry import Registry
    from veena.semantic.model import SemanticModel

SITE_INFER = "infer_spec"
OPEN = "open"

_WINDOW_NAME = re.compile(r"(?:^|_)(?:last_|trailing_)?(\d+)d(?:_|$)|(\d+)_days?\b", re.I)
_WINDOW_SQL = re.compile(r"dateadd\s*\(\s*day\s*,\s*-\s*(\d+)|interval\s+'(\d+)\s+day", re.I)
_BUCKET = {"daily": "day", "_day": "day", "monthly": "month", "_month": "month"}


class InferredDraft(BaseModel):
    """One asset's inferred requirements and how sure the engine is about each."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    question_id: str
    slots: dict[str, str]
    probabilities: dict[str, float]
    open_slots: tuple[str, ...]

    @property
    def min_probability(self) -> float:
        """The least confident filled slot, or 0.0 when a slot is open."""
        filled = [p for s, p in self.probabilities.items() if self.slots.get(s) != OPEN]
        return 0.0 if self.open_slots or not filled else min(filled)


class _Draft:
    """Mutable working state for one asset's inference."""

    def __init__(
        self,
        *,
        asset_id: str,
        text: str,
        sql_excerpt: str,
        slots: dict[str, str],
        probabilities: dict[str, float],
        evidence: list[str],
    ) -> None:
        self.asset_id = asset_id
        self.text = text
        self.sql_excerpt = sql_excerpt
        self.slots = slots
        self.probabilities = probabilities
        self.evidence = evidence


def _pick(
    registry: Registry,
    classifier: Classifier,
    draft: _Draft,
    slot: str,
    options: dict[str, str],
    actor: str,
    authority: str,
) -> None:
    """Fill one slot from a closed set and record the classifier call."""
    if not options:
        draft.slots[slot] = OPEN
        draft.probabilities[slot] = 0.0
        draft.evidence.append(f"{slot} open: the semantic model defines no options")
        return
    enum = closed_set(sorted(options))
    context = SlotContext(
        slot=slot, text=draft.text + " " + draft.sql_excerpt, option_descriptions=options
    )
    result = classifier.classify(context, enum)
    registry.append(
        Event(
            actor=actor,
            authority=authority,
            causes=(f"asset:{draft.asset_id}",),
            payload=ClassificationProduced(
                site=SITE_INFER,
                slot=slot,
                asset_id=draft.asset_id,
                model_id=result.model_id,
                input_hash=input_hash(context, enum),
                value=result.value.value,
                probability=result.probability,
                alternatives={v.value: p for v, p in result.alternatives},
            ),
        )
    )
    draft.slots[slot] = result.value.value
    draft.probabilities[slot] = result.probability
    draft.evidence.append(f"{slot} = {result.value.value} (p={result.probability:.2f})")


class RationaleInput(BaseModel):
    """What the drafter receives to explain a draft. Structured slots, never a verdict."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    description: str
    slots: dict[str, str]
    probabilities: dict[str, float]
    sql_excerpt: str


def infer_drafts(
    registry: Registry,
    semantic: SemanticModel,
    classifier: Classifier,
    *,
    drafter: Drafter | None = None,
    only: Iterable[str] | None = None,
    actor: str = "engine",
    authority: str = "policy:resolution@1",
) -> list[InferredDraft]:
    """Open one `inferred_spec` question per observed asset that has none yet.

    Deterministic slots (window, bucket, inactive members, identity policy) get probability 1.0.
    Classified slots (entity, calculation) carry the classifier's probability. A slot the engine
    cannot fill is `open` with a reason in the evidence.
    """
    state = registry.state
    wanted = set(only) if only is not None else None
    drafts: list[InferredDraft] = []
    pending = {
        q.asset_id
        for q in state.questions.values()
        if q.slot == "inferred_spec" and q.decision_id is None
    }
    entities = {e.name: e.description or e.name for e in semantic.entities}
    calculations = {c.name: c.description or c.name for c in semantic.calculations}
    for asset in sorted(state.assets.values(), key=lambda a: a.asset_id):
        if (
            asset.observed is None
            or asset.spec is not None
            or asset.status is not AssetStatus.OBSERVED
        ):
            continue
        if asset.asset_id in pending or (wanted is not None and asset.asset_id not in wanted):
            continue
        observed = asset.observed
        text = f"{asset.asset_id} {observed.description} columns: {', '.join(observed.columns)}"
        sql = observed.sql.lower()
        slots: dict[str, str] = {}
        probabilities: dict[str, float] = {}
        evidence: list[str] = []

        if not re.search(r"\bgroup\s+by\b", sql) and not re.search(r"\b(sum|count)\s*\(", sql):
            slots["shape"] = OPEN
            probabilities["shape"] = 0.0
            evidence.append(
                "shape open: no aggregation in the SQL; the spec IR describes aggregates only"
            )

        draft = _Draft(
            asset_id=asset.asset_id,
            text=text,
            sql_excerpt=observed.sql[:400],
            slots=slots,
            probabilities=probabilities,
            evidence=evidence,
        )
        _pick(registry, classifier, draft, "population.entity", entities, actor, authority)
        chosen_entity = next(
            (e for e in semantic.entities if e.name == slots["population.entity"]), None
        )
        if (
            chosen_entity is not None
            and observed.columns
            and chosen_entity.key not in observed.columns
        ):
            evidence[-1] = (
                f"population.entity open: the classifier picked {chosen_entity.name} "
                f"(p={probabilities['population.entity']:.2f}) but the output has no "
                f"{chosen_entity.key} column; the grain is something else"
            )
            slots["population.entity"] = OPEN
            probabilities["population.entity"] = 0.0
        _pick(registry, classifier, draft, "measures.calculation", calculations, actor, authority)

        bucket = next((b for k, b in _BUCKET.items() if k in asset.asset_id.lower()), "none")
        slots["grain.time_bucket"] = bucket
        probabilities["grain.time_bucket"] = 1.0
        evidence.append(f"grain.time_bucket = {bucket} (from the name)")

        days = _window_days(asset.asset_id, sql)
        if bucket == "none":
            slots["window.complete_calendar_days"] = str(days) if days else OPEN
            probabilities["window.complete_calendar_days"] = 1.0 if days else 0.0
            evidence.append(
                f"window.complete_calendar_days = {days} (from the SQL)"
                if days
                else "window.complete_calendar_days open: no trailing window in the name or SQL"
            )
        else:
            slots["window.history_days"] = str(days) if days else OPEN
            probabilities["window.history_days"] = 1.0 if days else 0.0
            evidence.append(
                f"window.history_days = {days}"
                if days
                else "window.history_days open: no retention stated"
            )

        inactive = bool(re.search(r"\bleft\s+join\b", sql) and "coalesce(" in sql)
        slots["population.include_inactive"] = str(inactive).lower()
        probabilities["population.include_inactive"] = 1.0
        evidence.append(
            f"population.include_inactive = {str(inactive).lower()} (left join with coalesce)"
            if inactive
            else "population.include_inactive = false (inner joins only)"
        )

        entity = slots.get("population.entity", OPEN)
        calc = slots.get("measures.calculation", OPEN)
        calc_def = next((c for c in semantic.calculations if c.name == calc), None)
        candidates = [
            p
            for p in semantic.identity_policies
            if p.entity.name == entity and (calc_def is None or p.fact_source == calc_def.source)
        ]
        if len(candidates) == 1:
            slots["identity_policy"] = candidates[0].name
            probabilities["identity_policy"] = 1.0
            evidence.append(
                f"identity_policy = {candidates[0].name} "
                f"(only policy attaching {calc_def.source if calc_def else '?'} to {entity})"
            )
        else:
            slots["identity_policy"] = OPEN
            probabilities["identity_policy"] = 0.0
            fact = calc_def.source if calc_def else "the facts"
            evidence.append(
                f"identity_policy open: {len(candidates)} policies attach {fact} to {entity}"
            )

        open_slots = tuple(sorted(s for s, v in slots.items() if v == OPEN))
        count = sum(1 for q in registry.state.questions.values() if q.asset_id == asset.asset_id)
        question_id = f"{asset.asset_id}_q{count + 1}"
        prose = (
            f"Imported {asset.asset_id} ({observed.layer}, {observed.materialized}, "
            f"owner {observed.owner or '-'}). Inferred: "
            + "; ".join(evidence)
            + ". Confirm, edit, or reject this draft."
        )
        if drafter is not None:
            rationale_input = RationaleInput(
                asset_id=asset.asset_id,
                description=observed.description,
                slots=slots,
                probabilities=probabilities,
                sql_excerpt=observed.sql[:600],
            )
            prose = drafter.draft(rationale_input)
            registry.append(
                Event(
                    actor=actor,
                    authority=authority,
                    causes=(f"asset:{asset.asset_id}",),
                    payload=DraftProduced(
                        site=SITE_INFER,
                        asset_id=asset.asset_id,
                        model_id=getattr(drafter, "model_id", type(drafter).__name__),
                        input_hash=input_hash(rationale_input),
                        text=prose,
                    ),
                )
            )
        registry.append(
            Event(
                actor=actor,
                authority=authority,
                causes=(f"asset:{asset.asset_id}",),
                payload=QuestionOpened(
                    question_id=question_id,
                    asset_id=asset.asset_id,
                    question=prose,
                    alternatives=("confirm_draft", "edit_draft", "reject_draft"),
                    slot="inferred_spec",
                    consumer=f"owner:{observed.owner}" if observed.owner else "",
                    evidence=tuple(evidence),
                    draft=slots,
                    draft_probabilities=probabilities,
                ),
            )
        )
        drafts.append(
            InferredDraft(
                asset_id=asset.asset_id,
                question_id=question_id,
                slots=slots,
                probabilities=probabilities,
                open_slots=open_slots,
            )
        )
    return drafts


def _window_days(name: str, sql: str) -> int | None:
    for match in _WINDOW_SQL.finditer(sql):
        value = match.group(1) or match.group(2)
        if value:
            return int(value)
    name_match = _WINDOW_NAME.search(name)
    if name_match:
        return int(name_match.group(1) or name_match.group(2))
    return None


__all__ = ["OPEN", "SITE_INFER", "SITE_RESOLVE", "InferredDraft", "RationaleInput", "infer_drafts"]
