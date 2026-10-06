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

app = typer.Typer(no_args_is_help=True)
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
    Path,
    typer.Option(
        "--scenario",
        help="DuckDB file holding the sources and assets. The CLI drives DuckDB only; "
        "Snowflake is used from code.",
    ),
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
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _fail(VeenaError(f"--as-of {text!r} is not an ISO 8601 instant")) from exc
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


def _open_registry(path: Path, *, must_exist: bool = False) -> Registry:
    """Open the registry; the command callback closes it on exit.

    Read-only commands pass `must_exist` so a mistyped path is an error, not an empty registry.
    """
    if must_exist and not path.exists():
        raise _fail(
            VeenaError(
                f"no registry at {path}; run `veena asset request` or `veena import dbt` first"
            )
        )
    try:
        registry = Registry(SqliteEventStore(path))
    except VeenaError as exc:
        raise _fail(exc) from exc
    _OPEN.append(registry)
    return registry


def _scenario_connection(scenario: Path, *, read_only: bool) -> DuckDBBackend:
    if not scenario.exists():
        raise _fail(
            VeenaError(f"no scenario database at {scenario}; run `veena scenario build` first")
        )
    backend = DuckDBBackend(duckdb.connect(str(scenario), read_only=read_only))
    _OPEN.append(backend)
    return backend


_OPEN: list[Registry | DuckDBBackend] = []


@app.callback(result_callback=lambda *a, **k: _close_all())
def _root() -> None:
    """Veena: the self-organizing data stack."""


def _close_all() -> None:
    while _OPEN:
        _OPEN.pop().close()


@registry_app.command("log")
def registry_log(registry: RegistryPath = DEFAULT_REGISTRY, after: int = 0) -> None:
    """Print every event after a revision."""
    reg = _open_registry(registry, must_exist=True)
    if reg.revision <= after:
        typer.echo(f"no events after revision {after}")
    for stored in reg.events(after=after):
        event = stored.event
        typer.echo(
            f"{stored.seq:>5}  {event.recorded_at.isoformat(timespec='seconds')}  "
            f"{event.kind:<24} actor={event.actor} authority={event.authority}"
        )


