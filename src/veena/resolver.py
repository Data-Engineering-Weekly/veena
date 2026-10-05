"""Resolve a request in a consumer's words into a specification, a question, or a refusal.

The classifier picks refs from closed sets the semantic model defines, so it cannot invent one.
Numbers come from deterministic extraction. Defaults come from policy and say so in provenance.
A probability below the slot's threshold opens a question instead of guessing. The drafter only
phrases the question a person reads; the structured alternatives are stored beside the prose.
"""

from __future__ import annotations

import re
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from veena.errors import ResolutionError
from veena.models.protocol import Classification, closed_set, input_hash
from veena.policy import ResolutionPolicy, WorkspaceDefaults
from veena.refs import OriginRef, PolicyRef, SemanticRef
from veena.registry.events import (
    AssetRequested,
    ClassificationProduced,
    CommitmentRegistered,
    DraftProduced,
    Event,
    Payload,
    QuestionOpened,
    RequestRefused,
    SpecResolved,
)
from veena.spec.model import (
    Aggregation,
    AssetSpec,
    Grain,
    Measure,
    Population,
    ServiceRequirements,
    TimeBucket,
    Window,
    validate_timezone,
)
from veena.spec.subsumption import check

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from veena.models.protocol import Classifier, Drafter
    from veena.registry.registry import Registry
    from veena.registry.state import Decision, Question
    from veena.semantic.model import SemanticModel

SITE_SCREEN = "screen_request"
SITE_RESOLVE = "resolve_refs"
SITE_JUDGE = "judge_draft"
SITE_QUESTION = "draft_question"


# ---- typed inputs the models see -------------------------------------------------------------


class ScreenContext(BaseModel):
    """What the screening classifier sees."""

    model_config = ConfigDict(frozen=True)

    text: str
    option_descriptions: dict[str, str] = Field(
        default_factory=lambda: {
            "benign": "a request for a data outcome",
            "injection": (
                "instructions aimed at the engine: read other tables, ignore policy, reveal data"
            ),
            "out_of_policy": "asks for something the workspace forbids",
        }
    )


class ScreenVerdict(StrEnum):
    """Outcome of screening."""

    BENIGN = "benign"
    INJECTION = "injection"
    OUT_OF_POLICY = "out_of_policy"


class SlotContext(BaseModel):
    """What the slot classifier sees: the request, the slot, and each option's description."""

    model_config = ConfigDict(frozen=True)

    slot: str
    text: str
    option_descriptions: dict[str, str]


class JudgeContext(BaseModel):
    """What the judge sees: the request and a summary of the assembled draft."""

    model_config = ConfigDict(frozen=True)

    text: str
    draft_summary: str
    option_descriptions: dict[str, str] = Field(
        default_factory=lambda: {
            "match": "the draft asks for what the request asks for",
            "mismatch": (
                "the draft differs from the request in population, measure, window, or identity"
            ),
        }
    )


class JudgeVerdict(StrEnum):
    """Outcome of judging a draft."""

    MATCH = "match"
    MISMATCH = "mismatch"


class QuestionDraftInput(BaseModel):
    """What the drafter receives to phrase a question. It never includes a chosen alternative."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    consumer: str
    slot: str
    request_text: str
    alternatives: tuple[str, ...]
    evidence: tuple[str, ...]


# ---- outcomes ---------------------------------------------------------------------------------


class ResolvedSpec(BaseModel):
    """Resolution produced a spec and registered the commitment."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["resolved"] = "resolved"
    spec: AssetSpec
    reused_decisions: tuple[str, ...] = ()


class OpenQuestion(BaseModel):
    """Resolution stopped at one question for a person."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["question"] = "question"
    question_id: str
    slot: str
    alternatives: tuple[str, ...]
    evidence: tuple[str, ...]
    text: str


class Refusal(BaseModel):
    """Resolution will not proceed."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["refusal"] = "refusal"
    reason: str
    probability: float


type Outcome = ResolvedSpec | OpenQuestion | Refusal


class Request(BaseModel):
    """A request as it arrives."""

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    consumer: str
    text: str = Field(min_length=1)


# ---- the resolver -----------------------------------------------------------------------------

