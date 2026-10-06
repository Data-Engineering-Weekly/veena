# Developing veena

This guide is for people changing the code. Read [AGENTS.md](AGENTS.md) first. It holds the vocabulary and the writing and code rules that apply to every change, and this guide does not repeat them.

## Setup

```bash
uv sync --all-extras --all-groups
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

All three checks must pass before a change is done. CI runs them on every push; the badge and the workflow log carry the current counts.

Everything runs through `uv`. `uv run python -c "..."` replaces any inline snippet.

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
* `veena.overlap` runs subsumption between a new spec and every active spec in both directions, and derives a shared daily spec from two windowed specs that agree on entity, identity policy, access policy, timezone, and re-aggregable measures.
* `veena.policy` holds `ReorganizationPolicy`: minimum measured benefit, a cap on reorganizations per asset per period, and whether a person must approve.
* `veena.coordinator` runs the loop: `propose` decides between reading from a provider, creating a shared intermediate, or building direct, and records what it considered; `implement`, `verify`, and `deploy` act on every affected asset, shared asset first. `activate` runs all of it with re-planning up to the repair bound. `deploy` refuses when any affected asset's commitments changed since the proposal opened, records `DeploymentActivated` first, then switches every interface in one transaction when the backend can, or one at a time with an `InterfaceSwitched` event each when it cannot. `rollback` repoints interfaces to what the latest deployment replaced; older deployments cannot be rolled back because a later one already changed what they switched. `approve`, `reject`, and `resume` manage proposals that wait for a person.
* `veena.models` holds the two protocols and their adapters. `KeywordClassifier` is the offline baseline and CLI default; it scores word overlap and abstains with a uniform probability on verdict contexts. `ClaudeClassifier` constrains Claude's structured output to the closed set; `ClaudeDrafter` writes prose. `JevClassifier` posts to an assumed HTTP contract behind an injectable transport. `RecordingClassifier` and `ReplayClassifier` capture and replay real answers for offline tests. `calibration.py` turns labeled predictions into a reliability table. `closed_set` builds the enum a classifier must choose from.
* `veena.importer` reads a dbt manifest (`manifest.py`), records models as observed assets and exposures as commitments (`dbt.py`), maps MetricFlow to a semantic model with gaps (`metricflow.py`), imports query history into usage, refresh cost, unmanaged reads, and proposed commitments (`history.py`), infers one draft spec per observed asset as a question (`infer.py`), confirms drafts into specs (`confirm.py`), and reports equivalence sets and consolidation proposals (`report.py`). Nothing it imports becomes meaning without a decision.
* `veena.sqldialect` holds the SQL that differs by engine: timestamp literals, zone conversion, time buckets, distinct-row counts, session setup. `DuckDBDialect` and `SnowflakeDialect` implement it. The backends and verification share it, so a check renders for the same engine as the plan.
* `veena.backends.sql` is the shared SQL backend over any connection with `execute(sql)`; DuckDB and Snowflake subclass it with a dialect and override the DDL hooks. Snowflake's DDL autocommits, so it reports non-atomic publishing and the coordinator stages switches. Backends also drop implementations, unpublish interfaces, and measure the fact rows a plan processes, which is how proposals get a measured cost.
* `veena.errors` is the exception tree. Everything the library raises on purpose is a `VeenaError` subclass, and the CLI maps that one type to exit code 1.
* `veena.registry.postgres` is the multi-writer store behind the optional `postgres` extra. The revision check runs in a serializable transaction.
* `veena.scenario.dbt_fixture` generates the shoply manifest and query history.
* `veena.resolver` turns a request into a spec, a question, or a refusal. It screens the text, picks each ref slot from a closed set, extracts numbers deterministically, fills unstated service terms from `WorkspaceDefaults`, consumes scoped decisions once so an answered question is never asked again, judges the assembled draft, and records every model call as an event. Thresholds come from `ResolutionPolicy`.
* `veena.registry` is the event log and its projections. `events.py` lists every event kind. `state.py` applies them and enforces the invariants: an event that names an unknown asset, moves an asset to an impossible status, or activates an unverified implementation raises, so the log can never hold a state the projections cannot explain. `registry.py` appends with the revision it last saw and retries a conflict a bounded number of times; callers that need to assume a revision pass `expected_revision` themselves. `store.py` has the memory and SQLite stores.
* `veena.repair` holds the triage call site: `TriageInput` is the structured evidence for a failed check (never SQL), `RuleTriageClassifier` is the offline baseline, and `triage` applies `RepairPolicy` to the classifier's answer. A test confirms the module never imports the planner or a backend.
* `veena.ops` computes the operator view from events: the four unmet conditions (freshness, failed verification, schema, cost), affected consumers, recent deployments, proposals and questions, lags, brakes, and rollbacks awaiting assessment. `trail` follows a commitment back through deployment, proposal, verification, specification, and decisions; `narrate` hands that structured trail to a drafter and records the text.
* `veena.retirement` decides and takes graded steps. `consider` weighs commitments, dependents, pins, freezes, observed reads inside the quiet period, and retention before naming the next step; `Retirer.advance` takes one step per eligible asset, dependents before what they read.
* `veena.lineage` exports OpenLineage run events per deployment and asset, and a PROV-JSON document of entities, activities, and agents, then checks both against the structural rules the export relies on.
* `veena.scenario` builds the synthetic workspace, holds the reference SQL that tests compare against, and can apply drift to the sources so a live asset fails its next check.
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

**Sharing is a measured, gated decision.** The engine builds the shared candidate once to measure fact rows processed per refresh for the current and proposed graphs, then a policy decides. Below the minimum benefit, or over the reorganization cap, the proposal is recorded as rejected with the reason and the engine builds directly. Nothing about the decision comes from a model.

**Consumers read an interface; the engine moves it.** Each implementation is a table named by implementation id. Each asset's interface is a SQL view named by asset id. Rewiring the 30-day asset means building a new table from the daily asset and repointing the view inside the same transaction that publishes the daily and 90-day views.

**A model never sees the whole problem.** The classifier gets a typed context and a closed option set per slot. The drafter gets structured alternatives and evidence with no chosen answer. Neither gets the planner's SQL or verification checks. The resolver assembles the spec; the models only nominate.

**Probabilities are claims until measured.** Every classifier call site keeps a labeled set under `scenario/labels/` and a log under `.veena/calibration/`. `veena models calibration <site>` prints the reliability table. Tune thresholds from that table, not from intuition.

**Imported state is observed, never assumed.** A dbt model becomes an asset with status `observed` and its SQL on record. Its meaning arrives as a draft question whose slots carry probabilities, and a spec exists only after a `DecisionRecorded`. The inference refuses to guess the grain when the model's output lacks the entity's key column, which is how it declines to call a postal-code rollup a customer asset.

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

Findings from a code review are pinned in `tests/test_*_review.py` modules, one per area, so the fix and the failure it prevents stay next to each other.

* One test module per source module, mirrored under `tests/`.
* `tests/conftest.py` exposes `make_spec(...)` for building spec variants in one call, plus `spec_dir` and `semantic_path` fixtures pointing at the bundled files.
* Store tests are parameterized over memory and SQLite so both stay in step.
* The scenario tests build an in-memory DuckDB once per module. They assert the traps exist, not just that rows exist.
* CLI tests use Typer's `CliRunner` and a `tmp_path` registry.

## Roadmap

The full requirements for each phase, with ids and acceptance tests, live in [prd/](prd/README.md). In short, the phases set on Oct 5 2026:

1. **Done.** Registry, spec IR, semantic model, DuckDB scenario, CLI.
2. **Done.** Deterministic planner, DuckDB backend, independent verification, coordinator. The planner produces the point-in-time join; verification catches the naive one and the coordinator escalates with a question.
3. **Done.** Overlap proposals: the shared daily asset is derived from two windowed specs, all three are verified, activation is atomic, the 30-day asset is rewired behind its view, and rollback restores it.
4. **Done.** Request resolution: text to spec through a classifier over closed sets, questions with evidence, scoped decision reuse, screening, judging, and a calibration report.
5. **Done.** Shadow mode on the generated shoply fixture: dbt import, MetricFlow to semantic model, query history to usage and cost, inferred drafts, batch confirmation, the duplicates report, consolidation proposals in shadow, and the Snowflake backend.
6. **Done.** Operations: the operator view ordered by unmet commitments, the brakes (freeze, pin, replay), live checks with triage and repair through the provider, graded retirement, reproduction artifacts on every deployment, and lineage export.

After phase 6 the open work is the Partial list in each PRD: live Snowflake execution, the real Jev contract, calibration data from real failures, and a usability check of the trail with someone who has never seen the scenario.

Open questions that would change the design, and what settles each:

* Which semantic layer format to import first. MetricFlow is the working assumption; a sample manifest from a real dbt project would settle it.
* Whether the registry needs Postgres before phase 4. SQLite is enough for one writer; a second concurrent writer would force the move.
* How to grade evidence from a restricted-dialect equivalence prover. Nothing produces `proof` grade yet.