@registry_app.command("assets")
def registry_assets(registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """List assets with status, spec version, and open commitments."""
    state = _open_registry(registry, must_exist=True).state
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
    from veena.repair import RuleTriageClassifier

    match name:
        case "keyword":
            return KeywordClassifier()
        case "rules":
            return RuleTriageClassifier()
        case "claude":
            return ClaudeClassifier()
        case "jev":
            return JevClassifier()
    raise _fail(VeenaError(f"unknown classifier {name!r}; use keyword, rules, claude, or jev"))


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
        execute = _scenario_connection(scenario, read_only=True).execute
    return reg, Resolver(
        reg,
        model,
        _classifier(classifier),
        drafter=_drafter(drafter),
        policy=ResolutionPolicy(default_threshold=threshold),
        execute=execute,
    )


def _print_alternatives(
    alternatives: tuple[str, ...], evidence: tuple[str, ...], *, per_alternative: bool
) -> None:
    """Evidence is one line per alternative for resolver questions, one per slot for drafts."""
    if per_alternative:
        for alternative, line in zip(alternatives, evidence, strict=False):
            typer.echo(f"  - {alternative}: {line}")
        for alternative in alternatives[len(evidence) :]:
            typer.echo(f"  - {alternative}")
        return
    for line in evidence:
        typer.echo(f"  {line}")
    typer.echo(f"  choose one of: {', '.join(alternatives)}")


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
    consumer: Annotated[str, typer.Option(help="Who depends on the asset, e.g. app:sales.")],
    spec_path: Annotated[
        Path | None, typer.Argument(help="Resolved spec to register. Omit to resolve --text.")
    ] = None,
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
        backend = DuckDBBackend(duckdb.connect())
        _OPEN.append(backend)
        sql, drafted = backend.render(plan, as_of=_as_of(as_of))
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
        backend = _scenario_connection(scenario, read_only=True)
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
    state = _open_registry(registry, must_exist=True).state
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
            "grain.time_bucket": spec.grain.time_bucket.value,
            "measures.calculation": ", ".join(str(m.calculation) for m in spec.measures),
            "measures.aggregation": ", ".join(m.aggregation for m in spec.measures),
        }
        if spec.window.complete_calendar_days is not None:
            values["window.complete_calendar_days"] = str(spec.window.complete_calendar_days)
        if spec.window.history_days is not None:
            values["window.history_days"] = str(spec.window.history_days)
        values |= {
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
    classifier: str = "rules",
    min_probability: float = 0.7,
) -> tuple[Registry, Coordinator]:
    from veena.policy import ReorganizationPolicy, RepairPolicy

    reg = _open_registry(registry)
    model = load_semantic_model(_semantic_path(semantic))
    backend = _scenario_connection(scenario, read_only=False)
    policy = ReorganizationPolicy(
        autonomy=_autonomy(autonomy),
        require_approval=require_approval,
        min_benefit_rows=min_benefit,
        max_reorganizations_per_period=max_reorganizations,
    )
    return reg, Coordinator(
        reg,
        backend,
        model,
        repair_bound=repair_bound,
        policy=policy,
        repair_policy=RepairPolicy(min_probability=min_probability),
        classifier=_classifier(classifier),
    )


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
    state = _open_registry(registry, must_exist=True).state
    if not state.proposals:
        typer.echo("no proposals recorded")
    for p in sorted(state.proposals.values(), key=lambda p: p.seq):
        typer.echo(
            f"{p.proposal_id:<34} {p.kind_of_change:<7} {p.status:<10} "
            f"affects {', '.join(p.affected_assets)}"
        )


