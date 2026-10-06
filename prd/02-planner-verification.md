# Phase 2: Planner and verification

Status: Done, Oct 5 2026. Three items are partial and noted under Evidence.

## Goal

Turn a resolved specification into a maintained asset without a person writing SQL or tests. The planner compiles the spec plus the semantic model into an implementation. Verification generates its own checks from the same inputs and grades the evidence. A passing verification lets the coordinator activate the asset.

This phase is where the registry starts doing work. At the end, `customer_spend_30d` goes from `specified` to `active` on DuckDB with a reconciliation that passes, and a planted naive implementation is refused.

## The story this phase adds

The sales application's request is already in the registry. The engine plans a direct aggregation over purchases, joined to customers through the point-in-time identity policy. It derives four checks from the spec. It runs them, records the evidence, and activates the asset behind a stable view. Separately, a naive candidate that joins on account id alone passes schema and uniqueness checks but fails reconciliation, and the engine traces the discrepancy to the join.

## Requirements

### Implementation record

* **R2.1** An implementation is a versioned record with an id, the asset it serves, the spec version it implements, a backend-neutral plan, rendered SQL for one backend, its dependencies on sources or assets, and a refresh rule.
  Acceptance: the record is a frozen model, stored as the payload of an `ImplementationPlanned` event, and projected onto the asset.
* **R2.2** Dependencies are explicit edges from an implementation to each source or asset it reads, with the version it assumes.
  Acceptance: the 30-day implementation lists `purchases`, `identity_mappings`, and `customers` with the semantic model version.

### Planner

* **R2.3** The planner is deterministic. Same spec, same semantic model, same output. No model call anywhere in it.
  Acceptance: planning twice yields byte-identical plans; `grep` finds no LLM import under `veena.planner`.
* **R2.4** The planner produces a backend-neutral plan, an ordered relational algebra over the IR: source scan, identity join, time attribution, filter, aggregate, population join, window. A backend renders the plan to SQL.
  Acceptance: the DuckDB renderer and a plan pretty-printer both consume the same plan object. A second renderer is not required, but the plan type has no DuckDB-specific fields.
* **R2.5** The identity join follows the identity policy: fact timestamp inside `[valid_from, valid_to)`, null `valid_to` meaning still valid.
  Acceptance: the rendered SQL for the bundled policy reproduces the attribution of `approved_spend_sql` exactly on the default scenario.
* **R2.6** Measures use the approved calculation's expression and aggregation, never a column by name.
  Acceptance: a spec citing `purchase_amount_usd@4` renders `CASE WHEN status = 'settled' ...`, so refunds contribute zero.
* **R2.7** Window boundaries follow the spec: trailing N complete calendar days in the spec's timezone, ending at the last complete day.
  Acceptance: on 2026-10-01 the 30-day window is `[2026-09-01, 2026-10-01)`; crossing a day boundary moves both ends.
* **R2.8** When `include_inactive` is true, the plan ends with a left join from the entity's source so every member appears with zero-valued measures.
  Acceptance: the 30-day result has exactly one row per customer, including the 30 inactive ones, each at zero.
* **R2.9** The refresh rule derives from the spec: incorporate source changes within the service requirement, roll the window at each day boundary in the spec's timezone, and apply late corrections to earlier dates.
  Acceptance: the rule is a data structure, not a cron string, with fields for incorporation bound, boundary timezone, and correction lookback. A test checks the three fields for the 30-day spec.
* **R2.10** The planner refuses a spec it cannot compile and says why: unknown semantic ref, calculation on a source the identity policy does not attach, time rule missing for the fact source.
  Acceptance: one failing-plan test per reason, each raising a typed `PlanError` with the ref in the message.
* **R2.10a** When the deterministic renderer cannot express a plan step for a backend, a generative drafter may produce the SQL for that step from the plan. The draft is parsed, its lineage is checked against the plan's declared sources, its output columns are type-checked against the plan, and only then is it used. The event records that the step was drafted.
  Acceptance: a scripted drafter that references an undeclared table is rejected with a `PlanError`; a correct draft is accepted and `ImplementationPlanned` marks the step `drafted`. The bundled 30-day plan uses no drafted steps.

### Executor

* **R2.11** A DuckDB backend materializes an implementation as a table named by implementation id and publishes a stable view named by asset id that points at the active implementation.
  Acceptance: after activation `SELECT * FROM customer_spend_30d` returns the asset; the view's definition names the implementation table.
* **R2.12** Switching the view to another implementation is one statement, so later rewiring does not interrupt readers.
  Acceptance: a test materializes two implementations and repoints the view; row counts before and after match.
* **R2.13** The backend records source progress: the max fact timestamp it has incorporated, per source.
  Acceptance: progress appears on an `ImplementationRefreshed` event and in `veena registry assets`.

### Verification

