# Phase 6: Operations and retirement

Status: Next. Phases 3 and 5 are done.

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