@proposal_app.command("show")
def proposal_show(proposal_id: str, registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """Print one proposal: changes, costs, alternatives considered, evidence, status."""
    state = _open_registry(registry, must_exist=True).state
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
    state = _open_registry(registry, must_exist=True).state
    if not state.deployments:
        typer.echo("no deployments recorded")
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
    state = _open_registry(registry, must_exist=True).state
    if not state.questions:
        typer.echo("no questions recorded")
    for q in sorted(state.questions.values(), key=lambda q: q.seq):
        if open_only and q.decision_id:
            continue
        answer = f"-> {state.decisions[q.decision_id].selected}" if q.decision_id else "(open)"
        typer.echo(f"{q.question_id:<30} {q.asset_id:<24} {q.slot or '-':<28} {answer}")


@question_app.command("show")
def question_show(question_id: str, registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """Print one question with its alternatives and evidence."""
    state = _open_registry(registry, must_exist=True).state
    q = state.questions.get(question_id)
    if q is None:
        raise _fail(VeenaError(f"unknown question {question_id!r}"))
    typer.echo(
        f"question {q.question_id} for {q.asset_id} ({q.slot or 'general'}), "
        f"asked by {q.consumer or '-'}"
    )
    typer.echo(f"  {q.question}")
    _print_alternatives(q.alternatives, q.evidence, per_alternative=q.slot != "inferred_spec")
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
    actor: Annotated[str, typer.Option(help="Who is deciding, e.g. user:owner.")] = "user:owner",
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
    classifier: Annotated[
        str,
        typer.Option(
            "--classifier",
            help="keyword or rules (offline baselines), claude (ANTHROPIC_API_KEY), jev (JEV_*).",
        ),
    ] = "keyword",
    labels: Annotated[Path | None, typer.Option(help="Labeled YAML. Default: bundled.")] = None,
    log_dir: Annotated[Path, typer.Option(help="Where labeled predictions accumulate.")] = Path(
        ".veena/calibration"
    ),
) -> None:
    """Run the labeled set through a classifier and print the reliability table.

    Sites: `resolve_refs` (default classifier keyword) and `triage_failure` (use `--classifier
    rules` for the offline baseline).
    """
    import yaml

    from veena.models import CalibrationLog, CalibrationRecord, calibration_report, closed_set
    from veena.resolver import SlotContext

    if site not in {"resolve_refs", "triage_failure"}:
        raise _fail(
            VeenaError(f"no labeled set for site {site!r}; resolve_refs and triage_failure exist")
        )
    path = labels or Path(str(files("veena.scenario") / "labels" / f"{site}.yaml"))
    if not path.exists():
        raise _fail(VeenaError(f"no labeled set at {path}"))
    entries = yaml.safe_load(path.read_text())
    clf = _classifier(classifier)
    log = CalibrationLog(log_dir / f"{site}.jsonl")
    records = []
    if site == "triage_failure":
        from veena.policy import CauseClass
        from veena.repair import TriageInput

        for entry in entries:
            expected = str(entry.pop("expected"))
            context = TriageInput.model_validate(entry)
            picked = clf.classify(context, CauseClass)
            record = CalibrationRecord(
                site=site,
                slot="cause",
                model_id=picked.model_id,
                predicted=picked.value.value,
                expected=expected,
                probability=picked.probability,
            )
            log.append(record)
            records.append(record)
        typer.echo(calibration_report(site, tuple(records)).render())
        return
    model = load_semantic_model(_semantic_path(semantic))
    options_by_slot = {
        "population.entity": {e.name: e.description or e.name for e in model.entities},
        "measures.calculation": {c.name: c.description or c.name for c in model.calculations},
    }
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

    try:
        manifest, history = write_fixture(out, DbtFixtureConfig(history_days=history_days))
    except OSError as exc:
        raise _fail(exc) from exc
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
            f"{report.assets_updated} updated, {report.assets_unchanged} unchanged, "
            f"{report.assets_skipped} ephemeral skipped; "
            f"{report.commitments_added} commitments added, {report.commitments_unchanged} kept"
        )
        for asset_id in report.missing:
            typer.echo(f"  missing from this manifest: {asset_id} (retirement is a later phase)")
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
    except (VeenaError, OSError) as exc:
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
    force: Annotated[
        bool, typer.Option(help="Re-infer assets whose draft a person rejected.")
    ] = False,
) -> None:
    """Infer a draft spec for every observed asset and open one question each."""
    from veena.importer import infer_drafts

    if not semantic.exists():
        raise _fail(
            VeenaError(
                f"no semantic model at {semantic}; run `veena import dbt` first or pass --semantic"
            )
        )
    try:
        reg = _open_registry(registry, must_exist=True)
        model = load_semantic_model(semantic)
        drafts = infer_drafts(
            reg, model, _classifier(classifier), drafter=_drafter(drafter), only=only, force=force
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
) -> None:
    """Confirm inferred drafts in one batch, plus any a person already answered confirm_draft.

    Threshold confirmations are recorded as the engine's decisions under the resolution policy.
    """
    from veena.importer import confirm_drafts

    if not semantic.exists():
        raise _fail(
            VeenaError(
                f"no semantic model at {semantic}; run `veena import dbt` first or pass --semantic"
            )
        )
    try:
        reg = _open_registry(registry, must_exist=True)
        model = load_semantic_model(semantic)
        confirmed = confirm_drafts(reg, model, above=above)
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
    state = _open_registry(registry, must_exist=True).state
    if not state.usage:
        typer.echo("no usage recorded; import query history with `veena import dbt --history`")
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


# ---- phase 6: operations, brakes, repair, retirement, lineage -----------------------------

