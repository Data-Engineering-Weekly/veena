"""The `veena` command. This is the only module that prints."""

from __future__ import annotations

from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import duckdb
import typer

from veena import __version__
from veena.backends import DuckDBBackend
from veena.coordinator import Coordinator
from veena.errors import VeenaError
from veena.planner import compile_plan, describe_plan
from veena.registry import (
    AssetRequested,
    CommitmentRegistered,
    Event,
    Registry,
    SpecResolved,
    SqliteEventStore,
)
from veena.scenario import ScenarioConfig, build_scenario, scenario_stats
from veena.semantic import load_semantic_model
from veena.spec import check, dump_spec, load_spec
from veena.verification import verify

if TYPE_CHECKING:
    from veena.coordinator import ActivationOutcome
    from veena.evidence import CheckResult
    from veena.models import Classifier, Drafter
    from veena.registry.state import RegistryState
    from veena.resolver import Outcome, Resolver
    from veena.semantic.model import SemanticModel
    from veena.spec.model import AssetSpec

app = typer.Typer(no_args_is_help=True, help="Veena: the self-organizing data stack.")
spec_app = typer.Typer(no_args_is_help=True, help="Validate and compare specifications.")
scenario_app = typer.Typer(no_args_is_help=True, help="Build the synthetic DuckDB scenario.")
registry_app = typer.Typer(no_args_is_help=True, help="Inspect the event-sourced registry.")
asset_app = typer.Typer(no_args_is_help=True, help="Request and inspect assets.")
app.add_typer(spec_app, name="spec")
app.add_typer(scenario_app, name="scenario")
app.add_typer(registry_app, name="registry")
app.add_typer(asset_app, name="asset")

DEFAULT_REGISTRY = Path(".veena/registry.sqlite")
DEFAULT_SCENARIO = Path(".veena/scenario.duckdb")

RegistryPath = Annotated[
    Path, typer.Option("--registry", help="SQLite file holding the event log.")
]
ScenarioPath = Annotated[
    Path, typer.Option("--scenario", help="DuckDB file holding the sources and assets.")
]
SemanticPath = Annotated[
    Path | None, typer.Option("--semantic", help="Semantic model YAML. Default: bundled.")
]
AsOf = Annotated[
    str | None,
    typer.Option("--as-of", help="Instant that fixes the window, ISO 8601. Default: now."),
]


def _semantic_path(path: Path | None) -> Path:
    return path or Path(str(files("veena.scenario") / "semantic.yaml"))


def _as_of(text: str | None) -> datetime:
    if text is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"veena {__version__}")


@spec_app.command("validate")
def spec_validate(path: Path) -> None:
    """Load a spec and print it back in canonical form."""
    try:
        spec = load_spec(path)
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(dump_spec(spec), nl=False)


@spec_app.command("compare")
def spec_compare(provider: Path, consumer: Path) -> None:
    """Report whether the provider spec can serve the consumer spec."""
    try:
        result = check(load_spec(provider), load_spec(consumer))
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"{result.provider} -> {result.consumer}: {result.verdict}")
    for op in result.operations:
        typer.echo(f"  needs {op}")
    for reason in result.reasons:
        typer.echo(f"  cannot: {reason}")
    if not result.can_serve:
        raise typer.Exit(code=2)


@spec_app.command("examples")
def spec_examples() -> None:
    """Print the paths of the bundled example specs and semantic model."""
    root = files("veena.scenario")
    typer.echo(str(root / "semantic.yaml"))
    for entry in sorted((root / "specs").iterdir(), key=lambda e: e.name):
        typer.echo(str(entry))