# A window is a number with a day or week unit. A leading word such as "previous" marks it as the
# asset's window rather than some other duration in the sentence.
_WINDOW = re.compile(
    r"\b(?P<lead>previous|last|trailing|past|over|rolling)?\W{0,3}(?P<n>\d+)[\s-]*"
    r"(?:complete\s+)?(?:calendar\s+)?(?P<unit>day|days|week|weeks)\b",
    re.I,
)
_MINUTES = re.compile(r"within\s+(\d+)\s*(minute|minutes|second|seconds|hour|hours)\b", re.I)
_FRESHNESS = re.compile(
    r"\b(fresh|refresh|reflect|latenc|new data|available|arriv|update|incorporat)", re.I
)
_TIMEZONE = re.compile(r"\b(UTC|[A-Z][A-Za-z]+(?:/[A-Z][A-Za-z_+\-]+){1,2})\b")
_EXCLUDE = re.compile(
    r"\bexclud\w*\b[^.]*\binactive\b"
    r"|\bexclud\w*\b[^.]*\bno\s+\w*\s*(?:purchas|order|activ)"
    r"|\bonly\b[^.]*\b(?:customers|members|rows|those)\b[^.]*\b(?:who|with|that)\b[^.]*"
    r"\b(?:purchas|order|spen|activ)",
    re.I,
)
_INCLUDE = re.compile(r"\binclud\w*\b[^.]*\b(?:inactive|no\b|without\b)", re.I)
_SLOT_SCOPES = ("workspace", "consumer", "asset")