ops_app = typer.Typer(
    invoke_without_command=True,
    help="The operator view: unmet commitments, consumers, deployments, proposals, brakes.",
)
commitment_app = typer.Typer(no_args_is_help=True, help="List and release commitments.")
retire_app = typer.Typer(no_args_is_help=True, help="Graded retirement of unused assets.")
export_app = typer.Typer(no_args_is_help=True, help="Export lineage.")
app.add_typer(ops_app, name="ops")
app.add_typer(commitment_app, name="commitment")
app.add_typer(retire_app, name="retire")
app.add_typer(export_app, name="export")

Actor = Annotated[str, typer.Option(help="Who is acting, e.g. user:oncall.")]
Authority = Annotated[str, typer.Option(help="Under what authority, e.g. role:operator.")]
Reason = Annotated[str, typer.Option(help="Why. Recorded on the event.")]


@ops_app.callback()
def ops(
    ctx: typer.Context,
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
    as_of: AsOf = None,
    recent_days: Annotated[int, typer.Option(help="How far back a deployment is recent.")] = 7,
) -> None:
    """Print the operator view. With a scenario present, source positions are observed first."""
    if ctx.invoked_subcommand is not None:
        return
    from veena.ops import operator_view, render_view

    instant = _as_of(as_of)
    try:
        if scenario.exists():
            reg, coordinator = _coordinator(registry, scenario, semantic)
            coordinator.observe_sources(as_of=instant)
        else:
            reg = _open_registry(registry, must_exist=True)
        view = operator_view(reg.state, as_of=instant, recent_days=recent_days)
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(render_view(view, reg.state))


@ops_app.command("trail")
def ops_trail(
    commitment_id: str,
    registry: RegistryPath = DEFAULT_REGISTRY,
    narrate: Annotated[
        bool, typer.Option("--narrate", help="Add prose from the drafter under the trail.")
    ] = False,
    drafter: DrafterChoice = "none",
) -> None:
    """Deployment, proposal, verification, specification, decisions, with actor and authority."""
    from veena.ops import narrate as narrate_trail
    from veena.ops import trail

    reg = _open_registry(registry, must_exist=True)
    try:
        record = trail(reg.state, commitment_id)
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(record.render())
    if narrate:
        chosen = _drafter(drafter)
        if chosen is None:
            typer.echo("")
            typer.echo("narration: no drafter configured; the structured trail above is complete")
            return
        try:
            text = narrate_trail(reg, record, chosen)
        except VeenaError as exc:
            raise _fail(exc) from exc
        typer.echo("")
        typer.echo(f"narration: {text}")


@ops_app.command("ack")
def ops_ack(
    deployment_id: str,
    reason: Reason,
    actor: Actor = "user:oncall",
    authority: Authority = "role:operator",
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
) -> None:
    """Record that the readers of a rolled-back deployment were assessed."""
    try:
        _, coordinator = _coordinator(registry, scenario, semantic)
        coordinator.acknowledge_impact(
            deployment_id, reason=reason, actor=actor, authority=authority
        )
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"acknowledged impact of {deployment_id}")


@ops_app.command("freeze")
def ops_freeze(
    asset_id: str,
    reason: Reason,
    subgraph: Annotated[
        bool, typer.Option("--subgraph", help="Also freeze every asset that reads this one.")
    ] = False,
    actor: Actor = "user:oncall",
    authority: Authority = "role:operator",
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
) -> None:
    """Stop reorganization of an asset. Proposals touching it are recorded and refused."""
    try:
        _, coordinator = _coordinator(registry, scenario, semantic)
        coordinator.freeze(
            asset_id, reason=reason, actor=actor, authority=authority, include_dependents=subgraph
        )
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"frozen {asset_id}" + (" and its dependents" if subgraph else ""))


@ops_app.command("unfreeze")
def ops_unfreeze(
    asset_id: str,
    reason: Reason = "released",
    actor: Actor = "user:oncall",
    authority: Authority = "role:operator",
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
) -> None:
    """End a freeze."""
    try:
        _, coordinator = _coordinator(registry, scenario, semantic)
        coordinator.unfreeze(asset_id, reason=reason, actor=actor, authority=authority)
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"unfrozen {asset_id}")


