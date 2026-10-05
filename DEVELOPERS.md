# Developing veena

This guide is for people changing the code. Read [AGENTS.md](AGENTS.md) first. It holds the vocabulary and the writing and code rules that apply to every change, and this guide does not repeat them.

## Setup

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

All three checks must pass before a change is done. Current state: 88 tests, 0 lint findings, 0 type errors across 55 files.

Everything runs through `uv`. The `python3` on this machine is not used, and `uv run python -c "..."` replaces any inline snippet.

## How the pieces fit

```
request ──► spec (IR) ──► registry events ──► projections ──► CLI output
                │                 ▲    ▲
                ▼                 │    │
          subsumption        coordinator: propose ─► implement ─► verify ─► deploy
                                  │          │           │          │
                                  ▼          ▼           ▼          ▼
                             planner     backend     verification  backend.publish
                           (spec → plan) (plan → SQL, (spec + semantic
                                          materialize)  → checks, never SQL)
```

* `veena.refs` defines the versioned reference types. Every pointer into the semantic model is a `SemanticRef`, every policy pointer is a `PolicyRef`, and every provenance pointer is an `OriginRef`.
* `veena.spec` is the specification IR. `model.py` holds the frozen Pydantic models and the cross-field rules. `subsumption.py` decides whether one spec can serve another. `load.py` reads and writes YAML.
* `veena.semantic` holds the semantic model and its loader. `SemanticModel.fingerprint()` is the version a plan records on its dependencies.
* `veena.planner` is the compiler. `plan.py` is the backend-neutral plan: six step types, `Dependency`, `RefreshRule`, `Implementation`. `compile.py` turns a spec plus the semantic model into a plan and raises `PlanError` for anything it cannot express. No model call appears here, and a test enforces it.
* `veena.backends` renders and runs. `protocol.py` is what every backend does: render, materialize, publish, execute. `duckdb.py` is the first backend. It renders one CTE per step and, when told a step is unsupported, hands that step to a `Drafter` and validates the draft by parse, lineage against declared tables, and output columns.
* `veena.verification` builds checks from the spec and semantic model and runs them against a relation name. It does not import the planner or the backends, and a test enforces it. `veena.evidence` holds `Grade` and `CheckResult` so the registry can store results without importing verification.
* `veena.coordinator` runs the loop for one asset: `propose`, `implement`, `verify`, `deploy`, or `activate` for all four with re-planning up to the repair bound. `deploy` refuses when the asset's commitments changed since the proposal opened.
* `veena.models` holds the `Classifier` and `Drafter` protocols with scripted adapters. Phase 2 uses only `Drafter`, and only for the unsupported-step path.
* `veena.registry` is the event log and its projections. Two event kinds were added in phase 2: `ImplementationPlanned` and `ImplementationRefreshed`. `VerificationRecorded` now carries structured `CheckResult`s and source progress.
* `veena.scenario` builds the synthetic workspace and holds the reference SQL that tests compare against.
* `veena.calendar` computes complete-day window bounds in a named timezone. Both the planner and verification use it; it is arithmetic, not candidate logic.
* `veena.cli` is the only module that prints.

## Design decisions and why

**Specs carry meaning; implementations carry SQL.** An asset keeps its `asset_id` while its implementation changes. That is what lets the engine rewire the 30-day asset to read from a daily asset without the consumer noticing.

**Subsumption works on specs only.** It never parses SQL and never samples data. The check compares population, identity policy, access policy, timezone, grain keys, time bucket, history, measures, and filters. This keeps reuse decisions explainable and keeps the hard problem small. General SQL equivalence is out of scope; a later restricted-dialect prover may add `proof`-grade evidence.

**The registry is event-sourced.** The store holds an append-only log. Projections are rebuilt by replay and are never the source of truth. Each event records an actor, an authority, the events that caused it, and both recorded and effective times. `expected_revision` on append is the primitive the activation guard will use: a deployment names the revision it assumed, and the append fails if a consumer registered in between.

**The facade validates before storing.** `Registry.append` applies the event to a copy of the projection first. If the projection rejects it, nothing reaches the store. For example, a `DeploymentActivated` that cites a failed verification raises `RegistryError` and the revision does not move.

