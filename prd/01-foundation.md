# Phase 1: Foundation

Status: Done, Oct 5 2026.

## Goal

Make the durable core exist before any planning or model calls: the specification IR, the semantic model, the event-sourced registry, the synthetic scenario, and a CLI that exercises them.

## Requirements

### Specification IR

* **R1.1** A spec models population, grain, measures, window, filters, identity policy, provenance, and service requirements as frozen, validated types.
  Acceptance: the three bundled specs load and roundtrip through YAML unchanged. `tests/spec/test_model.py`.
* **R1.2** Every pointer into the semantic model is a versioned ref of the form `semantic:<name>@<version>`. Provenance pointers are `message:`, `decision:`, `policy:`, or `conversation:`.
  Acceptance: an unversioned semantic ref and a provenance ref of the wrong kind both fail validation.
* **R1.3** Cross-field rules hold: the population entity is a grain key, measure names are unique, a windowed asset sets complete calendar days, a bucketed asset sets history days.
  Acceptance: one failing test per rule.

### Subsumption

* **R1.4** Given a provider spec and a consumer spec, the check returns equal, serves, serves with operations, or cannot. It works on specs only.
  Acceptance: `tests/spec/test_subsumption.py`. No test touches SQL or data.
* **R1.5** The check reports every blocking reason at once, not the first one found.
  Acceptance: a provider differing in identity policy, access policy, and timezone yields exactly three reasons.
* **R1.6** A daily asset with enough history serves a 30-day and a 90-day windowed spec, needing a window operation and a population join. A 30-day asset cannot serve a 90-day one.
  Acceptance: the bundled specs produce exactly those verdicts.
* **R1.7** Measures that cannot be re-aggregated (`avg`, `count_distinct`) block serving from a finer grain.
  Acceptance: a daily `avg` provider cannot serve a windowed `avg` consumer.

### Semantic model

* **R1.8** The model holds versioned sources, entities, calculations, time rules, and identity policies, and resolves refs by name and version.
  Acceptance: resolving an unknown version lists the known versions in the error.
* **R1.9** The loader checks every internal name: a source named by a definition exists, and a column named by a definition is in its source.
  Acceptance: a model whose entity key is not a column fails at load.
* **R1.10** An identity policy carries validity columns so point-in-time attribution is expressible.
  Acceptance: the bundled policy `customer_identity_point_in_time@2` loads with `valid_from` and `valid_to`.

### Registry

* **R1.11** The registry is an append-only event log. Each event records actor, authority, causes, recorded time, and effective time, and carries one typed payload.
  Acceptance: eleven event kinds exist with discriminated payloads. `tests/registry/`.
* **R1.12** The store supports optimistic concurrency: an append may name the revision it assumes and fails if the store has moved.
  Acceptance: `RevisionConflictError` with expected and actual revisions, in both the memory and SQLite stores.
* **R1.13** Projections rebuild from replay and reject events that reference missing records or violate invariants: a decision must pick an offered alternative, a deployment must cite a passing verification, a retirement needs zero commitments.
  Acceptance: one test per invariant; a rejected event never reaches the store and the revision does not move.
* **R1.14** The SQLite store persists across connections.
  Acceptance: an event written by one connection is read back identically by another.

### Scenario

* **R1.15** A generator builds customers, accounts, identity mappings, and purchases into DuckDB from a seed, deterministically.
  Acceptance: two builds with the same seed produce identical purchase tables.
* **R1.16** The scenario plants the two traps the engine must handle: accounts remapped between customers inside the window, and refunded purchases.
  Acceptance: the default build has at least 10 remapped accounts and at least one refund.
* **R1.17** Reference SQL written from the semantic definitions attributes every settled purchase exactly once and returns every customer. A naive join on account id over counts.
  Acceptance: attributed total equals the settled total; the naive total is strictly larger.

### CLI

* **R1.18** `veena` exposes spec validate and compare, scenario build and semantic, asset request, and registry log and assets. Library code never prints.
  Acceptance: `tests/test_cli.py`. `spec compare` exits 2 when the provider cannot serve.

## Out of scope

Planning, execution, verification, model calls, any backend but DuckDB, any UI but the CLI.

## Evidence

46 tests pass. `ruff check`, `ruff format --check`, and `mypy --strict` are clean across 32 source files. The CLI walkthrough in USAGE.md ran end to end on Oct 5 2026.