@ops_app.command("pin")
def ops_pin(
    asset_id: str,
    implementation_id: str,
    reason: Reason,
    actor: Actor = "user:oncall",
    authority: Authority = "role:operator",
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
) -> None:
    """Bind the asset's interface to one implementation until `veena ops unpin`."""
    try:
        _, coordinator = _coordinator(registry, scenario, semantic)
        coordinator.pin(
            asset_id, implementation_id, reason=reason, actor=actor, authority=authority
        )
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"pinned {asset_id} to {implementation_id}")


@ops_app.command("unpin")
def ops_unpin(
    asset_id: str,
    reason: Reason = "released",
    actor: Actor = "user:oncall",
    authority: Authority = "role:operator",
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
) -> None:
    """Release a pin; the interface moves to the active implementation."""
    try:
        reg, coordinator = _coordinator(registry, scenario, semantic)
        coordinator.unpin(asset_id, reason=reason, actor=actor, authority=authority)
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"unpinned {asset_id}; interface at {reg.state.interface_of(asset_id) or '-'}")


@ops_app.command("brakes")
def ops_brakes(registry: RegistryPath = DEFAULT_REGISTRY, as_of: AsOf = None) -> None:
    """The weekly report: every active freeze and pin with its age."""
    from veena.ops import operator_view

    state = _open_registry(registry, must_exist=True).state
    view = operator_view(state, as_of=_as_of(as_of))
    if not view.brakes:
        typer.echo("no active brakes")
    for b in view.brakes:
        days = b.age_seconds / 86400
        typer.echo(
            f"{b.kind:<6} {b.asset_id:<28} {days:>6.1f} days  by {b.actor} ({b.authority})  "
            f"{b.detail}"
        )


@asset_app.command("check")
def asset_check(
    asset_id: str,
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
    as_of: AsOf = None,
    classifier: Annotated[
        str,
        typer.Option(
            "--classifier", help="rules (offline baseline), keyword, claude, or jev for triage."
        ),
    ] = "rules",
    min_probability: Annotated[
        float, typer.Option(help="Triage probability below which the engine asks (policy knob).")
    ] = 0.7,
    repair_bound: Annotated[int, typer.Option(help="Repair attempts before escalating.")] = 2,
) -> None:
    """Check the live interface; on failure triage the cause and repair under policy."""
    try:
        reg, coordinator = _coordinator(
            registry,
            scenario,
            semantic,
            repair_bound=repair_bound,
            classifier=classifier,
            min_probability=min_probability,
        )
        outcome = coordinator.check_live(asset_id, as_of=_as_of(as_of))
    except VeenaError as exc:
        raise _fail(exc) from exc
    live = reg.state.live_verifications[outcome.verification_id]
    _print_checks(live.checks)
    if outcome.passed:
        typer.echo(f"{outcome.verification_id} passed; grade {live.grade}")
        return
    assert outcome.triage is not None
    typer.echo(
        f"{outcome.verification_id} failed; {outcome.triage.summary()} [{outcome.triage.model_id}]"
    )
    if outcome.frozen_by:
        typer.echo(
            f"repair {outcome.proposal_id} recorded and refused: {outcome.frozen_by} is frozen"
        )
        raise typer.Exit(code=3)
    if outcome.activation is not None:
        _report_activation(outcome.activation, reg)
        return
    if outcome.question_id:
        typer.echo(f"opened question {outcome.question_id}; the commitment is degraded")
        raise typer.Exit(code=3)
    typer.echo("waiting for the source; the commitment is degraded")
    raise typer.Exit(code=3)


