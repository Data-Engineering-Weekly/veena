"""Triage of a failed check on a live asset, and the policy table that picks a repair strategy.

Triage sees structure only: the failing check, its magnitude, source progress, recent
deployments, and whether the semantic model or the interface changed. It never sees candidate
SQL, and this module never imports the planner, so triage stays independent of the author the
way verification does. A classifier nominates a cause class with a probability; `RepairPolicy`
decides what happens.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from veena.models.protocol import Classification, input_hash
from veena.policy import CauseClass, RepairPolicy, RepairStrategy

if TYPE_CHECKING:
    from veena.evidence import CheckResult
    from veena.models.protocol import Classifier
    from veena.registry.state import RegistryState
    from veena.semantic.model import SemanticModel

CAUSE_DESCRIPTIONS: dict[CauseClass, str] = {
    CauseClass.SOURCE_LAG: "A source has not delivered facts up to the position the asset "
    "promised; the engine did its work and the data is late.",
    CauseClass.IDENTITY_MAPPING_CHANGE: "A mapping between facts and entities changed after the "
    "asset was built, so facts attach to different entity instances now.",
    CauseClass.SCHEMA_DRIFT: "The interface's columns differ from what a stable commitment "
    "recorded.",
    CauseClass.LATE_CORRECTIONS: "Facts inside the window were added or corrected after the "
    "asset was built.",
    CauseClass.CALCULATION_DRIFT: "An approved calculation or time rule changed since the plan "
    "was compiled.",
    CauseClass.UNKNOWN: "None of the evidence points one way.",
}

_MAGNITUDE = re.compile(r"(\d+(?:\.\d+)?) keys differ, total discrepancy (\d+(?:\.\d+)?)")
_DIFFERENCE = re.compile(r"difference (-?\d+(?:\.\d+)?)")


class TriageInput(BaseModel):
    """What the triage classifier sees. No field carries SQL.

    `source_progress` is what the asset incorporated. `source_positions` is what each source
    held when the engine last looked. `magnitude` is the total discrepancy of a value check or
    the number of offending rows of a structural check.
    """

    model_config = ConfigDict(frozen=True)

    asset_id: str
    implementation_id: str
    failing_check: str
    check_grade: str
    structural: bool
    detail: str
    magnitude: float = Field(ge=0.0)
    source_progress: dict[str, datetime]
    source_positions: dict[str, datetime]
    recent_deployments: tuple[str, ...]
    semantic_changed: bool
    interface_columns_changed: bool
    uses_mapping_source: bool
    option_descriptions: dict[str, str] = Field(
        default_factory=lambda: {c.value: d for c, d in CAUSE_DESCRIPTIONS.items()}
    )


class Triage(BaseModel):
    """What the classifier said and what policy made of it."""

    model_config = ConfigDict(frozen=True)

    cause: CauseClass
    probability: float = Field(ge=0.0, le=1.0)
    strategy: RepairStrategy
    model_id: str
    input_hash: str
    alternatives: dict[str, float] = Field(default_factory=dict)

    def summary(self) -> str:
        """One clause for questions and proposal summaries."""
        return f"triage {self.cause} p={self.probability:.2f}, strategy {self.strategy}"


def magnitude_of(check: CheckResult) -> float:
    """The size of a failure as the check reported it, or zero when the detail has no number."""
    match = _MAGNITUDE.search(check.detail)
    if match:
        return float(match.group(2)) if not check.structural else float(match.group(1))
    match = _DIFFERENCE.search(check.detail)
    if match:
        return abs(float(match.group(1)))
    return 0.0


def build_triage_input(
    state: RegistryState,
    semantic: SemanticModel,
    asset_id: str,
    implementation_id: str,
    failing: CheckResult,
    *,
    as_of: datetime,
    lookback_days: int = 7,
) -> TriageInput:
    """Assemble the structured evidence for one failed check from the registry.

    Args:
        state: The projection to read from.
        semantic: The current semantic model, compared with the one the plan assumed.
        asset_id: The asset whose check failed.
        implementation_id: The implementation that was checked.
        failing: The first failing check.
        as_of: When the check ran.
        lookback_days: How far back a deployment counts as recent.
    """
    asset = state.assets[asset_id]
    impl = state.implementations[implementation_id]
    since = as_of - timedelta(days=lookback_days)
    related = {asset_id, *asset.depends_on}
    recent = tuple(
        d.deployment_id
        for d in sorted(state.deployments.values(), key=lambda d: d.seq)
        if d.effective_at >= since and related & set(d.activated)
    )
    columns_changed = any(
        state.commitments[c].schema_stable
        and state.commitments[c].interface_columns
        and state.commitments[c].interface_columns != impl.plan.output_columns
        for c in asset.commitments
    )
    uses_mapping = False
    if asset.spec is not None:
        policy = semantic.identity_policy(asset.spec.identity_policy)
        uses_mapping = bool(policy.mapping_source)
    return TriageInput(
        asset_id=asset_id,
        implementation_id=implementation_id,
        failing_check=failing.name,
        check_grade=failing.grade.value,
        structural=failing.structural,
        detail=failing.detail,
        magnitude=magnitude_of(failing),
        source_progress=dict(asset.source_progress),
        source_positions={s: p.position for s, p in state.source_positions.items()},
        recent_deployments=recent,
        semantic_changed=impl.plan.semantic_fingerprint != semantic.fingerprint(),
        interface_columns_changed=columns_changed,
        uses_mapping_source=uses_mapping,
    )


def triage(context: TriageInput, classifier: Classifier, policy: RepairPolicy) -> Triage:
    """Ask the classifier for a cause class and let the policy table pick the strategy."""
    result = classifier.classify(context, CauseClass)
    return Triage(
        cause=result.value,
        probability=result.probability,
        strategy=policy.strategy(result.value, result.probability),
        model_id=result.model_id,
        input_hash=input_hash(context, CauseClass),
        alternatives={v.value: p for v, p in result.alternatives},
    )


class RuleTriageClassifier:
    """The offline baseline for the triage call site: fixed rules over the structured input.

    Its probabilities are hand-set, not calibrated, and the calibration report says so. A real
    classifier replaces it through the same protocol.
    """

    model_id = "rules"

    def classify[T: StrEnum](self, context: BaseModel, options: type[T]) -> Classification[T]:
        """Pick a cause class from the evidence, or abstain on any other context."""
        members = list(options)
        if not isinstance(context, TriageInput):
            uniform = round(1.0 / len(members), 4)
            return Classification(
                value=members[0],
                probability=uniform,
                alternatives=tuple((m, uniform) for m in members[1:]),
                model_id=self.model_id,
            )
        cause, probability = _rule(context)
        ranked = _spread(cause, probability, members)
        return Classification(
            value=ranked[0][0],
            probability=ranked[0][1],
            alternatives=tuple(ranked[1:]),
            model_id=self.model_id,
        )


def _rule(context: TriageInput) -> tuple[CauseClass, float]:
    if context.structural and context.interface_columns_changed:
        return CauseClass.SCHEMA_DRIFT, 0.8
    behind = any(
        position > context.source_progress.get(source, position)
        for source, position in context.source_positions.items()
    )
    value_check = context.failing_check.startswith(("reconcile", "conserve"))
    if behind and value_check:
        return CauseClass.SOURCE_LAG, 0.75
    if context.semantic_changed:
        return CauseClass.CALCULATION_DRIFT, 0.7
    if context.failing_check.startswith("reconcile") and context.uses_mapping_source:
        return CauseClass.IDENTITY_MAPPING_CHANGE, 0.75
    if context.failing_check.startswith("conserve"):
        # Facts counted twice or dropped: a mapping overlap or a late fact, the rules cannot say.
        return CauseClass.IDENTITY_MAPPING_CHANGE, 0.55
    return CauseClass.UNKNOWN, 0.5


def _spread[T: StrEnum](
    cause: CauseClass, probability: float, members: list[T]
) -> list[tuple[T, float]]:
    rest = [m for m in members if m.value != cause.value]
    share = round((1.0 - probability) / len(rest), 4) if rest else 0.0
    chosen = next(m for m in members if m.value == cause.value)
    return [(chosen, round(probability, 4)), *((m, share) for m in rest)]


__all__ = [
    "CAUSE_DESCRIPTIONS",
    "RuleTriageClassifier",
    "Triage",
    "TriageInput",
    "build_triage_input",
    "magnitude_of",
    "triage",
]