**Verification must be independent.** `veena.verification` receives a spec, the semantic model, a relation name, and a function that runs SQL. It builds its own reference attribution from the identity policy and never sees the candidate's SQL or plan. A static test asserts it imports neither the planner nor the backends. The conservation check compares the asset's total to the raw fact total in the window, which catches a double count even when every per-key row is plausible.

**The plan is backend-neutral.** Six step types, no SQL, no engine names. A backend renders them. A test asserts the serialized plan contains no backend words. This is the guard against phase 5 becoming a rewrite.

**The planner is a compiler, not a model.** Same spec and semantic model give byte-identical plans. The only place a model may write SQL is a step the backend declares it cannot render, and that draft is parsed, lineage-checked against declared tables, and column-checked before use.

**Deploy is guarded twice.** The coordinator compares the asset's commitments to the set it saw when the proposal opened and raises `RevisionConflictError` if they differ. The store then checks the exact revision on the append. The first guard explains why; the second catches any writer.

**Determinism.** The scenario generator takes a seed. Same seed and config give byte-identical tables. Tests rely on this.

## Adding things

### A new event kind

1. Add a value to `EventKind` in `registry/events.py`.
2. Add a payload class with `kind: Literal[EventKind.X] = EventKind.X` and frozen config.
3. Add it to the `Payload` union.
4. Add a `case` in `RegistryState.apply` in `registry/state.py`. Validate references with `_require` and raise `RegistryError` for anything the projection cannot accept.
5. Export it from `registry/__init__.py`.
6. Add a test in `tests/registry/test_registry.py` that covers the happy path and at least one rejection.

### A new subsumption rule

1. Add a private `_check_*` function in `spec/subsumption.py` that appends to `reasons` or `operations`.
2. Call it from `check`. Keep the order: fixed fields, grain, time, measures, filters, population.
3. If it introduces a new planner operation, add it to `Operation` and document it in `USAGE.md`.
4. Add a test per direction in `tests/spec/test_subsumption.py`. Use `make_spec` from `tests/conftest.py`.

### A new spec field

1. Add it to the model in `spec/model.py` with a frozen config and a `Field` description.
2. If it is a ref, add it to `_textify` in `spec/load.py` so `dump_spec` prints the text form.
3. Decide whether subsumption must compare it. If yes, add a rule as above.
4. Update the three bundled specs in `scenario/specs/` and the roundtrip test.

### A new semantic definition type

1. Add a model in `semantic/model.py` inheriting from `_Versioned`.
2. Add a tuple field on `SemanticModel` and a resolver method that calls `_resolve`.
3. Add its name checks to `_check_internal_names` in `semantic/load.py`.
4. Add it to `scenario/semantic.yaml` and to `veena scenario semantic` in `cli.py`.

## Testing conventions

* One test module per source module, mirrored under `tests/`.
* `tests/conftest.py` exposes `make_spec(...)` for building spec variants in one call, plus `spec_dir` and `semantic_path` fixtures pointing at the bundled files.
* Store tests are parameterized over memory and SQLite so both stay in step.
* The scenario tests build an in-memory DuckDB once per module. They assert the traps exist, not just that rows exist.
* CLI tests use Typer's `CliRunner` and a `tmp_path` registry.

## Roadmap

The full requirements for each phase, with ids and acceptance tests, live in [prd/](prd/README.md). In short, the phases agreed on Oct 5 2026:

1. **Done.** Registry, spec IR, semantic model, DuckDB scenario, CLI.
2. **Done.** Deterministic planner, DuckDB backend, independent verification, coordinator. The planner produces the point-in-time join; verification catches the naive one and the coordinator escalates with a question.
3. **Next.** Overlap proposals: derive the shared daily asset from two windowed specs, verify all three, activate atomically, rewire the 30-day asset behind its view.
4. LLM request resolution: natural language to spec, with open questions recorded as `QuestionOpened` and answers as `DecisionRecorded`.
5. Warehouse backends (Snowflake or BigQuery first), the operator view ordered by unmet commitments, and the brakes: freeze, pin, replay.

Open questions that would change the design, and what settles each:

* Which semantic layer format to import first. MetricFlow is the working assumption; a sample manifest from a real dbt project would settle it.
* Whether the registry needs Postgres before phase 4. SQLite is enough for one writer; a second concurrent writer would force the move.
* How to grade evidence from a restricted-dialect equivalence prover. Nothing produces `proof` grade yet.