@proposal_app.command("replay")
def proposal_replay(
    proposal_id: str,
    at: Annotated[str, typer.Option("--at", help="Source position to replay at, ISO 8601.")],
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    semantic: SemanticPath = None,
) -> None:
    """Rebuild the proposal's implementations at a past position and verify them there."""
    try:
        _, coordinator = _coordinator(registry, scenario, semantic)
        outcomes = coordinator.replay(proposal_id, at=_as_of(at))
    except VeenaError as exc:
        raise _fail(exc) from exc
    for outcome in outcomes:
        typer.echo(f"replay of {outcome.relation} at {outcome.as_of.isoformat()}:")
        _print_checks(outcome.checks)
    passed = all(o.passed for o in outcomes)
    typer.echo(
        f"replay {'passed' if passed else 'failed'}; recorded as replay verifications that no "
        "deployment can cite"
    )
    if not passed:
        raise typer.Exit(code=3)


@deployment_app.command("show")
def deployment_show(
    deployment_id: str, registry: RegistryPath = DEFAULT_REGISTRY, as_of: AsOf = None
) -> None:
    """Print one deployment with the artifacts needed to reproduce it."""
    state = _open_registry(registry, must_exist=True).state
    d = state.deployments.get(deployment_id)
    if d is None:
        raise _fail(VeenaError(f"unknown deployment {deployment_id!r}"))
    status = "rolled back" if d.rolled_back else "active"
    typer.echo(
        f"deployment {d.deployment_id} ({status}) at {d.effective_at.isoformat()} by {d.actor} "
        f"({d.authority}); proposal {d.proposal_id}; assumed revision {d.assumed_revision}"
    )
    for asset_id, impl_id in d.activated.items():
        typer.echo(f"  {asset_id} -> {impl_id} (replaced {d.replaced.get(asset_id) or 'nothing'})")
    if d.artifacts is None:
        typer.echo("  no reproduction artifacts recorded")
        return
    a = d.artifacts
    typer.echo(f"  backend {a.backend} {a.backend_version}")
    for source, position in sorted(a.source_positions.items()):
        typer.echo(f"  source position {source} @ {position.isoformat()}")
    for asset_id, impl_id in a.sql_refs.items():
        typer.echo(f"  rendered SQL for {asset_id}: implementation {impl_id}")
    now = _as_of(as_of)
    for name, expires in sorted(a.retention.items()):
        if expires is None:
            typer.echo(f"  retention {name}: kept forever")
        elif expires < now:
            typer.echo(
                f"  WARNING retention {name}: expired {expires.isoformat()}; the deployment can "
                "no longer be reproduced from the warehouse"
            )
        else:
            typer.echo(f"  retention {name}: until {expires.isoformat()}")


