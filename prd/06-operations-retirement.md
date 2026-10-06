# Phase 6: Operations and retirement

Status: Done, Oct 6 2026, on the DuckDB scenario. Four items are partial and noted under Evidence.

## Goal

Make the engine operable by someone who did not design the graph, and let the graph shrink when commitments end. This phase adds the operator view, the brakes, repair of live assets, and retirement.

## The story this phase adds

At 3 a.m. a reconciliation on `customer_spend_90d` fails. The on-call engineer opens the operator view. It shows one unmet commitment, the support agent it affects, the deployment two days ago that rewired its dependency, and the engine's proposed repair. The engineer follows the trail to the proposal, the verification evidence, the spec, and the decision that set the identity policy. They pin the 30-day asset to its previous implementation while the engine repairs the daily asset, then release the pin.

Months later the sales application drops its commitment. The engine stops refreshing the 30-day output, keeps its specification and history, and keeps the daily asset the support agent still depends on.

## Requirements

### Operator view

* **R6.1** The operator view answers four questions in order: which commitments are unmet, which consumers they affect, which recent deployments touched them, and what the engine proposes next.
  Acceptance: `veena ops` prints those four sections with no other content above them.
* **R6.2** A commitment is unmet when its freshness bound is exceeded, its last verification failed, its schema changed while marked stable, or its measured cost exceeds its ceiling. Each is computed from events, not from a health flag.
  Acceptance: one test per condition on the scenario produces the commitment in the unmet list with the reason.
* **R6.3** From any unmet commitment, `veena ops trail COMMITMENT` prints deployment, proposal, verification, specification, and decisions in that order, each with its actor and authority.
  Acceptance: the trail for the 90-day commitment after the phase 3 rewiring has five sections and fits on one screen.
* **R6.4** Source lag is reported separately from incorporation lag, because a late source is not an engine failure.
  Acceptance: the view shows both numbers per asset.

### Brakes

* **R6.5** Freeze: an operator can stop reorganization for an asset or a subgraph. Proposals touching frozen assets are recorded but not verified or activated.
  Acceptance: with the daily asset frozen, a new overlapping request builds directly and the rejected proposal cites the freeze.
* **R6.6** Pin: an operator can bind an asset to a known-good implementation. The view points there until the pin is released, regardless of deployments.
  Acceptance: pin, activate a new deployment, confirm the view still resolves to the pinned implementation, release, confirm it moves.
* **R6.7** Replay: an operator can run a proposal's verification against historical inputs at a named source position before trusting it.
  Acceptance: `veena proposal replay ID --at POSITION` produces a verification event marked `replay` that does not affect activation.
* **R6.8** Every brake is an event with actor and authority, and the view shows active brakes.
  Acceptance: `FreezeApplied`, `PinApplied`, and their releases appear in the log and in `veena ops`.

### Repair of live assets

* **R6.9** A failed check on a live asset opens a repair proposal automatically, within the repair bound, and marks the commitment degraded until a passing verification activates the fix.
  Acceptance: corrupting the identity mappings in the scenario makes the next verification fail, a repair proposal open, and the commitment show as degraded.
* **R6.9a** Before planning a repair, the classifier triages the failure into a cause class: source lag, identity mapping change, schema drift, late corrections, calculation drift, or unknown, with a probability. The class selects the repair strategy from a policy table; `unknown` or low probability escalates directly.
  Acceptance: the corrupted-mapping scenario triages to identity mapping change with a scripted 0.9 and selects the re-plan-join strategy; a scripted `unknown` yields a `QuestionOpened` with no repair attempt.
* **R6.9b** The triage input is structured: the failing check, its magnitude, source progress, recent deployments, and recent semantic model changes. The classifier never receives the candidate SQL, so triage stays independent of the author like verification does.
  Acceptance: the triage input type has no SQL field; a static test confirms `veena.repair` does not import the planner.
* **R6.10** During repair, consumers read the last verified implementation, and the view states that the data is as of the last verified source position.
  Acceptance: the asset's view resolves to the pre-failure implementation and `veena ops` prints the position.
* **R6.11** A repair that cannot pass within the bound escalates with one question containing the failing check, the magnitude, the triage class and probability, and the alternatives tried.
  Acceptance: with the bound at 1 and an unfixable corruption, one `QuestionOpened` appears with those four parts.
* **R6.11a** The generative model writes the narration an on-call engineer reads for a failure, a repair proposal, or a rollback, from the structured trail only. The structured trail is stored beside it and is the source of truth.
  Acceptance: `veena ops trail --narrate` prints prose from a scripted drafter; removing the drafter leaves the structured trail intact and the command still works.
* **R6.12** Rollback records who may have read bad data and over what interval, and the view lists those consumers until an operator acknowledges.
  Acceptance: after a rollback, `veena ops` shows an "assess impact" section with the consumers and interval; `veena ops ack` clears it with an event.

### Retirement

* **R6.13** Retirement considers explicit commitments, observed usage, retention policy, and reconstruction cost. Query counts alone never justify it.
  Acceptance: an asset with zero queries but an open commitment is never proposed for retirement; one with zero commitments and a retention policy stays until the policy allows.
* **R6.14** Retirement has graded steps: stop refreshing but keep the materialization, replace a table with a view when latency allows, drop the materialization but keep the specification and history, and finally drop an intermediate nobody depends on.
  Acceptance: releasing the sales commitment moves the 30-day asset through stop-refresh and then view replacement, each as an event, while the daily asset stays active.