* **R2.14** Verification derives checks from the spec and the semantic model only. The verification module does not import the planner's SQL and receives only the asset's output relation and the semantic model.
  Acceptance: a static test asserts `veena.verification` has no import from `veena.planner`; the check builder's signature takes a spec, a semantic model, and a relation name.
* **R2.15** Four checks are generated for the 30-day spec: uniqueness on grain keys, population preservation against the entity source, reconciliation of each measure against the approved definition, and a metamorphic conservation check that the summed measure equals the independently computed settled total over the window.
  Acceptance: the check list for the bundled spec has exactly these four, each with a human-readable description.
* **R2.16** Each check yields a graded result: `proof`, `reconciliation`, `test`, or `sample`, in that order of strength, plus pass or fail, plus a stated uncertainty. Structural checks (uniqueness, population) gate pass or fail and do not set the grade. The verification's grade is the strongest grade held by a passing value check; it is `sample` when no value check passed.
  Acceptance: the 30-day verification is graded `reconciliation` with all four passing; with the reconciliation and conservation checks removed it is graded `sample`.
* **R2.17** A naive implementation that joins on account id alone passes uniqueness and population checks and fails reconciliation. The failure names the measure, the magnitude of the discrepancy, and the customers affected.
  Acceptance: the scenario's `naive_spend_sql` materialized as a candidate yields a failed verification whose message lists at least one remapped customer.
* **R2.18** Verification results are recorded as `VerificationRecorded` events citing the proposal and the input versions they ran against.
  Acceptance: the event payload includes source progress at verification time.

### Coordinator

* **R2.19** A proposal to create an asset opens as `ProposalOpened`, verification runs, and only a passing verification leads to `DeploymentActivated`. The activation names the registry revision it assumed and fails if the registry moved.
  Acceptance: the end-to-end test produces the event sequence requested, resolved, committed, proposal, implementation, verification, deployment, and a concurrent commitment registration between verification and activation makes activation raise `RevisionConflictError`.
* **R2.20** Repair attempts are bounded. The engine tries at most a configured number of re-plans after a failed verification and then escalates by opening a question.
  Acceptance: with the bound at 1 and a planner forced to produce the naive join, the registry ends with one failed verification and one `QuestionOpened`.

### CLI

* **R2.21** `veena asset plan ASSET` prints the plan and rendered SQL. `veena asset verify ASSET` runs the checks and prints graded results. `veena asset activate ASSET` runs the full loop. `veena asset show ASSET` prints the trail: deployment, proposal, verification, specification, decisions.
  Acceptance: CLI tests for each; `activate` on the bundled 30-day spec against the default scenario exits 0 and `registry assets` shows `active`.

## Out of scope

Shared intermediates and rewiring (phase 3). Natural language (phase 4). Any backend but DuckDB (phase 5). Scheduling that actually runs on a clock; the refresh rule is computed and stored but a scheduler is not built. Cost measurement.

## Risks and what settles them

* The plan type grows DuckDB-specific fields under time pressure. Settled by R2.4's test that the plan has no backend fields and by keeping the renderer in a separate module.
* The conservation check in R2.15 needs a total that does not route through the identity policy. The semantic model may need a calculation such as `settled_purchase_total` that verification trusts directly. Settled by adding it to `semantic.yaml` with its own version and provenance.
* Verification that happens to agree with the planner because both read the same wrong definition. This is a semantic layer problem, not a verification problem. The record must show which definition version both used so a person can find it.

## Evidence

* The suite, `ruff check`, `ruff format --check`, and `mypy --strict` pass. CI carries the counts.
* The CLI walkthrough in USAGE.md ran `asset plan`, `asset activate`, `asset show`, and `asset verify` on the default scenario on Oct 5 2026 with the output shown there.
* R2.5: the rendered SQL matches `approved_spend_sql` row for row on the default scenario. `tests/backends/test_duckdb.py`.
* R2.17: the naive plan passes uniqueness and population, fails reconciliation and conservation, and the failure names remapped customers. `tests/verification/test_checks.py`.
* R2.19: a commitment registered between verify and deploy raises `RevisionConflictError`. `tests/test_coordinator.py`.
* R2.20: a naive planner with the bound at 1 ends with one failed verification and one question. Same file.

Partial:

* R2.4 asks that a second renderer not be required. None exists yet, so the "no backend fields" guarantee rests on the field-name test in `tests/planner/test_compile.py`, not on a second backend. Phase 5 settles it.
* R2.10 lists three refusal reasons; the compiler also refuses specs with filters or more than one grain key, because neither is supported yet. Those are honest `PlanError`s, not silent drops. Filters and multi-key grains are open work.
* R2.13 reports progress on `ImplementationRefreshed` and in `registry assets`. There is no scheduler yet; refresh happens when the coordinator runs.

* The review that preceded open-sourcing (Oct 5 2026) pinned its findings in `tests/test_sql_review.py`. Rules that changed as a result are listed in CHANGELOG.md.