@commitment_app.command("list")
def commitment_list(registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """List commitments with consumer, bound, and whether released."""
    state = _open_registry(registry, must_exist=True).state
    if not state.commitments:
        typer.echo("no commitments recorded")
    for c in sorted(state.commitments.values(), key=lambda c: c.seq):
        status = "released" if c.released else "open"
        typer.echo(
            f"{c.commitment_id:<36} {c.consumer:<20} within {c.incorporate_within_seconds}s "
            f"{status}"
        )


@commitment_app.command("release")
def commitment_release(
    commitment_id: str,
    reason: Reason = "no longer needed",
    actor: Actor = "user:owner",
    authority: Authority = "role:consumer_owner",
    registry: RegistryPath = DEFAULT_REGISTRY,
) -> None:
    """Record that a consumer no longer depends on the asset."""
    from veena.registry import CommitmentReleased

    reg = _open_registry(registry, must_exist=True)
    try:
        reg.append(
            Event(
                actor=actor,
                authority=authority,
                causes=(f"commitment:{commitment_id}",),
                payload=CommitmentReleased(commitment_id=commitment_id, reason=reason),
            )
        )
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(f"released {commitment_id}; registry at revision {reg.revision}")


@retire_app.command("plan")
def retire_plan(
    registry: RegistryPath = DEFAULT_REGISTRY,
    as_of: AsOf = None,
    keep_days: Annotated[
        int, typer.Option(help="Days a table is kept after its last commitment ends.")
    ] = 30,
) -> None:
    """Show the next retirement step for every asset, and why it waits."""
    from veena.policy import RetentionPolicy
    from veena.retirement import consider

    state = _open_registry(registry, must_exist=True).state
    policy = RetentionPolicy(keep_materialization_days=keep_days)
    instant = _as_of(as_of)
    rows = [
        consider(state, a.asset_id, as_of=instant, policy=policy)
        for a in sorted(state.assets.values(), key=lambda a: a.asset_id)
        if a.status in ("active", "dormant")
    ]
    if not rows:
        typer.echo("nothing to retire")
    for d in rows:
        step = d.next_step.value if d.next_step else "retire"
        verdict = f"next: {step}" if d.eligible else f"waits before {step}"
        typer.echo(f"{d.asset_id:<28} {verdict:<32} {'; '.join(d.reasons) or '-'}")


@retire_app.command("advance")
def retire_advance(
    registry: RegistryPath = DEFAULT_REGISTRY,
    scenario: ScenarioPath = DEFAULT_SCENARIO,
    as_of: AsOf = None,
    asset_id: Annotated[str, typer.Option(help="Only this asset.")] = "",
    keep_days: Annotated[
        int, typer.Option(help="Days a table is kept after its last commitment ends.")
    ] = 30,
    until_stable: Annotated[
        bool, typer.Option("--until-stable", help="Keep stepping until nothing changes.")
    ] = False,
) -> None:
    """Take the next retirement step for every eligible asset, dependents first."""
    from veena.policy import RetentionPolicy
    from veena.retirement import Retirer

    reg = _open_registry(registry, must_exist=True)
    backend = _scenario_connection(scenario, read_only=False)
    retirer = Retirer(reg, backend, policy=RetentionPolicy(keep_materialization_days=keep_days))
    instant = _as_of(as_of)
    total = 0
    try:
        while True:
            taken = retirer.advance(as_of=instant, asset_id=asset_id or None)
            for step in taken:
                typer.echo(f"{step.asset_id:<28} {step.label:<22} {step.detail}")
            total += len(taken)
            if not taken or not until_stable:
                break
    except VeenaError as exc:
        raise _fail(exc) from exc
    if total == 0:
        typer.echo("no asset is ready for a retirement step; see `veena retire plan`")


@export_app.command("lineage")
def export_lineage_command(
    registry: RegistryPath = DEFAULT_REGISTRY,
    out: Annotated[Path, typer.Option(help="Directory for the two files.")] = Path(
        ".veena/lineage"
    ),
) -> None:
    """Write OpenLineage run events and a W3C PROV document linked by asset and implementation."""
    from veena.lineage import export_lineage

    state = _open_registry(registry, must_exist=True).state
    ol_path, prov_path, problems = export_lineage(state, out)
    typer.echo(f"wrote {ol_path} and {prov_path}")
    if problems:
        for problem in problems:
            typer.echo(f"  problem: {problem}")
        raise typer.Exit(code=3)
    typer.echo("both documents pass the structural checks")


@scenario_app.command("drift")
def scenario_drift(
    kind: Annotated[
        str, typer.Argument(help="mapping_change, duplicate_mapping, or late_purchases.")
    ],
    scenario: ScenarioPath = DEFAULT_SCENARIO,
) -> None:
    """Change the sources after an asset was built, so the next live check fails."""
    from veena.scenario import DriftKind, apply_drift

    try:
        chosen = DriftKind(kind)
    except ValueError as exc:
        raise _fail(VeenaError(f"unknown drift {kind!r}; use {', '.join(DriftKind)}")) from exc
    backend = _scenario_connection(scenario, read_only=False)
    try:
        note = apply_drift(backend.connection, chosen)
    except VeenaError as exc:
        raise _fail(exc) from exc
    typer.echo(note)


def main() -> None:
    """Console entry point."""
    app()


__all__ = ["app", "main"]