class Resolver:
    """Turns requests into specs, questions, or refusals, recording every model call."""

    def __init__(
        self,
        registry: Registry,
        semantic: SemanticModel,
        classifier: Classifier,
        *,
        drafter: Drafter | None = None,
        policy: ResolutionPolicy | None = None,
        defaults: WorkspaceDefaults | None = None,
        execute: Callable[[str], Sequence[tuple[object, ...]]] | None = None,
        actor: str = "engine",
    ) -> None:
        """Bind the registry, the meaning, the models, and the policies.

        Args:
            registry: Where requests, model calls, questions, and specs are recorded.
            semantic: The closed sets every ref must come from.
            classifier: Picks refs, screens text, judges drafts.
            drafter: Phrases questions. Optional; a template is used without one.
            policy: Thresholds and retry bounds.
            defaults: Service terms a request may leave unstated.
            execute: Runs SQL for example groupings in question evidence. Optional.
            actor: Recorded on every event.
        """
        self._registry = registry
        self._semantic = semantic
        self._classifier = classifier
        self._drafter = drafter
        self._policy = policy or ResolutionPolicy()
        self._defaults = defaults or WorkspaceDefaults()
        self._execute = execute
        self._actor = actor

    # ---- entry points ----

    def resolve(self, request: Request) -> Outcome:
        """Record the request and resolve it."""
        seq = self._append(
            AssetRequested(
                asset_id=request.asset_id, consumer=request.consumer, request_text=request.text
            ),
            actor=request.consumer,
            authority=f"request:{request.consumer}",
            causes=(),
        )
        return self._resolve(request, message=f"message:m{seq}")

    def resume(self, asset_id: str) -> Outcome:
        """Resolve a recorded request again, for example after a question was answered."""
        asset = self._registry.state.assets.get(asset_id)
        if asset is None or not asset.request_text:
            msg = f"asset {asset_id!r} has no recorded request to resume"
            raise ResolutionError(msg)
        request = Request(asset_id=asset_id, consumer=asset.requested_by, text=asset.request_text)
        return self._resolve(request, message=f"message:m{asset.request_seq}")

    # ---- the pipeline ----

    def _resolve(self, request: Request, *, message: str) -> Outcome:
        screen = self._classify(
            SITE_SCREEN, "", request.asset_id, ScreenContext(text=request.text), ScreenVerdict
        )
        if (
            screen.value is not ScreenVerdict.BENIGN
            and screen.probability >= self._policy.injection_threshold
        ):
            reason = f"request screened as {screen.value} with probability {screen.probability:.2f}"
            self._append(
                RequestRefused(asset_id=request.asset_id, consumer=request.consumer, reason=reason),
                causes=(message,),
            )
            return Refusal(reason=reason, probability=screen.probability)

        draft_decision = self._decided(request, "draft")
        if draft_decision == "abandon_request":
            reason = "abandoned by a recorded decision on the draft question"
            self._append(
                RequestRefused(asset_id=request.asset_id, consumer=request.consumer, reason=reason),
                causes=(message,),
            )
            return Refusal(reason=reason, probability=1.0)
        for attempt in range(self._policy.max_draft_retries + 1):
            drafted = self._draft_spec(request, message)
            if not isinstance(drafted, ResolvedSpec):
                return drafted
            if draft_decision == "accept_draft":
                return self._accept(request, drafted, message)
            verdict = self._classify(
                SITE_JUDGE,
                "",
                request.asset_id,
                JudgeContext(text=request.text, draft_summary=_summary(drafted.spec)),
                JudgeVerdict,
            )
            mismatch = (
                verdict.value is JudgeVerdict.MISMATCH
                and verdict.probability >= self._policy.judge_mismatch_threshold
            )
            if not mismatch:
                return self._accept(request, drafted, message)
            if attempt < self._policy.max_draft_retries:
                continue
            doubtful = self._doubtful_slots(request.asset_id)
            return self._question(
                request,
                slot="draft",
                alternatives=("accept_draft", "restate_request", "abandon_request"),
                evidence=(
                    f"judge found a mismatch with probability {verdict.probability:.2f}",
                    f"slots in doubt: {', '.join(doubtful) or 'none scored low'}",
                    f"draft: {_summary(drafted.spec)}",
                ),
                causes=(message,),
            )
        msg = "unreachable: judge loop exited without an outcome"
        raise ResolutionError(msg)

    def _accept(self, request: Request, drafted: ResolvedSpec, message: str) -> ResolvedSpec:
        self._append(SpecResolved(spec=drafted.spec), causes=(message,))
        commitment_id = f"{request.asset_id}:{request.consumer}"
        if commitment_id not in self._registry.state.commitments:
            self._append(
                CommitmentRegistered(
                    commitment_id=commitment_id,
                    asset_id=request.asset_id,
                    consumer=request.consumer,
                    incorporate_within_seconds=int(
                        drafted.spec.service.incorporate_within.total_seconds()
                    ),
                ),
                causes=(message,),
            )
        return drafted

    def _decided(self, request: Request, slot: str) -> str | None:
        """The latest in-scope decision on a question about `slot` for this request, if any."""
        state = self._registry.state
        best: Decision | None = None
        for question in state.questions.values():
            if question.slot != slot or question.decision_id is None:
                continue
            decision = state.decisions[question.decision_id]
            if _scope_covers(decision, request, question) and (
                best is None or decision.seq > best.seq
            ):
                best = decision
        return best.selected if best else None

    def _draft_spec(self, request: Request, message: str) -> Outcome:
        provenance: dict[str, OriginRef] = {}
        reused: list[str] = []
        origin = OriginRef.model_validate(message)
        defaults_origin = OriginRef.model_validate(self._defaults.ref)

        # population.entity
        entities = {e.name: e.description or e.name for e in self._semantic.entities}
        entity = self._pick(request, "population.entity", entities, provenance, reused, origin)
        if not isinstance(entity, str):
            return entity
        entity_def = next(e for e in self._semantic.entities if e.name == entity)
        entity_ref = SemanticRef(name=entity_def.name, version=entity_def.version)

        # measures.calculation
        calculations = {c.name: c.description or c.name for c in self._semantic.calculations}
        calculation = self._pick(
            request, "measures.calculation", calculations, provenance, reused, origin
        )
        if not isinstance(calculation, str):
            return calculation
        calc_def = next(c for c in self._semantic.calculations if c.name == calculation)
        measure = Measure(
            name=calc_def.name,
            calculation=SemanticRef(name=calc_def.name, version=calc_def.version),
            aggregation=Aggregation.SUM,
        )
        provenance["measures.aggregation"] = OriginRef.model_validate(
            "policy:approved_calculations_are_additive@1"
        )

        # identity_policy: closed by the chosen entity
        policies = {
            p.name: p.description or p.name
            for p in self._semantic.identity_policies
            if p.entity == entity_ref and p.fact_source == calc_def.source
        }
        if not policies:
            return self._question(
                request,
                slot="identity_policy",
                alternatives=(
                    "define_identity_policy",
                    "choose_different_entity",
                    "abandon_request",
                ),
                evidence=(
                    f"no identity policy attaches {calc_def.source} to {entity_ref}",
                    "known identity policies: "
                    + ", ".join(
                        f"{p.name}@{p.version} -> {p.entity}"
                        for p in self._semantic.identity_policies
                    ),
                    "a new policy needs a mapping source with validity columns for "
                    f"{entity_def.key}",
                ),
                causes=(message,),
            )
        identity = self._pick(request, "identity_policy", policies, provenance, reused, origin)
        if not isinstance(identity, str):
            return identity
        policy_def = next(p for p in self._semantic.identity_policies if p.name == identity)

        # window: deterministic extraction, or a recorded decision, or a question
        days, candidates = _window_days(request.text)
        decided_days = self._decided(request, "window.complete_calendar_days")
        if decided_days is not None and decided_days.isdigit():
            days = int(decided_days)
            provenance["window.complete_calendar_days"] = OriginRef(
                name=self._decision_id(request, "window.complete_calendar_days"),
                origin_kind="decision",
            )
        elif days is None:
            offered = tuple(str(c) for c in candidates) or ("7", "30", "90")
            if len(offered) < 2:
                offered = (*offered, *(d for d in ("7", "30", "90") if d not in offered))[:3]
            return self._question(
                request,
                slot="window.complete_calendar_days",
                alternatives=offered,
                evidence=(
                    "the request names more than one duration"
                    if candidates
                    else "the request names no window",
                    "complete calendar days end at the last midnight in the asset's timezone",
                    "a daily shared asset can serve any of these",
                ),
                causes=(message,),
            )
        else:
            provenance["window.complete_calendar_days"] = origin
        timezone = _timezone(request.text)
        if timezone is None:
            timezone = self._defaults.timezone
            provenance["window.timezone"] = defaults_origin
        else:
            provenance["window.timezone"] = origin

        include_inactive, stated = _include_inactive(request.text)
        provenance["population.include_inactive"] = origin if stated else defaults_origin

        # service: a recorded decision, the stated value, or the default; then feasibility
        seconds = _incorporate_seconds(request.text)
        decided_service = self._decided(request, "service.incorporate_within")
        if decided_service == "abandon_request":
            reason = "abandoned by a recorded decision on the freshness question"
            self._append(
                RequestRefused(asset_id=request.asset_id, consumer=request.consumer, reason=reason),
                causes=(message,),
            )
            return Refusal(reason=reason, probability=1.0)
        if decided_service is not None and decided_service.startswith("accept_"):
            seconds = int(decided_service.split("_")[1])
            provenance["service.incorporate_within"] = OriginRef(
                name=self._decision_id(request, "service.incorporate_within"),
                origin_kind="decision",
            )
        elif seconds is None:
            seconds = self._defaults.incorporate_within_seconds
            provenance["service.incorporate_within"] = defaults_origin
        else:
            provenance["service.incorporate_within"] = origin
            if seconds < self._defaults.fastest_incorporate_within_seconds:
                fastest = self._defaults.fastest_incorporate_within_seconds
                return self._question(
                    request,
                    slot="service.incorporate_within",
                    alternatives=(f"accept_{fastest}_seconds", "raise_budget", "abandon_request"),
                    evidence=(
                        f"the request asks for {seconds} seconds; "
                        f"the budget policy affords {fastest}",
                        f"accepting {fastest} seconds keeps the request inside "
                        f"{self._defaults.budget_policy}",
                        "raising the budget needs the budget owner",
                    ),
                    causes=(message,),
                )
        provenance["service.access_policy"] = defaults_origin
        provenance["service.budget_policy"] = defaults_origin

        spec = AssetSpec(
            asset_id=request.asset_id,
            spec_version=self._next_spec_version(request.asset_id),
            population=Population(entity=entity_ref, include_inactive=include_inactive),
            grain=Grain(keys=(entity_ref,), time_bucket=TimeBucket.NONE),
            measures=(measure,),
            window=Window(complete_calendar_days=days, timezone=timezone),
            identity_policy=SemanticRef(name=policy_def.name, version=policy_def.version),
            provenance=provenance,
            service=ServiceRequirements(
                incorporate_within=timedelta(seconds=seconds),
                access_policy=PolicyRef.model_validate(self._defaults.access_policy),
                budget_policy=PolicyRef.model_validate(self._defaults.budget_policy),
            ),
        )
        return ResolvedSpec(spec=spec, reused_decisions=tuple(reused))

    # ---- one slot ----

    def _pick(
        self,
        request: Request,
        slot: str,
        options: dict[str, str],
        provenance: dict[str, OriginRef],
        reused: list[str],
        origin: OriginRef,
    ) -> str | OpenQuestion:
        names = tuple(sorted(options))
        decision = self._reusable_decision(request, slot, names)
        if decision is not None:
            provenance[slot] = OriginRef(name=decision.decision_id, origin_kind="decision")
            reused.append(decision.decision_id)
            return decision.selected
        if len(names) == 1:
            provenance[slot] = origin
            return names[0]
        enum = closed_set(names)
        result = self._classify(
            SITE_RESOLVE,
            slot,
            request.asset_id,
            SlotContext(slot=slot, text=request.text, option_descriptions=options),
            enum,
        )
        if result.probability >= self._policy.threshold(slot):
            provenance[slot] = origin
            return result.value.value
        ranked = result.ranked()
        scored = {v.value: p for v, p in ranked}
        top = [v.value for v, _ in ranked[:3]]
        for name in names:
            if len(top) >= 2:
                break
            if name not in top:
                top.append(name)
        evidence = tuple(
            self._evidence(slot, value, scored.get(value, 0.0), options[value], request)
            for value in top
        )
        return self._question(
            request, slot=slot, alternatives=tuple(top), evidence=evidence, causes=()
        )

    def _reusable_decision(
        self, request: Request, slot: str, names: tuple[str, ...]
    ) -> Decision | None:
        state = self._registry.state
        candidates: list[tuple[Question, Decision]] = []
        for question in state.questions.values():
            if question.slot != slot or question.decision_id is None:
                continue
            if set(question.alternatives) != set(names):
                continue
            decision = state.decisions[question.decision_id]
            if decision.selected not in names:
                continue
            if _scope_covers(decision, request, question):
                candidates.append((question, decision))
        if not candidates:
            return None
        return sorted(candidates, key=lambda c: c[1].seq)[-1][1]

    def _evidence(
        self, slot: str, value: str, probability: float, description: str, request: Request
    ) -> str:
        parts = [f"{value} (p={probability:.2f}): {description}"]
        if slot == "population.entity":
            entity = next((e for e in self._semantic.entities if e.name == value), None)
            if entity is not None:
                source = self._semantic.source(entity.source)
                parts.append(f"needs {source.table}")
                if self._execute is not None:
                    try:
                        rows = self._execute(
                            f"SELECT count(DISTINCT {entity.key}), count(*) FROM {source.table}"
                        )
                        if rows:
                            parts.append(f"{rows[0][0]} groups over {rows[0][1]} rows")
                    except Exception as exc:
                        parts.append(f"example unavailable: {exc}")
                reuse = self._reusable_assets(value)
                parts.append(f"could reuse {', '.join(reuse) if reuse else 'nothing active'}")
        return "; ".join(parts)

    def _reusable_assets(self, entity_name: str) -> list[str]:
        out: list[str] = []
        for asset in self._registry.state.assets.values():
            if asset.spec is None or asset.active_implementation is None:
                continue
            if asset.spec.population.entity.name == entity_name:
                probe = asset.spec.model_copy(update={"asset_id": "probe"})
                if check(asset.spec, probe).can_serve:
                    out.append(asset.spec.ref)
        return out

    def _decision_id(self, request: Request, slot: str) -> str:
        state = self._registry.state
        for question in sorted(state.questions.values(), key=lambda q: -q.seq):
            if question.slot == slot and question.decision_id is not None:
                decision = state.decisions[question.decision_id]
                if _scope_covers(decision, request, question):
                    return decision.decision_id
        msg = f"no decision on {slot} covers {request.asset_id}"
        raise ResolutionError(msg)

    def _doubtful_slots(self, asset_id: str) -> list[str]:
        calls = [
            c.event_payload
            for c in self._registry.state.classifications
            if isinstance(c.event_payload, ClassificationProduced)
            and c.event_payload.asset_id == asset_id
            and c.event_payload.site == SITE_RESOLVE
        ]
        return sorted({c.slot for c in calls if c.probability < 0.95})

    # ---- questions ----

    def _question(
        self,
        request: Request,
        *,
        slot: str,
        alternatives: tuple[str, ...],
        evidence: tuple[str, ...],
        causes: tuple[str, ...],
    ) -> OpenQuestion:
        count = sum(
            1 for q in self._registry.state.questions.values() if q.asset_id == request.asset_id
        )
        question_id = f"{request.asset_id}_q{count + 1}"
        draft_input = QuestionDraftInput(
            asset_id=request.asset_id,
            consumer=request.consumer,
            slot=slot,
            request_text=request.text,
            alternatives=alternatives,
            evidence=evidence,
        )
        if self._drafter is not None:
            text = self._drafter.draft(draft_input)
            self._append(
                DraftProduced(
                    site=SITE_QUESTION,
                    asset_id=request.asset_id,
                    model_id=getattr(self._drafter, "model_id", type(self._drafter).__name__),
                    input_hash=input_hash(draft_input),
                    text=text,
                ),
                causes=causes,
            )
        else:
            text = (
                f'{request.consumer} asked for {request.asset_id}: "{request.text}". '
                f"The engine cannot settle {slot}. Choose one of: {', '.join(alternatives)}."
            )
        self._append(
            QuestionOpened(
                question_id=question_id,
                asset_id=request.asset_id,
                question=text,
                alternatives=alternatives,
                slot=slot,
                consumer=request.consumer,
                evidence=evidence,
            ),
            causes=causes,
        )
        return OpenQuestion(
            question_id=question_id,
            slot=slot,
            alternatives=alternatives,
            evidence=evidence,
            text=text,
        )

    # ---- model calls ----

    def _classify[T: StrEnum](
        self, site: str, slot: str, asset_id: str, context: BaseModel, options: type[T]
    ) -> Classification[T]:
        result = self._classifier.classify(context, options)
        self._append(
            ClassificationProduced(
                site=site,
                slot=slot,
                asset_id=asset_id,
                model_id=result.model_id,
                input_hash=input_hash(context, options),
                value=result.value.value,
                probability=result.probability,
                alternatives={v.value: p for v, p in result.alternatives},
            ),
            causes=(),
        )
        return result

    def _next_spec_version(self, asset_id: str) -> int:
        asset = self._registry.state.assets.get(asset_id)
        if asset is None or asset.spec is None:
            return 1
        return asset.spec.spec_version + 1

    def _append(
        self,
        payload: Payload,
        *,
        causes: tuple[str, ...],
        actor: str | None = None,
        authority: str | None = None,
    ) -> int:
        return self._registry.append(
            Event(
                actor=actor or self._actor,
                authority=authority or self._policy.ref,
                causes=causes,
                payload=payload,
            )
        )


