# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Phases 1 to 5 of the self-organizing data stack: specification IR and subsumption, semantic model, event-sourced registry with SQLite and Postgres stores, deterministic planner, DuckDB and Snowflake backends, independent verification, coordinator with shared intermediates and rollback, request resolution with classifier and drafter models, dbt and MetricFlow import, query history import, inferred drafts, duplicates report, and the `veena` CLI.
- A generated e-commerce dbt project, shoply, as the import fixture.
- `veena.sqldialect`, so verification renders SQL for the same engine as the plan.
- Registry projection invariants: impossible transitions and unknown references are rejected at append time.
- Optional extras `claude`, `postgres`, and `all`. The core install has no model SDK or Postgres driver.
- Public exports on `veena` and a `main()` entry point on `veena.cli`.

### Changed

- `DeploymentActivated` is recorded before the interface is published, and only the latest deployment can be rolled back.
- Shared proposals compare service freshness as well as meaning, and a stale shared spec is re-derived.
- Resolution consumes an answered decision once, and window and freshness extraction reads lead words rather than any number in the text.
- dbt import handles versioned models, seeds, snapshots, and ephemeral models; history import handles quoted names and `CREATE OR REPLACE`, `MERGE`, and `INSERT` builds.
- Every store, backend, and registry is closeable, and the test suite treats an unclosed resource as a failure.
- `veena.spec.check` is renamed `compare`; the old name stays as an alias.

### Fixed

- A bucketed spec dropped its bucket column; `COUNT` measures failed conservation; naive timestamps were compared with aware ones.
- A concurrent writer could leave the registry with a projection it could not rebuild.
- Drafted SQL was checked against unqualified table names.
- Duplicate ids in an import overwrote earlier assets.
- The CLI created empty registry and scenario files when given a wrong path.
