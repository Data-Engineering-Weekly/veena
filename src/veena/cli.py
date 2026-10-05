"""The `veena` command. This is the only module that prints."""

from __future__ import annotations

from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

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
    from veena.evidence import CheckResult
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


@asset_app.command("request")
def asset_request(
    spec_path: Annotated[Path, typer.Argument(help="Resolved spec to register.")],
    consumer: Annotated[str, typer.Option(help="Who depends on the asset, e.g. app:sales.")],
    text: Annotated[str, typer.Option(help="The request in the consumer's words.")] = "",
    registry: RegistryPath = DEFAULT_REGISTRY,
) -> None:
    """Record a request, its resolved spec, and the consumer's commitment."""
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
) -> None:
    """Propose, implement, verify, and deploy the asset on the scenario database."""
    reg = _open_registry(registry)
    try:
        model = load_semantic_model(_semantic_path(semantic))
        backend = DuckDBBackend(duckdb.connect(str(scenario)))
        outcome = Coordinator(reg, backend, model, repair_bound=repair_bound).activate(
            asset_id, as_of=_as_of(as_of)
        )
    except VeenaError as exc:
        raise _fail(exc) from exc
    if outcome.last_outcome is not None:
        _print_checks(outcome.last_outcome.checks)
    if outcome.activated:
        typer.echo(
            f"activated {asset_id} via {outcome.deployment_id} "
            f"after {outcome.attempts} attempt(s); "
            f"grade {outcome.last_outcome.grade if outcome.last_outcome else '-'}; "
            f"registry at revision {reg.revision}"
        )
    else:
        typer.echo(f"not activated; opened question {outcome.question_id}")
        raise typer.Exit(code=3)


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
def asset_show(asset_id: str, registry: RegistryPath = DEFAULT_REGISTRY) -> None:
    """Print the trail: spec, commitments, deployments, proposals, verifications, decisions."""
    state = _open_registry(registry).state
    asset = state.assets.get(asset_id)
    if asset is None:
        raise _fail(VeenaError(f"unknown asset {asset_id!r}"))
    typer.echo(
        f"asset {asset.asset_id}  status={asset.status}  impl={asset.active_implementation or '-'}"
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
            typer.echo(
                f"deployment {d.deployment_id} ({status}): {d.activated[asset_id]} "
                f"via {d.proposal_id}, verified by {d.verification_id}, "
                f"assumed revision {d.assumed_revision}"
            )
    for p in sorted(state.proposals.values(), key=lambda p: p.seq):
        if asset_id in p.affected_assets:
            typer.echo(f"proposal {p.proposal_id}: {p.summary}")
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
            typer.echo(f"question {q.question_id}: {q.question}{answer}")


def _print_checks(checks: tuple[CheckResult, ...]) -> None:
    for c in checks:
        mark = "ok  " if c.passed else "FAIL"
        kind = "structural" if c.structural else c.grade
        typer.echo(f"  {mark} {c.name:<26} [{kind}] {c.description}")
        if not c.passed or c.uncertainty:
            typer.echo(
                f"       {c.detail}" if not c.passed else f"       uncertainty: {c.uncertainty}"
            )
