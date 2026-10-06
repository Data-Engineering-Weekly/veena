# Phase 3: Overlap and reorganization

Status: Done, Oct 5 2026. Four items are partial and noted under Evidence.

## Goal

Let a second request change the architecture. When two specifications overlap, the engine proposes a shared intermediate, verifies every affected consumer, and rewires existing assets without any consumer noticing. This is the responsibility no current tool takes: authorship of the graph.

## The story this phase adds

The support agent asks for 90-day spend. The engine finds `customer_spend_30d`, sees that a 30-day total cannot supply 60 missing days, and looks at the specifications underneath. Both use the same calculation, identity policy, and calendar boundaries. The engine proposes `customer_spend_daily` with 120 days of history, confirms that much source history exists and that it may use it, backfills, verifies both windows, rewires the 30-day asset, and creates the 90-day asset. The sales application's answer does not change.

Then the harder part. A candidate daily asset has the naive join. The daily output has unique keys and passes its own checks. The 30-day consumer's reconciliation fails and traces to the shared dependency. The engine fixes the join, re-verifies both, and only then activates.

## Requirements

### Finding overlap

* **R3.1** On every new request, the engine runs subsumption between the new spec and every active spec, in both directions.
  Acceptance: the 90-day request yields `cannot` against the 30-day asset with the window reason, and the result is recorded on the proposal as considered alternatives.
* **R3.2** When no active asset serves the new spec, the engine derives candidate shared intermediates from pairs of specs that agree on population entity, identity policy, access policy, timezone, and reaggregable measures. The derived spec takes a daily grain and exactly enough history for the longer window.
  Acceptance: from the bundled 30-day and 90-day specs the planner derives a spec equal to `customer_spend_daily.yaml` except for provenance.
* **R3.3** The derived spec records its provenance: the proposal that created it and the two consumer specs it serves.
  Acceptance: provenance entries point to `proposal:<id>` and both asset refs.
* **R3.4** The engine never proposes sharing across different identity policies, access policies, currencies, or time boundaries, even when outputs would match.
  Acceptance: a 30-day spec with a different identity policy produces no shared candidate.

### Deciding whether to reorganize

* **R3.5** A proposal carries an expected benefit and an expected cost: fact rows processed per refresh for the current graph and the proposed graph, measured on the backend, not estimated by a model. A source-fed plan processes the facts that pass its window; a provider-fed plan processes the pre-aggregated rows that pass its bucket window.
  Acceptance: the proposal payload has both numbers for the daily extraction on the default scenario.
* **R3.6** The engine requires a minimum expected benefit before reorganizing and caps reorganizations per asset per period. Both are workspace policies.
  Acceptance: with the minimum set above the measured benefit, the engine builds the 90-day asset directly and records the rejected sharing proposal with the reason. With the cap at zero, same result.
* **R3.7** A proposal names every affected consumer, every assumed version, and the evidence required before activation.
  Acceptance: the daily extraction proposal lists both commitments and the registry revision it assumed.

### Building the shared asset

* **R3.8** Before building, the engine confirms the required history exists in the sources and that access policy permits reading it.
  Acceptance: a scenario built with 60 days of history makes the 90-day request open a question instead of a proposal.
* **R3.9** Backfill is a tracked operation with progress per source and a target source position.
  Acceptance: `BackfillProgressed` events appear and `veena asset show` reports the position reached.
* **R3.10** The 30-day implementation is re-planned to read from the daily asset with window and population join operations, exactly the operations subsumption reported.
  Acceptance: the new 30-day plan's dependency list is `customer_spend_daily` and `customers`, and the plan contains window and population join steps.

### Verifying every affected consumer

* **R3.11** Verification runs for every asset the proposal touches, not just the new one. Each gets its own evidence against approved definitions.
  Acceptance: the proposal has three verifications: daily, 30-day, 90-day.
* **R3.12** For an existing asset, verification also compares old and new implementations at matching input versions and records that this comparison alone is weak evidence.
  Acceptance: the 30-day verification includes an `equivalence_at_version` check graded `test`, and the overall grade is still set by the reconciliation.
* **R3.13** A defect in the shared asset that passes the shared asset's own checks is caught by a consumer's reconciliation and traced to the dependency.
  Acceptance: with the naive daily candidate, the 30-day reconciliation fails and the failure cites `customer_spend_daily` as the suspected cause.