# ---- deterministic extraction -----------------------------------------------------------------


def _window_days(text: str) -> tuple[int | None, tuple[int, ...]]:
    """The window the text asks for, and every duration it mentions.

    A duration led by a word such as "previous" wins. Without a lead word, a single duration is
    taken; several are returned as candidates for a question.
    """
    led: list[int] = []
    others: list[int] = []
    for match in _WINDOW.finditer(text):
        count = int(match.group("n"))
        if match.group("unit").lower().startswith("week"):
            count *= 7
        if count <= 0:
            continue
        (led if match.group("lead") else others).append(count)
    candidates = tuple(dict.fromkeys([*led, *others]))
    if len(led) == 1:
        return led[0], candidates
    if not led and len(others) == 1:
        return others[0], candidates
    return None, candidates


def _timezone(text: str) -> str | None:
    for match in _TIMEZONE.finditer(text):
        try:
            return validate_timezone(match.group(1))
        except ValueError:
            continue
    return None


def _include_inactive(text: str) -> tuple[bool, bool]:
    if _INCLUDE.search(text):
        return True, True
    if _EXCLUDE.search(text):
        return False, True
    return True, False


def _incorporate_seconds(text: str) -> int | None:
    """A freshness bound: `within N units` in a sentence that talks about fresh or new data."""
    for sentence in re.split(r"[.;\n]", text):
        match = _MINUTES.search(sentence)
        if match is None or not _FRESHNESS.search(sentence):
            continue
        value = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("hour"):
            return value * 3600
        if unit.startswith("minute"):
            return value * 60
        return value
    return None


def _summary(spec: AssetSpec) -> str:
    return (
        f"one row per {spec.population.entity}, "
        f"{', '.join(f'{m.aggregation} of {m.calculation}' for m in spec.measures)}, "
        f"{spec.window.complete_calendar_days} complete days in {spec.window.timezone}, "
        f"identity {spec.identity_policy}, "
        f"{'including' if spec.population.include_inactive else 'excluding'} inactive members"
    )


def _scope_covers(decision: Decision, request: Request, question: Question) -> bool:
    scope = decision.scope
    if scope == "workspace":
        return decision.authority.startswith("role:")
    if scope == f"consumer:{request.consumer}":
        return True
    return scope == f"asset:{request.asset_id}" and question.asset_id == request.asset_id


__all__ = [
    "JudgeContext",
    "JudgeVerdict",
    "OpenQuestion",
    "Outcome",
    "QuestionDraftInput",
    "Refusal",
    "Request",
    "ResolvedSpec",
    "Resolver",
    "ScreenContext",
    "ScreenVerdict",
    "SlotContext",
]