@scenario_app.command("build")
def scenario_build(
    path: Annotated[Path, typer.Option(help="DuckDB file to create.")] = DEFAULT_SCENARIO,
    seed: int = 7,
    customers: int = 200,
    history_days: int = 120,
    last_complete_day: Annotated[
        str, typer.Option(help="Last full UTC day of source data, YYYY-MM-DD.")
    ] = "2026-09-30",
) -> None:
    """Generate the synthetic sources into a DuckDB file."""
    config = ScenarioConfig(
        seed=seed,
        customers=customers,
        history_days=history_days,
        last_complete_day=date.fromisoformat(last_complete_day),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = build_scenario(path, config)
    except VeenaError as exc:
        raise _fail(exc) from exc
    stats = scenario_stats(conn)
    conn.close()
    typer.echo(f"built {path}")
    for name, value in stats.model_dump().items():
        typer.echo(f"  {name}: {value}")


@scenario_app.command("semantic")
def scenario_semantic() -> None:
    """Validate the bundled semantic model and list its definitions."""
    path = Path(str(files("veena.scenario") / "semantic.yaml"))
    try:
        model = load_semantic_model(path)
    except VeenaError as exc:
        raise _fail(exc) from exc
    for entity in model.entities:
        typer.echo(
            f"entity      semantic:{entity.name}@{entity.version}  ({entity.source}.{entity.key})"
        )
    for calc in model.calculations:
        typer.echo(
            f"calculation semantic:{calc.name}@{calc.version}  {calc.expression}  [{calc.unit}]"
        )
    for rule in model.time_rules:
        typer.echo(
            f"time rule   semantic:{rule.name}@{rule.version}  "
            f"{rule.source}.{rule.timestamp_column} {rule.timezone}"
        )
    for policy in model.identity_policies:
        typer.echo(
            f"identity    semantic:{policy.name}@{policy.version}  "
            f"{policy.fact_source} -> {policy.entity}"
        )


def _open_registry(path: Path) -> Registry:
    return Registry(SqliteEventStore(path))


@registry_app.command("log")
def registry_log(registry: RegistryPath = DEFAULT_REGISTRY, after: int = 0) -> None:
    """Print every event after a revision."""
    reg = _open_registry(registry)
    for stored in reg.events(after=after):
        event = stored.event
        typer.echo(
            f"{stored.seq:>5}  {event.recorded_at.isoformat(timespec='seconds')}  "
            f"{event.kind:<24} actor={event.actor} authority={event.authority}"
        )


@registry_app.command("assets")
def registry_assets(registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """List assets with status, spec version, and open commitments."""
    state = _open_registry(registry).state
    typer.echo(f"revision {state.revision}")
    for asset in sorted(state.assets.values(), key=lambda a: a.asset_id):
        spec = asset.spec.ref if asset.spec else "-"
        progress = ", ".join(
            f"{k}@{v.isoformat(timespec='seconds')}"
            for k, v in sorted(asset.source_progress.items())
        )
        typer.echo(
            f"{asset.asset_id:<28} {asset.status:<10} spec={spec:<24} "
            f"commitments={len(asset.commitments)} impl={asset.active_implementation or '-'}"
            + (f" progress={progress}" if progress else "")
        )


ClassifierChoice = Annotated[
    str,
    typer.Option(
        "--classifier",
        help="keyword (offline baseline), claude (needs ANTHROPIC_API_KEY), jev (needs JEV_*).",
    ),
]
DrafterChoice = Annotated[
    str,
    typer.Option("--drafter", help="none (template prose) or claude (needs ANTHROPIC_API_KEY)."),
]


def _autonomy(name: str) -> Literal["shadow", "assisted", "autonomous"]:
    if name == "shadow":
        return "shadow"
    if name == "assisted":
        return "assisted"
    if name == "autonomous":
        return "autonomous"
    raise _fail(VeenaError("autonomy must be shadow, assisted, or autonomous"))


def _classifier(name: str) -> Classifier:
    from veena.models import ClaudeClassifier, JevClassifier, KeywordClassifier

    match name:
        case "keyword":
            return KeywordClassifier()
        case "claude":
            return ClaudeClassifier()
        case "jev":
            return JevClassifier()
    raise _fail(VeenaError(f"unknown classifier {name!r}; use keyword, claude, or jev"))


def _drafter(name: str) -> Drafter | None:
    from veena.models import ClaudeDrafter

    match name:
        case "none":
            return None
        case "claude":
            return ClaudeDrafter()
    raise _fail(VeenaError(f"unknown drafter {name!r}; use none or claude"))


Threshold = Annotated[
    float,
    typer.Option(
        "--threshold", help="Probability below which a slot becomes a question (policy knob)."
    ),
]


def _resolver(
    registry: Path,
    scenario: Path,
    semantic: Path | None,
    classifier: str,
    drafter: str,
    threshold: float,
) -> tuple[Registry, Resolver]:
    from veena.policy import ResolutionPolicy
    from veena.resolver import Resolver

    reg = _open_registry(registry)
    model = load_semantic_model(_semantic_path(semantic))
    execute = None
    if scenario.exists():
        execute = DuckDBBackend(duckdb.connect(str(scenario), read_only=True)).execute
    return reg, Resolver(
        reg,
        model,
        _classifier(classifier),
        drafter=_drafter(drafter),
        policy=ResolutionPolicy(default_threshold=threshold),
        execute=execute,
    )


def _report_outcome(outcome: Outcome, reg: Registry) -> None:
    from veena.resolver import OpenQuestion, Refusal, ResolvedSpec

    match outcome:
        case ResolvedSpec():
            spec = outcome.spec
            typer.echo(f"resolved {spec.ref}; registry at revision {reg.revision}")
            for key, origin in spec.provenance.items():
                typer.echo(f"  {key} <- {origin}")
            if outcome.reused_decisions:
                typer.echo(f"  reused decisions: {', '.join(outcome.reused_decisions)}")
        case OpenQuestion():
            typer.echo(f"question {outcome.question_id} on {outcome.slot}:")
            typer.echo(f"  {outcome.text}")
            for alternative, evidence in zip(outcome.alternatives, outcome.evidence, strict=False):
                typer.echo(f"  - {alternative}: {evidence}")
            for alternative in outcome.alternatives[len(outcome.evidence) :]:
                typer.echo(f"  - {alternative}")
            typer.echo(
                f"answer with: veena question answer {outcome.question_id} --select <alternative>"
            )
            raise typer.Exit(code=3)
        case Refusal():
            typer.echo(f"refused: {outcome.reason}")
            raise typer.Exit(code=4)


@asset_app.command("request")
def asset_request(
    spec_path: Annotated[
        Path | None, typer.Argument(help="Resolved spec to register. Omit to resolve --text.")
    ] = None,
    consumer: Annotated[str, typer.Option(help="Who depends on the asset, e.g. app:sales.")] = "",
    text: Annotated[str, typer.Option(help="The request in the consumer's words.")] = "",
    asset_id: Annotated[
        str, typer.Option("--asset-id", help="Identity for the asset when resolving from text.")
    ] = "",
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
    classifier: ClassifierChoice = "keyword",
    drafter: DrafterChoice = "none",
    threshold: Threshold = 0.8,
) -> None:
    """Record a request. With a spec file, register it as resolved; with --text, resolve it."""
    if not consumer:
        raise _fail(VeenaError("--consumer is required"))
    if spec_path is None:
        if not text or not asset_id:
            raise _fail(VeenaError("resolving from text needs --text and --asset-id"))
        from veena.resolver import Request

        try:
            reg, resolver = _resolver(registry, scenario, semantic, classifier, drafter, threshold)
            outcome = resolver.resolve(Request(asset_id=asset_id, consumer=consumer, text=text))
        except VeenaError as exc:
            raise _fail(exc) from exc
        _report_outcome(outcome, reg)
        return
    try:
        spec = load_spec(spec_path)
    except VeenaError as exc:
        raise _fail(exc) from exc
    reg = _open_registry(registry)
    authority = f"request:{consumer}"
    try:
        seq = reg.append(
            Event(
                actor=consumer,
                authority=authority,
                payload=AssetRequested(
                    asset_id=spec.asset_id, consumer=consumer, request_text=text
                ),
            )
        )
        reg.append(
            Event(
                actor="engine",
                authority=authority,
                causes=(f"event:{seq}",),
                payload=SpecResolved(spec=spec),
            )
        )
        reg.append(
            Event(
                actor="engine",
                authority=authority,
                causes=(f"event:{seq}",),
                payload=CommitmentRegistered(
                    commitment_id=f"{spec.asset_id}:{consumer}",
                    asset_id=spec.asset_id,
                    consumer=consumer,
                    incorporate_within_seconds=int(spec.service.incorporate_within.total_seconds()),
                ),
            )
        )
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"registered {spec.ref} for {consumer}; registry at revision {reg.revision}")


@asset_app.command("resolve")
def asset_resolve(
    asset_id: str,
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
    classifier: ClassifierChoice = "keyword",
    drafter: DrafterChoice = "none",
    threshold: Threshold = 0.8,
) -> None:
    """Resolve a recorded request again, for example after answering its question."""
    try:
        reg, resolver = _resolver(registry, scenario, semantic, classifier, drafter, threshold)
        outcome = resolver.resume(asset_id)
    except VeenaError as exc:
        raise _fail(exc) from exc
    _report_outcome(outcome, reg)


def _spec_for(reg: Registry, asset_id: str) -> tuple[Registry, AssetSpec]:
    asset = reg.state.assets.get(asset_id)
    if asset is None or asset.spec is None:
        raise _fail(VeenaError(f"asset {asset_id!r} has no resolved specification in the registry"))
    return reg, asset.spec


@asset_app.command("plan")
def asset_plan(
    asset_id: str,
    registry: RegistryPath = DEFAULT_REGISTRY,
    semantic: SemanticPath = None,
    as_of: AsOf = None,
) -> None:
    """Compile the asset's spec and print the plan and the DuckDB SQL."""
    _, spec = _spec_for(_open_registry(registry), asset_id)
    try:
        model = load_semantic_model(_semantic_path(semantic))
        plan = compile_plan(spec, model)
        sql, drafted = DuckDBBackend(duckdb.connect()).render(plan, as_of=_as_of(as_of))
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(describe_plan(plan))
    if drafted:
        typer.echo(f"  drafted steps: {', '.join(drafted)}")
    typer.echo("")
    typer.echo(sql)


@asset_app.command("activate")
def asset_activate(
    asset_id: str,
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
    as_of: AsOf = None,
    repair_bound: Annotated[int, typer.Option(help="Verification attempts before escalating.")] = 2,
    require_approval: Annotated[
        bool, typer.Option(help="Make share proposals wait for `veena proposal approve`.")
    ] = False,
    min_benefit: Annotated[
        int,
        typer.Option(
            help="Smallest saving in fact rows processed per refresh that justifies sharing."
        ),
    ] = 1,
    max_reorganizations: Annotated[
        int, typer.Option(help="Cap on share deployments per asset in the policy period.")
    ] = 1,
    autonomy: Annotated[
        str, typer.Option(help="shadow records proposals only; assisted; autonomous.")
    ] = "assisted",
) -> None:
    """Find overlap, then propose, implement, verify, and deploy on the scenario database."""
    try:
        reg, coordinator = _coordinator(
            registry,
            scenario,
            semantic,
            repair_bound=repair_bound,
            require_approval=require_approval,
            min_benefit=min_benefit,
            max_reorganizations=max_reorganizations,
            autonomy=autonomy,
        )
        outcome = coordinator.activate(asset_id, as_of=_as_of(as_of))
    except VeenaError as exc:
        raise _fail(exc) from exc
    _report_activation(outcome, reg)


@asset_app.command("verify")
def asset_verify(
    asset_id: str,
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
    as_of: AsOf = None,
) -> None:
    """Run the generated checks against the asset's published interface."""
    reg, spec = _spec_for(_open_registry(registry), asset_id)
    asset = reg.state.assets[asset_id]
    if asset.active_implementation is None:
        raise _fail(VeenaError(f"asset {asset_id!r} has no active implementation to verify"))
    try:
        model = load_semantic_model(_semantic_path(semantic))
        backend = DuckDBBackend(duckdb.connect(str(scenario), read_only=True))
        outcome = verify(spec, model, asset_id, backend.execute, as_of=_as_of(as_of))
    except VeenaError as exc:
        raise _fail(exc) from exc
    _print_checks(outcome.checks)
    typer.echo(f"{'passed' if outcome.passed else 'failed'}; grade {outcome.grade}")
    if not outcome.passed:
        raise typer.Exit(code=3)


@asset_app.command("show")
def asset_show(
    asset_id: str,
    registry: RegistryPath = DEFAULT_REGISTRY,
    output_format: Annotated[str, typer.Option("--format", help="text or yaml.")] = "text",
) -> None:
    """Print the trail: spec, commitments, deployments, proposals, verifications, decisions."""
    state = _open_registry(registry).state
    asset = state.assets.get(asset_id)
    if asset is None:
        raise _fail(VeenaError(f"unknown asset {asset_id!r}"))
    if output_format == "yaml":
        typer.echo(_fragment_yaml(asset_id, state), nl=False)
        return
    if asset.request_text:
        typer.echo(f'request by {asset.requested_by}: "{asset.request_text}"')
    if asset.refused:
        typer.echo(f"refused: {asset.refused}")
    typer.echo(
        f"asset {asset.asset_id}  status={asset.status}  impl={asset.active_implementation or '-'}"
        + (f"  reads {', '.join(asset.depends_on)}" if asset.depends_on else "")
    )
    if asset.spec:
        s = asset.spec
        window = (
            f"{s.window.complete_calendar_days} complete days"
            if s.window.complete_calendar_days
            else f"{s.window.history_days} days of history"
        )
        typer.echo(
            f"spec {s.ref}: one row per {', '.join(str(k) for k in s.grain.keys)}"
            f"{' by ' + s.grain.time_bucket if s.grain.time_bucket != 'none' else ''}, "
            f"{window} in {s.window.timezone}, identity {s.identity_policy}"
        )
        for key, origin in s.provenance.items():
            typer.echo(f"  {key} <- {origin}")
    for cid in asset.commitments:
        c = state.commitments[cid]
        typer.echo(
            f"commitment {cid}: {c.consumer}, incorporate within {c.incorporate_within_seconds}s"
        )
    for d in sorted(state.deployments.values(), key=lambda d: d.seq):
        if asset_id in d.activated:
            status = "rolled back" if d.rolled_back else "active"
            verified_by = next(
                (
                    v.verification_id
                    for v in state.verifications.values()
                    if v.proposal_id == d.proposal_id
                    and v.implementation_id == d.activated[asset_id]
                ),
                d.verification_id,
            )
            typer.echo(
                f"deployment {d.deployment_id} ({status}): {d.activated[asset_id]} "
                f"via {d.proposal_id}, verified by {verified_by}, "
                f"assumed revision {d.assumed_revision}"
            )
    for p in sorted(state.proposals.values(), key=lambda p: p.seq):
        if asset_id in p.affected_assets:
            typer.echo(f"proposal {p.proposal_id} ({p.kind_of_change}, {p.status}): {p.summary}")
            for impl_id in p.implementations:
                impl = state.implementations[impl_id]
                deps = ", ".join(f"{d.name}@{d.version}" for d in impl.dependencies)
                typer.echo(f"  implementation {impl_id} on {impl.backend}; reads {deps}")
            for vid in p.verifications:
                v = state.verifications[vid]
                typer.echo(
                    f"  verification {vid}: {'passed' if v.passed else 'failed'}, grade {v.grade}, "
                    f"{sum(c.passed for c in v.checks)}/{len(v.checks)} checks"
                )
                for chk in v.checks:
                    if not chk.passed:
                        typer.echo(f"    FAIL {chk.name}: {chk.detail}")
    for q in sorted(state.questions.values(), key=lambda q: q.seq):
        if q.asset_id == asset_id:
            answer = (
                f" -> {state.decisions[q.decision_id].selected}" if q.decision_id else " (open)"
            )
            typer.echo(f"question {q.question_id} ({q.slot or 'general'}): {q.question}{answer}")
    calls = [
        c for c in (*state.classifications, *state.drafts) if c.event_payload.asset_id == asset_id
    ]
    if calls:
        typer.echo(f"model calls: {len(calls)} (see `veena models calls --asset-id {asset_id}`)")


def _fragment_yaml(asset_id: str, state: RegistryState) -> str:
    """Render the article's fragment: requirements with ref and source, plus trail refs."""
    import yaml

    asset = state.assets[asset_id]
    spec = asset.spec
    doc: dict[str, object] = {
        "asset_id": asset_id,
        "spec_version": spec.spec_version if spec else None,
    }
    if spec is not None:
        values: dict[str, str] = {
            "population.entity": str(spec.population.entity),
            "population.include_inactive": str(spec.population.include_inactive).lower(),
            "measures.calculation": ", ".join(str(m.calculation) for m in spec.measures),
            "measures.aggregation": ", ".join(m.aggregation for m in spec.measures),
            "window.complete_calendar_days": str(spec.window.complete_calendar_days),
            "window.timezone": spec.window.timezone,
            "identity_policy": str(spec.identity_policy),
            "service.incorporate_within": (
                f"{int(spec.service.incorporate_within.total_seconds())}s"
            ),
            "service.access_policy": str(spec.service.access_policy),
            "service.budget_policy": str(spec.service.budget_policy),
        }
        requirements: dict[str, dict[str, str]] = {}
        for key, value in values.items():
            source = spec.provenance.get(key)
            requirements[key] = {"ref": value, "source": str(source) if source else "unrecorded"}
        doc["requirements"] = requirements
    proposals = [p for p in state.proposals.values() if asset_id in p.affected_assets]
    if proposals:
        last = sorted(proposals, key=lambda p: p.seq)[-1]
        doc["change_proposal_ref"] = f"proposal:{last.proposal_id}"
        if last.verifications:
            doc["verification_ref"] = f"verification:{last.verifications[-1]}"
        if last.deployment_id:
            doc["deployment_ref"] = f"deployment:{last.deployment_id}"
    questions = [q for q in state.questions.values() if q.asset_id == asset_id]
    if questions:
        doc["decisions"] = [
            {"question": q.question_id, "slot": q.slot, "decision": q.decision_id or "open"}
            for q in questions
        ]
    return yaml.safe_dump(doc, sort_keys=False)


def _print_checks(checks: tuple[CheckResult, ...]) -> None:
    for c in checks:
        mark = "ok  " if c.passed else "FAIL"
        kind = "structural" if c.structural else c.grade
        typer.echo(f"  {mark} {c.name:<26} [{kind}] {c.description}")
        if not c.passed or c.uncertainty:
            typer.echo(
                f"       {c.detail}" if not c.passed else f"       uncertainty: {c.uncertainty}"
            )


proposal_app = typer.Typer(
    no_args_is_help=True, help="List, inspect, approve, or reject proposals."
)
deployment_app = typer.Typer(no_args_is_help=True, help="Inspect and roll back deployments.")
app.add_typer(proposal_app, name="proposal")
app.add_typer(deployment_app, name="deployment")


def _coordinator(
    registry: Path,
    scenario: Path,
    semantic: Path | None,
    *,
    repair_bound: int = 2,
    require_approval: bool = False,
    min_benefit: int = 1,
    max_reorganizations: int = 1,
    autonomy: str = "assisted",
) -> tuple[Registry, Coordinator]:
    from veena.policy import ReorganizationPolicy

    reg = _open_registry(registry)
    model = load_semantic_model(_semantic_path(semantic))
    backend = DuckDBBackend(duckdb.connect(str(scenario)))
    policy = ReorganizationPolicy(
        autonomy=_autonomy(autonomy),
        require_approval=require_approval,
        min_benefit_rows=min_benefit,
        max_reorganizations_per_period=max_reorganizations,
    )
    return reg, Coordinator(reg, backend, model, repair_bound=repair_bound, policy=policy)


def _report_activation(outcome: ActivationOutcome, reg: Registry) -> None:
    for asset_id, result in outcome.outcomes.items():
        typer.echo(f"verification of {asset_id}:")
        _print_checks(result.checks)
    if outcome.pending_approval:
        typer.echo(
            f"proposal {outcome.proposal_id} ({outcome.kind_of_change}) waits for approval; "
            f"run `veena proposal show {outcome.proposal_id}`"
        )
        return
    if outcome.activated:
        last = outcome.last_outcome
        typer.echo(
            f"activated {outcome.asset_id} via {outcome.deployment_id} "
            f"({outcome.kind_of_change}) after {outcome.attempts} attempt(s); "
            f"grade {last.grade if last else '-'}; registry at revision {reg.revision}"
        )
        return
    typer.echo(f"not activated; opened question {outcome.question_id}")
    raise typer.Exit(code=3)


@proposal_app.command("list")
def proposal_list(registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """List proposals with kind, status, and affected assets."""
    state = _open_registry(registry).state
    for p in sorted(state.proposals.values(), key=lambda p: p.seq):
        typer.echo(
            f"{p.proposal_id:<34} {p.kind_of_change:<7} {p.status:<10} "
            f"affects {', '.join(p.affected_assets)}"
        )


@proposal_app.command("show")
def proposal_show(proposal_id: str, registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """Print one proposal: changes, costs, alternatives considered, evidence, status."""
    state = _open_registry(registry).state
    p = state.proposals.get(proposal_id)
    if p is None:
        raise _fail(VeenaError(f"unknown proposal {proposal_id!r}"))
    typer.echo(f"proposal {p.proposal_id} ({p.kind_of_change}) status={p.status}")
    typer.echo(f"  {p.summary}")
    if p.reason:
        typer.echo(f"  reason: {p.reason}")
    for change in p.changes:
        typer.echo(f"  change: {change}")
    typer.echo(f"  affects: {', '.join(p.affected_assets)}; assumed revision {p.assumed_revision}")
    if p.cost_current_rows is not None and p.cost_proposed_rows is not None:
        typer.echo(
            f"  cost: {p.cost_current_rows} fact rows processed per refresh now, "
            f"{p.cost_proposed_rows} proposed, benefit {p.cost_current_rows - p.cost_proposed_rows}"
        )
    for e in p.evidence_required:
        typer.echo(f"  evidence required: {e}")
    for a in p.alternatives:
        typer.echo(f"  considered: {a}")
    for impl_id in p.implementations:
        typer.echo(f"  implementation {impl_id}")
    for vid in p.verifications:
        v = state.verifications[vid]
        typer.echo(
            f"  verification {vid} of {v.implementation_id}: "
            f"{'passed' if v.passed else 'failed'}, grade {v.grade}"
        )
    if p.deployment_id:
        typer.echo(f"  deployment {p.deployment_id}")


@proposal_app.command("approve")
def proposal_approve(
    proposal_id: str,
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
    as_of: AsOf = None,
    reason: str = "",
) -> None:
    """Approve an open proposal and run it to deployment."""
    try:
        reg, coordinator = _coordinator(registry, scenario, semantic)
        outcome = coordinator.approve(proposal_id, as_of=_as_of(as_of), reason=reason)
    except VeenaError as exc:
        raise _fail(exc) from exc
    _report_activation(outcome, reg)


@proposal_app.command("reject")
def proposal_reject(
    proposal_id: str,
    reason: Annotated[str, typer.Option(help="Why the proposal will not proceed.")],
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
) -> None:
    """Reject an open proposal."""
    try:
        _, coordinator = _coordinator(registry, scenario, semantic)
        coordinator.reject(proposal_id, reason=reason)
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"rejected {proposal_id}")


@deployment_app.command("list")
def deployment_list(registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """List deployments and what each switched."""
    state = _open_registry(registry).state
    for d in sorted(state.deployments.values(), key=lambda d: d.seq):
        status = "rolled back" if d.rolled_back else "active"
        switched = ", ".join(f"{a} -> {i}" for a, i in d.activated.items())
        typer.echo(
            f"{d.deployment_id:<34} {status:<11} {'staged' if d.staged else 'atomic':<6} {switched}"
        )


@deployment_app.command("rollback")
def deployment_rollback(
    deployment_id: str,
    reason: Annotated[str, typer.Option(help="Why the deployment is being reverted.")],
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
    as_of: AsOf = None,
) -> None:
    """Repoint every interface the deployment switched back to what it replaced."""
    try:
        reg, coordinator = _coordinator(registry, scenario, semantic)
        coordinator.rollback(deployment_id, reason=reason, as_of=_as_of(as_of))
    except VeenaError as exc:
        raise _fail(exc) from exc
    d = reg.state.deployments[deployment_id]
    typer.echo(f"rolled back {deployment_id}; restored {d.replaced}")


question_app = typer.Typer(no_args_is_help=True, help="List and answer the engine's questions.")
models_app = typer.Typer(no_args_is_help=True, help="Inspect model calls and calibration.")
app.add_typer(question_app, name="question")
app.add_typer(models_app, name="models")


@question_app.command("list")
def question_list(
    registry: RegistryPath = DEFAULT_REGISTRY,
    open_only: Annotated[bool, typer.Option("--open", help="Only unanswered questions.")] = False,
) -> None:
    """List questions with slot, asset, and answer."""
    state = _open_registry(registry).state
    for q in sorted(state.questions.values(), key=lambda q: q.seq):
        if open_only and q.decision_id:
            continue
        answer = f"-> {state.decisions[q.decision_id].selected}" if q.decision_id else "(open)"
        typer.echo(f"{q.question_id:<30} {q.asset_id:<24} {q.slot or '-':<28} {answer}")


@question_app.command("show")
def question_show(question_id: str, registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """Print one question with its alternatives and evidence."""
    state = _open_registry(registry).state
    q = state.questions.get(question_id)
    if q is None:
        raise _fail(VeenaError(f"unknown question {question_id!r}"))
    typer.echo(
        f"question {q.question_id} for {q.asset_id} ({q.slot or 'general'}), "
        f"asked by {q.consumer or '-'}"
    )
    typer.echo(f"  {q.question}")
    for alternative, evidence in zip(q.alternatives, q.evidence, strict=False):
        typer.echo(f"  - {alternative}: {evidence}")
    for alternative in q.alternatives[len(q.evidence) :]:
        typer.echo(f"  - {alternative}")
    if q.decision_id:
        d = state.decisions[q.decision_id]
        typer.echo(
            f"  answered by {d.actor} ({d.authority}): {d.selected}, scope {d.scope}: {d.rationale}"
        )


@question_app.command("answer")
def question_answer(
    question_id: str,
    select: Annotated[str, typer.Option(help="One of the question's alternatives.")],
    rationale: Annotated[str, typer.Option(help="Why this alternative.")] = "",
    scope: Annotated[
        str,
        typer.Option(help="asset, consumer, or workspace. Workspace needs --authority role:..."),
    ] = "asset",
    actor: Annotated[str, typer.Option(help="Who is deciding, e.g. user:ananth.")] = "user:owner",
    authority: Annotated[
        str, typer.Option(help="Under what authority, e.g. role:data_owner.")
    ] = "role:data_owner",
    registry: RegistryPath = DEFAULT_REGISTRY,
) -> None:
    """Record a decision for an open question."""
    from veena.registry import DecisionRecorded

    reg = _open_registry(registry)
    q = reg.state.questions.get(question_id)
    if q is None:
        raise _fail(VeenaError(f"unknown question {question_id!r}"))
    scope_value = {
        "asset": f"asset:{q.asset_id}",
        "consumer": f"consumer:{q.consumer}",
        "workspace": "workspace",
    }.get(scope)
    if scope_value is None:
        raise _fail(VeenaError("scope must be asset, consumer, or workspace"))
    count = sum(1 for d in reg.state.decisions.values() if d.question_id == question_id)
    decision_id = f"{question_id}_d{count + 1}"
    try:
        reg.append(
            Event(
                actor=actor,
                authority=authority,
                causes=(f"question:{question_id}",),
                payload=DecisionRecorded(
                    decision_id=decision_id,
                    question_id=question_id,
                    selected=select,
                    rationale=rationale,
                    scope=scope_value,
                ),
            )
        )
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(
        f"recorded {decision_id}: {select} with scope {scope_value}; "
        f"resume with `veena asset resolve {q.asset_id}`"
    )


@models_app.command("calls")
def models_calls(registry: RegistryPath = DEFAULT_REGISTRY, asset_id: str = "") -> None:
    """List every recorded classifier and drafter call."""
    from veena.registry.events import ClassificationProduced

    state = _open_registry(registry).state
    calls = sorted((*state.classifications, *state.drafts), key=lambda c: c.seq)
    for call in calls:
        p = call.event_payload
        if asset_id and p.asset_id != asset_id:
            continue
        if isinstance(p, ClassificationProduced):
            typer.echo(
                f"{call.seq:>5} classify {p.site:<16} {p.slot or '-':<24} {p.asset_id:<22} "
                f"{p.value} p={p.probability:.2f} [{p.model_id}]"
            )
        else:
            typer.echo(
                f"{call.seq:>5} draft    {p.site:<16} {'-':<24} {p.asset_id:<22} "
                f"{len(p.text)} chars [{p.model_id}]"
            )


@models_app.command("calibration")
def models_calibration(
    site: Annotated[str, typer.Argument(help="Call site, e.g. resolve_refs.")],
    semantic: SemanticPath = None,
    classifier: ClassifierChoice = "keyword",
    labels: Annotated[Path | None, typer.Option(help="Labeled YAML. Default: bundled.")] = None,
    log_dir: Annotated[Path, typer.Option(help="Where labeled predictions accumulate.")] = Path(
        ".veena/calibration"
    ),
) -> None:
    """Run the labeled set through a classifier and print the reliability table."""
    import yaml

    from veena.models import CalibrationLog, CalibrationRecord, calibration_report, closed_set
    from veena.resolver import SlotContext

    if site != "resolve_refs":
        raise _fail(VeenaError(f"no labeled set for site {site!r}; only resolve_refs exists yet"))
    model = load_semantic_model(_semantic_path(semantic))
    path = labels or Path(str(files("veena.scenario") / "labels" / "resolve_refs.yaml"))
    entries = yaml.safe_load(path.read_text())
    clf = _classifier(classifier)
    options_by_slot = {
        "population.entity": {e.name: e.description or e.name for e in model.entities},
        "measures.calculation": {c.name: c.description or c.name for c in model.calculations},
    }
    log = CalibrationLog(log_dir / f"{site}.jsonl")
    records = []
    for entry in entries:
        slot = str(entry["slot"])
        options = options_by_slot[slot]
        enum = closed_set(sorted(options))
        result = clf.classify(
            SlotContext(slot=slot, text=str(entry["text"]), option_descriptions=options), enum
        )
        record = CalibrationRecord(
            site=site,
            slot=slot,
            model_id=result.model_id,
            predicted=result.value.value,
            expected=str(entry["expected"]),
            probability=result.probability,
        )
        log.append(record)
        records.append(record)
    typer.echo(calibration_report(site, tuple(records)).render())


import_app = typer.Typer(no_args_is_help=True, help="Import observed state from a warehouse.")
report_app = typer.Typer(no_args_is_help=True, help="Reports over the imported graph.")
app.add_typer(import_app, name="import")
app.add_typer(report_app, name="report")

DEFAULT_IMPORTED_SEMANTIC = Path(".veena/semantic.yaml")


@scenario_app.command("dbt")
def scenario_dbt(
    out: Annotated[
        Path, typer.Option(help="Directory for manifest.json and query_history.jsonl.")
    ] = Path(".veena/dbt"),
    history_days: int = 14,
) -> None:
    """Write the bundled e-commerce dbt manifest and query history fixture."""
    from veena.scenario.dbt_fixture import DbtFixtureConfig, write_fixture

    manifest, history = write_fixture(out, DbtFixtureConfig(history_days=history_days))
    typer.echo(f"wrote {manifest} and {history}")


@import_app.command("dbt")
def import_dbt(
    manifest: Annotated[Path, typer.Argument(help="dbt manifest.json")],
    history: Annotated[Path | None, typer.Option(help="Query history JSONL to import too.")] = None,
    registry: RegistryPath = DEFAULT_REGISTRY,
    semantic_out: Annotated[
        Path, typer.Option(help="Where to write the semantic model derived from MetricFlow.")
    ] = DEFAULT_IMPORTED_SEMANTIC,
) -> None:
    """Import models as observed assets, exposures as commitments, MetricFlow as meaning.

    With `--history`, query history becomes usage, refresh cost, unmanaged reads, and proposals.
    """
    import yaml

    from veena.importer import (
        import_history,
        import_project,
        load_history,
        load_manifest,
        semantic_from_project,
    )
    from veena.registry import QuestionOpened

    try:
        project = load_manifest(manifest)
        reg = _open_registry(registry)
        report = import_project(reg, project)
        typer.echo(
            f"imported {project.project_name}: {report.assets_added} assets added, "
            f"{report.assets_updated} updated, {report.assets_unchanged} unchanged; "
            f"{report.commitments_added} commitments added, {report.commitments_unchanged} kept"
        )
        imported = semantic_from_project(project)
        semantic_out.parent.mkdir(parents=True, exist_ok=True)
        semantic_out.write_text(
            "# Derived from MetricFlow definitions in the dbt manifest. People own this file.\n"
            + yaml.safe_dump(_semantic_yaml(imported.model), sort_keys=False)
        )
        typer.echo(
            f"semantic model: {len(imported.model.entities)} entities, "
            f"{len(imported.model.calculations)} calculations, "
            f"{len(imported.model.time_rules)} time rules, "
            f"{len(imported.model.identity_policies)} identity policies -> {semantic_out}"
        )
        recorded = 0
        state = reg.state
        for gap in imported.gaps:
            if gap.asset_id not in state.assets:
                typer.echo(f"  gap: {gap.text}")
                continue
            already = any(
                q.slot == "semantic_gap" and q.asset_id == gap.asset_id and q.question == gap.text
                for q in state.questions.values()
            )
            if already:
                continue
            count = sum(1 for q in reg.state.questions.values() if q.asset_id == gap.asset_id)
            reg.append(
                Event(
                    actor="importer:metricflow",
                    authority=f"import:{project.project_name}",
                    causes=(f"asset:{gap.asset_id}",),
                    payload=QuestionOpened(
                        question_id=f"{gap.asset_id}_q{count + 1}",
                        asset_id=gap.asset_id,
                        question=gap.text,
                        alternatives=("define_in_semantic_model", "leave_as_derived", "ignore"),
                        slot="semantic_gap",
                    ),
                )
            )
            recorded += 1
        typer.echo(f"semantic gaps recorded as questions: {recorded}")
        if history is not None:
            h = import_history(reg, load_history(history))
            typer.echo(
                f"history: {h.queries} queries, {h.matched} matched assets, "
                f"{h.usage_rows} usage rows, "
                f"{h.refresh_cost_rows} refresh costs, {h.unmanaged_reads} unmanaged reads, "
                f"{h.commitments_proposed} commitments proposed"
            )
    except VeenaError as exc:
        raise _fail(exc) from exc


def _semantic_yaml(model: SemanticModel) -> dict[str, object]:
    data = model.model_dump(mode="json")
    for policy in data["identity_policies"]:
        policy["entity"] = f"semantic:{policy['entity']['name']}@{policy['entity']['version']}"
        for key in (
            "mapping_source",
            "mapping_fact_key",
            "mapping_entity_key",
            "valid_from",
            "valid_to",
        ):
            if not policy.get(key):
                policy.pop(key, None)
    return data


@import_app.command("infer")
def import_infer(
    registry: RegistryPath = DEFAULT_REGISTRY,
    semantic: Annotated[
        Path, typer.Option("--semantic", help="Semantic model YAML.")
    ] = DEFAULT_IMPORTED_SEMANTIC,
    classifier: ClassifierChoice = "keyword",
    drafter: DrafterChoice = "none",
    only: Annotated[
        list[str] | None, typer.Option(help="Asset ids to infer; default all observed.")
    ] = None,
) -> None:
    """Infer a draft spec for every observed asset and open one question each."""
    from veena.importer import infer_drafts

    try:
        reg = _open_registry(registry)
        model = load_semantic_model(semantic)
        drafts = infer_drafts(
            reg, model, _classifier(classifier), drafter=_drafter(drafter), only=only
        )
    except VeenaError as exc:
        raise _fail(exc) from exc
    for d in sorted(drafts, key=lambda d: -d.min_probability):
        gaps = f" open: {', '.join(d.open_slots)}" if d.open_slots else ""
        typer.echo(f"{d.asset_id:<28} weakest slot p={d.min_probability:.2f}{gaps}")
    typer.echo(f"{len(drafts)} drafts opened as questions")


@question_app.command("confirm")
def question_confirm(
    above: Annotated[
        float, typer.Option(help="Confirm drafts whose weakest slot is at or above this.")
    ] = 0.9,
    registry: RegistryPath = DEFAULT_REGISTRY,
    semantic: Annotated[
        Path, typer.Option("--semantic", help="Semantic model YAML.")
    ] = DEFAULT_IMPORTED_SEMANTIC,
    actor: str = "user:owner",
    authority: str = "role:data_owner",
) -> None:
    """Confirm inferred drafts in one batch, plus any a person already answered confirm_draft."""
    from veena.importer import confirm_drafts

    try:
        reg = _open_registry(registry)
        model = load_semantic_model(semantic)
        confirmed = confirm_drafts(reg, model, above=above, actor=actor, authority=authority)
    except VeenaError as exc:
        raise _fail(exc) from exc
    for asset_id in confirmed:
        typer.echo(f"confirmed {asset_id}")
    typer.echo(f"{len(confirmed)} specs resolved; registry at revision {reg.revision}")


@report_app.command("duplicates")
def report_duplicates(
    registry: RegistryPath = DEFAULT_REGISTRY,
    propose: Annotated[
        bool, typer.Option(help="Record one consolidation proposal per equivalence set.")
    ] = False,
    autonomy: Annotated[
        str, typer.Option(help="Policy autonomy to record on proposals.")
    ] = "shadow",
) -> None:
    """Group specified assets that mean the same thing, with consumers and measured cost."""
    from veena.importer import duplicates_report, propose_consolidations
    from veena.policy import ReorganizationPolicy

    reg = _open_registry(registry)
    report = duplicates_report(reg)
    typer.echo(report.render())
    if propose:
        opened = propose_consolidations(
            reg,
            report,
            as_of=datetime.now(UTC),
            policy=ReorganizationPolicy(autonomy=_autonomy(autonomy)),
        )
        for pid in opened:
            typer.echo(f"proposed {pid} ({autonomy}: recorded, not activated)")


@report_app.command("usage")
def report_usage(registry: RegistryPath = DEFAULT_REGISTRY, asset_id: str = "") -> None:
    """Readers per asset with cadence, class, and whether a commitment exists or is proposed."""
    state = _open_registry(registry).state
    for (aid, consumer), u in sorted(state.usage.items()):
        if asset_id and aid != asset_id:
            continue
        has = any(state.commitments[c].consumer == consumer for c in state.assets[aid].commitments)
        proposed = (aid, consumer) in state.commitment_proposals
        unmanaged = (aid, consumer) in state.unmanaged_reads
        flag = "committed" if has else "proposed" if proposed else "unmanaged" if unmanaged else "-"
        typer.echo(
            f"{aid:<28} {consumer:<24} {u.reader_class:<10} p={u.reader_probability or 0:.2f} "
            f"{u.queries:>5} queries {u.runs_per_day:>7.2f}/day {u.distinct_hours:>2}h {flag}"
        )
    if state.refresh_costs:
        typer.echo("refresh cost over the window:")
        for aid, c in sorted(state.refresh_costs.items(), key=lambda kv: -kv[1].credits):
            if asset_id and aid != asset_id:
                continue
            typer.echo(
                f"  {aid:<28} {c.runs:>3} rebuilds {c.bytes_scanned / 1e9:>8.2f} GB "
                f"{c.credits:>9.4f} credits"
            )