* **R6.15** A retired asset's specification, decisions, and history remain readable forever. Only physical work is removed.
  Acceptance: `veena asset show customer_spend_30d` works after retirement and prints the full trail.
* **R6.16** Retirement of a shared intermediate waits until no remaining implementation depends on it.
  Acceptance: releasing both windowed commitments retires both outputs, then the daily asset, in that order.

### Lineage and reproducibility

* **R6.17** Operational lineage is exported in OpenLineage format and decision lineage in W3C PROV, linked by asset and implementation ids.
  Acceptance: `veena export lineage` writes both files for the scenario and they validate against the schemas.
* **R6.18** Every deployment points to the artifacts needed to reproduce it: source snapshots or positions, rendered SQL, backend and version, and retention limits on each.
  Acceptance: the deployment record has the four fields and the CLI warns when a referenced snapshot has expired.

## Out of scope

A web UI; the operator view is CLI first. Alerting integrations; the view is pollable and emits events others can route. Multi-region or multi-tenant operation.

## Risks and what settles them

* Operators distrust a graph they did not draw. Settled by R6.3's five-step trail and by a usability check: a person who has never seen the scenario traces an injected failure to its decision without help. Record the time it took.
* Retirement deletes something a quiet consumer needed. Settled by R6.13 and R6.14's graded steps; the first step is reversible by refreshing again.
* Brakes become permanent and the engine stops reorganizing. Settled by showing brake age in `veena ops` and by a weekly report of frozen and pinned assets.

## Evidence

* **R6.1, R6.4** `tests/test_ops.py::test_view_prints_the_four_sections_first_and_in_order` and `tests/test_cli_ops.py::test_ops_view_has_the_four_sections_then_lags_and_brakes`. The lag table carries two numbers per asset and source.
* **R6.2** One test per condition in `tests/test_ops.py`: freshness from `SourcePositionObserved`, failed live verification, schema change against a stable commitment, cost above the ceiling. Nothing reads a health flag.
* **R6.3** `test_trail_has_five_sections_in_order_with_actors`: five sections, each line with actor and authority, under 40 lines for the 90-day commitment after the rewiring.
* **R6.5 to R6.8** `tests/test_coordinator_brakes.py`: a frozen asset makes the new request build directly and the rejected proposal cites the freeze with actor, authority, and time; a pin holds the interface through a deployment and a rollback until released; replay records verifications marked `replay` that the registry refuses to let a deployment cite (`tests/registry/test_invariants.py::test_replay_verification_never_activates_anything`); `FreezeApplied`, `PinApplied`, and their releases appear in the log and in `veena ops`.
* **R6.9, R6.9a, R6.9b** `test_mapping_change_fails_the_live_check_then_repairs_under_policy`: a moved identity mapping fails the next check, a scripted 0.9 for `identity_mapping_change` selects `re_plan_join`, the repair proposal rebuilds and deploys, and the commitment is no longer degraded. A scripted `unknown` yields one `QuestionOpened` and no repair attempt. `tests/test_repair.py` confirms `TriageInput` has no SQL field and `veena.repair` imports neither the planner nor a backend.
* **R6.10** `test_unfixable_corruption_escalates_with_the_four_parts` and `test_failed_live_verification_is_unmet_and_degraded_until_a_fix_deploys`: the interface resolves to the last verified implementation and `veena ops` prints its source position.
* **R6.11** With the bound at 1 and a duplicated mapping, one question carries the failing check, the magnitude, the triage class and probability, and the alternatives tried.
* **R6.11a** `test_narration_comes_from_the_structured_trail_and_is_recorded`: the drafter receives only the structured trail, the text is a `DraftProduced` event, and without a drafter the command prints the trail and says so.
* **R6.12** `test_rollback_shows_impact_until_acknowledged`: the view lists consumers and interval until `veena ops ack` records `ImpactAcknowledged`.
* **R6.13 to R6.16** `tests/test_retirement.py`: an asset with zero queries and an open commitment is never eligible; observed reads inside the quiet period hold it; the 30-day asset moves through stop refresh, view replacement, and drop as three events while the daily asset stays active; `veena asset show` prints the full trail after retirement; releasing both windowed commitments retires both outputs before the daily asset, which retires last.
* **R6.17** `tests/test_lineage.py`: both files are written, share asset and implementation ids, and pass the structural checks.
* **R6.18** `test_deployments_carry_reproduction_artifacts`: source positions, rendered SQL references, backend and version, and a retention date per artifact; `veena deployment show` warns when a source snapshot has expired.

### Partial

* **R6.9a** The triage classifier in the CLI is the rule baseline. Its probabilities are hand-set; the bundled labeled set has six entries and `veena models calibration triage_failure --classifier rules` reports accuracy 0.83 with expected calibration error 0.12. Real failures labeled by on-call engineers would settle whether a stated 0.75 is right three times in four.
* **R6.14** `stop_refresh` is a record, not a scheduler change, because veena has no scheduler yet. It is reversed by a new commitment on the asset.
* **R6.17** Validation checks the structural rules the export relies on: required RunEvent fields and UUID run ids, known PROV tables and relations that name known ids. The published JSON schemas were not fetched offline. Running both files through the OpenLineage and PROV validators would settle full conformance.
* **Usability check.** Nobody who had not seen the scenario has traced an injected failure to its decision yet. The time it takes is unrecorded.
