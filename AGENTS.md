# Veena

Veena is the self-organizing data stack. Consumers declare the data outcome they need and the commitment they expect. The engine decides which assets exist, how they depend on each other, and when to reorganize them. People own meaning: definitions, policies, and decisions.

The design is described in "The Self-Organizing Data Stack" (Ananth Packkildurai, Data Engineering Weekly, Sep 28 2026). This file holds the rules for working in this repo. `CLAUDE.md` points here.

## Vocabulary

Use one name for each concept everywhere: planner, backend, registry, CLI, docs, metrics, tests. Do not introduce synonyms.

| Name | Meaning |
|---|---|
| asset | A persistent logical data product with a stable identity. It declares; the engine acts. |
| specification (spec) | The versioned meaning of an asset: population, grain, measures, window, identity policy, access scope, service requirements. |
| commitment | What the platform promised one consumer about one asset: freshness, correctness tolerance, schema stability, cost ceiling. |
| implementation | One versioned physical realization of a spec: the SQL, its dependencies, and its backend. |
| dependency | A directed edge from an implementation to the asset or source it reads. |
| semantic ref | A versioned pointer into the semantic model, written `semantic:<name>@<version>`. |
| decision | A recorded human choice: alternatives, selected meaning, rationale, authority, scope. |
| proposal | An inspectable graph change the engine wants to make, with affected consumers and assumed revisions. |
| verification | Checks run against a proposal or a live asset, with graded evidence: proof, test, reconciliation, sample. |
| deployment | The versions the engine activated, replaced, or rolled back. |
| revision | The registry's monotonically increasing sequence number. Every activation names the revision it assumed. |
| desired state | The assets and commitments consumers asked for. |
| observed state | What exists: implementations, dependencies, source progress, verification results, health. |
| subsumption | The check that one spec can serve another. Result: equal, serves, serves with operations, or cannot. |
| plan | The backend-neutral description of an implementation: an ordered list of steps over the IR. |
| step | One operation in a plan: source scan, time attribution, window, identity join, aggregate, population join. |
| backend | Renders a plan to SQL for one engine, materializes implementations, and publishes the asset interface. There is no separate executor. |
| interface | What consumers read: a SQL view named by the asset id. Repointing it is how the engine rewires. Say interface, not view, outside SQL. |
| refresh rule | Derived from the spec: incorporation bound, boundary timezone, correction lookback. |
| check | One generated verification query with a grade. Structural checks gate pass or fail; value checks set the grade. |
| grade | Strength of evidence: proof, reconciliation, test, sample. |
| source progress | The newest fact timestamp an implementation has incorporated, per source. |
| coordinator | Runs propose, verify, deploy for one asset and enforces the revision guard. |

## Layout

```
src/veena/
  cli.py          Typer app. The only module that prints to the terminal.
  refs.py         Versioned reference types: semantic, policy, origin.
  spec/           The specification IR and subsumption rules.
  semantic/       The semantic model: entities, sources, calculations, time rules, identity policies.
  planner/        Plan model and the deterministic compiler from spec (or spec plus provider) to plan.
  backends/       Backend protocol; SqlBackend base with dialect hooks; DuckDB and Snowflake backends.
  verification/   Checks generated from spec and semantic model. Never imports the planner.
  coordinator.py  Propose (direct, from provider, or share), implement, verify, deploy, rollback.
  resolver.py     Request text to spec, question, or refusal. Classifier picks refs; drafter phrases questions.
  importer/       dbt manifest, MetricFlow, and query history to observed state; inferred drafts; duplicates report.
  overlap.py      Overlap detection and derivation of a shared intermediate, from specs only.
  policy.py       Workspace policies the engine reads before it acts.
  models/         Classifier and Drafter protocols; adapters: keyword (offline), Claude, Jev; record/replay; calibration.
  evidence.py     Grade and CheckResult, shared by verification and the registry.
  calendar.py     Complete-day window arithmetic in a named timezone.
  registry/       Event-sourced registry: events, SQLite and Postgres stores, projections.
  scenario/       Synthetic DuckDB workspace and the shoply dbt manifest and query history fixture.
tests/            Mirrors src/veena: one module per source module, plus `test_*_review.py` modules that pin review findings.
prd/              One requirements document per phase, with ids and acceptance tests.
```

## Commands

```bash
uv sync --all-extras --all-groups   # everything: runtime, optional adapters, dev tools
uv run veena --help          # the CLI
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Use `uv` for every build, run, lint, dependency change, and one-off script. Never call `python`, `python3`, or `pip` directly, in docs, in shell commands, or in tooling. Run a script with `uv run path/to/script.py` and an inline snippet with `uv run python -c "..."`. uv is the only interpreter path this project supports. Target the newest stable CPython (3.14 as of Oct 2026); bump `requires-python` and `.python-version` together.

## Writing

Write like two engineers talking at a whiteboard.

* Lead with the answer, then the reasoning. Short sentences, one idea each.
* Use bullet points for anything longer than a few sentences.
* Use everyday words. Define an unavoidable technical term once, in one sentence, then use it the same way every time.
* No analogies, metaphors, or dramatic shorthand. Say "the deployment is recorded", not "the deployment has landed"; say "saved", "finished", "running", not "banked", "landed", "armed", "healthy".
* Give an example with "For example," and all of its context: "For example, imagine a table where each row is an agent trace…".
* Give numbers, and say what each is compared with: "52.3 s, compared with 47 s for the unavoidable work alone".
* Do not start a sentence with a number.
* When something failed or is uncertain, say so, and say what would settle it.

## Code

* Comments state constraints the code cannot show, nothing else.
* Docstrings follow Google style: a one-line summary, a blank line, then optional Args, Returns, and Raises. Say what the code does, not why it was written or what it replaced.
* Use one name for each concept across the planner, the backend, the docs, and the metrics. See Vocabulary.
* Type every function. `mypy --strict` must pass.
* Data that crosses a module boundary is a frozen Pydantic model or a frozen dataclass. No dicts as records.
* Enumerations are `StrEnum`.
* Paths are `pathlib.Path`. Times are timezone-aware `datetime` in UTC.
* Library code never prints. The CLI prints. Library code raises typed exceptions from `veena.errors`.
* Deterministic by default. Anything random takes an explicit seed.
* Two kinds of model are used together: a classifier (for example Jev) that picks from a closed set of typed options with a calibrated probability, and a generative model (for example Claude) that drafts SQL or prose. Each sits behind a protocol in `veena.models` with a scripted adapter for tests. The call sites are listed in `prd/00-vision.md` under "Model roles"; adding one means editing that table. A model output is an input to a decision table or a validator, never the decision itself.
* Verification code never reads a candidate implementation's SQL. It generates its own queries from the semantic model.
* Tests use `pytest` plain functions and fixtures. One test module per source module. Name tests `test_<behavior>`.