* **R3.14** After a failed verification the engine revises the plan within the repair bound, re-verifies everything affected, and does not activate anything until all pass.
  Acceptance: the end-to-end test records failed verifications, then passing ones, then one deployment.

### Activating without breaking anyone

* **R3.15** Activation is one registry transaction that switches every affected view and records one `DeploymentActivated` naming all of them.
  Acceptance: the deployment's `activated` map has three entries and all three views resolve after it.
* **R3.16** Activation is bound to the registry revision the proposal assumed. A commitment registered during verification makes activation fail and forces re-verification against the new consumer set.
  Acceptance: the concurrent-registration test ends with two proposals, the second one activated.
* **R3.17** When views cannot switch atomically on a backend, the engine runs a staged migration with compatible versions and explicit consumer transitions, recorded as events.
  Acceptance: on DuckDB the atomic path is used; the staged path is implemented and tested with a backend stub that refuses multi-statement transactions.
* **R3.18** Rollback repoints views to the previous implementations and records which consumers may have read from the rolled-back version and over what interval.
  Acceptance: `DeploymentRolledBack` lists both consumers and the interval; views resolve to the old implementations.

### CLI

* **R3.19** `veena asset request` triggers overlap detection and either builds directly or proposes. `veena proposal list|show|approve|reject` manages proposals when policy requires a person. `veena asset show` prints the dependency trail after rewiring.
  Acceptance: the USAGE walkthrough shows the 90-day request producing a proposal, its approval, and the resulting three assets.

## Evidence

* The suite, `ruff check`, `ruff format --check`, and `mypy --strict` pass. CI carries the counts.
* The CLI walkthrough in USAGE.md ran the 30-day request, then the 90-day request, on the default scenario on Oct 5 2026. The second activation was a `share` proposal with a measured benefit of 881 fact rows per refresh. Three assets ended active, both windowed views match the approved definition row for row, and the 30-day asset reads the daily asset.
* R3.2: the derived spec equals the bundled `customer_spend_daily.yaml` except for provenance. `tests/test_overlap.py`.
* R3.6: with the minimum set above the measured benefit, and separately with the cap at zero, the engine builds directly and records the rejected proposal with the reason. `tests/test_coordinator_share.py`.
* R3.8: a scenario with 60 days of history makes the 90-day request open a question naming the missing history.
* R3.13 and R3.14: a planner that produces the naive join on its first attempt yields three failed verifications, the 30-day failure cites `customer_spend_daily`, then three passing verifications and one deployment.
* R3.16: a commitment registered between verify and deploy raises `RevisionConflictError`; the next activation opens a second proposal and activates.
* R3.17: a backend that reports non-atomic publishing produces one `InterfaceSwitched` event per interface and a deployment marked staged.
* R3.18: rollback repoints the 30-day view to its previous implementation, drops the views that had no predecessor, and records both consumers and the interval.

Partial:

* R3.9 records one `BackfillProgressed` event per shared implementation with the source position reached. There is no incremental backfill; the materialization is one statement on DuckDB.
* R3.10 compiles the rewired plan from a provider for the window and population-join operations only. A consumer needing a rollup across grain keys or a filter still gets `PlanError`.
* R3.12 adds the equivalence check to the existing asset's verification and records it graded `test`. The overall grade still comes from the reconciliation, as specified. The check compares the two implementations at the same `as_of`, which is the same input version on this backend; a real backend will need the source position pinned.
* Cost is a count of fact rows processed, not currency. Phase 5 measures bytes and credits.

## Out of scope

Natural language. Non-DuckDB backends. Cost in currency. Retirement (phase 6).

## Risks and what settles them

* Reorganization churn when demand shifts slightly. Settled by R3.6 and a test that flips demand back and forth under the cap and observes no second reorganization.
* Blast radius: a shared dependency failing takes down more consumers. Settled by recording affected consumers on every proposal (R3.7) and by the operator view in phase 6.
* Backfill cost on large history. Not settled in this phase; the scenario is small. Phase 5 measures it on a real backend.

* The review that preceded open-sourcing (Oct 5 2026) pinned its findings in `tests/test_coordinator_review.py`. Rules that changed as a result are listed in CHANGELOG.md.
