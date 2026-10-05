# Phase 5: Shadow mode on a real warehouse

Status: Planned. Depends on phase 2. Independent of phases 3 and 4, though both make it more useful.

## Goal

Run beside an existing dbt project and warehouse. Import what exists as observed state, import who depends on it as commitments, find overlap from specifications, and propose. Activate nothing until a person turns the dial. This is the product wedge: immediately useful, no trust required on day one.

## The story this phase adds

A platform team points veena at a dbt manifest and a Snowflake account. The engine imports 400 models as assets. Each gets an inferred spec marked as an open question, because the engine cannot turn a column name into an approved meaning. The team approves a semantic model for customer spend. The engine finds nine models that resolve to the same specification and three more that differ only in window. It proposes one shared daily asset behind twelve existing interfaces, with measured cost and the consumers each change affects. The team reads the proposals. Nothing has changed in the warehouse.

## Requirements

### Import

* **R5.1** `veena import dbt MANIFEST` reads a dbt manifest and records each model as an asset with status `observed`, its SQL as an implementation, and its `depends_on` as dependencies.
  Acceptance: a fixture manifest with 20 models yields 20 assets and the right edge count; re-import is idempotent.
* **R5.2** Imported assets get an inferred spec recorded as a `QuestionOpened`, never as a resolved spec. The inference is a draft for a person to confirm.
  Acceptance: no imported asset reaches `specified` without a `DecisionRecorded`.
* **R5.2a** Inference uses the classifier per slot: grain keys, population entity, measures, identity policy, each chosen from the confirmed semantic model with a probability. Slots with no plausible ref are left open rather than filled.
  Acceptance: on a fixture of 20 models with 5 that match the bundled semantic model, those 5 get drafts with every slot filled and the other 15 have at least one open slot.
* **R5.2b** Questions from import are ranked by the lowest slot probability, and owners can confirm a batch of drafts above a policy threshold in one command.
  Acceptance: `veena questions confirm --above 0.9` records one `DecisionRecorded` per confirmed draft with the actor and the threshold as rationale.
* **R5.2c** The generative model writes a one-paragraph rationale for each draft from the structured slots and the model's SQL, so an owner can read why the classifier chose what it did without reading the SQL.
  Acceptance: the rationale is stored on the question as prose separate from the slots; a scripted drafter yields deterministic text the test checks.
* **R5.3** dbt exposures import as commitments. Warehouse query history, where available, imports as observed usage per asset per consumer.
  Acceptance: an exposure on a model becomes a commitment; a query-history fixture produces `UsageObserved` events.
* **R5.3a** Each query in history is classified by the classifier into managed read, unmanaged read, ad hoc exploration, or scheduled consumer, with a probability. The class drives whether the engine proposes a commitment for a repeat reader.
  Acceptance: a fixture of 1,000 queries classifies in one batch; scheduled consumers above the policy threshold become `CommitmentProposed` events for an owner to accept.
* **R5.4** The semantic layer imports from dbt's MetricFlow definitions where present: semantic models to sources and entities, measures to calculations, and metric time to time rules. Gaps become questions.
  Acceptance: a MetricFlow fixture yields a semantic model that passes the phase 1 loader; unmapped fields appear as questions.

### Overlap across the graph

* **R5.5** Once specs are confirmed, the engine runs subsumption across every pair and groups assets into equivalence sets and serves-with relations.
  Acceptance: on a fixture with nine equal specs and three windowed variants, the report shows one equivalence set of nine and three serves-with edges to a derived daily spec.
* **R5.6** The duplicates report shows, per group, the assets, their consumers, their measured refresh cost, and the specification evidence for equivalence.
  Acceptance: `veena report duplicates` prints the fixture groups with all four columns.
* **R5.7** Proposals in shadow mode are recorded and listed but never activated. A workspace policy `autonomy: shadow` enforces this.
  Acceptance: with the policy set, `veena asset activate` exits with a policy error and no `DeploymentActivated` event exists.

### First real backend

* **R5.8** A Snowflake backend renders the backend-neutral plan to Snowflake SQL, materializes implementations as tables or dynamic tables, and publishes the stable interface as a view.
  Acceptance: the phase 2 plan for the 30-day spec renders to Snowflake SQL that parses with sqlglot in the Snowflake dialect. Live execution is tested manually against a sandbox account and the result recorded in the PR.
* **R5.9** Cost is measured, not estimated: bytes scanned and credits per refresh from the backend's query history.
  Acceptance: a refresh records cost on `ImplementationRefreshed`; proposals cite measured costs for current and proposed graphs.
* **R5.10** The DuckDB backend remains the development and test backend. All phase 2 and 3 tests run on it unchanged.
  Acceptance: the suite passes without Snowflake credentials.
* **R5.11** Backends are pluggable behind one protocol: render, materialize, switch interface, read progress, read cost.
  Acceptance: both backends implement the protocol; a conformance test runs against DuckDB and against a Snowflake stub.

### Consumers that bypass the interface

* **R5.12** The engine detects reads that go to an implementation table directly instead of the asset's view, from query history, and records them as unmanaged consumers. Detection is deterministic on the table name; the classifier (R5.3a) only decides whether the reader looks like a recurring consumer worth contacting.
  Acceptance: a query-history fixture with a direct table read produces an `UnmanagedReadObserved` event and a warning in `veena registry assets`.

### Registry storage

* **R5.13** The registry supports Postgres as a store when more than one writer exists. SQLite stays the default for one writer.
  Acceptance: the store test suite runs against both; the Postgres store uses a serializable transaction for the revision check.

## Out of scope

Autonomy above shadow. Databricks and BigQuery backends; they follow the same protocol later. A web UI; reports are CLI output and files. Cost optimization beyond measurement.

## Risks and what settles them

* Teams without a semantic layer get only questions. Settled by R5.4 import and by measuring how many models get a confirmed spec in the first week of a pilot. If that number is low, the next phase is bootstrap tooling.
* The dbt manifest encodes SQL, not meaning. Inference will be wrong often. Settled by R5.2: inference is always a question. The quality metrics are how often the owner accepts a draft unchanged and the classifier's calibration on the confirmed set, reported with `veena models calibration infer_spec`.
* Snowflake-specific plan fields leak into the neutral plan. Settled by the phase 2 R2.4 test and by the conformance suite.
