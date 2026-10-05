# Vision

## The product

Veena is an orchestration engine that owns the data asset graph. A consumer declares an outcome and a commitment. The engine decides which assets should exist, builds them, verifies them against approved definitions, and reorganizes them when needs overlap or expire. People own meaning: definitions, policies, and judgments. The engine owns the graph.

Today an engineer authors both the data model and the pipeline that keeps it current, and that authored graph ages from the day it is committed. Veena moves authorship of the graph into the engine and leaves authorship of meaning with people.

## Who it serves

| User | What they do with veena | What they need from it |
|---|---|---|
| Consumer, human or agent | Asks for an outcome and registers a commitment. | The same answer at the same service level, however the implementation changes underneath. |
| Data owner | Defines meaning: entities, calculations, identity policies, time rules, access and budget policies. Answers the engine's questions. | One specific question at a time, with evidence and alternatives. A durable record of every decision. |
| Platform engineer | Operates the engine. Builds backends and verification mechanisms it can trust. | A view ordered by unmet commitments. Brakes: freeze, pin, replay. Traces from a failure back to the decision that shaped the asset. |

## Principles

These are the rules every phase must honor. A requirement that violates one is wrong.

1. **Assets declare; the engine acts.** An asset keeps its identity while its implementation changes. No asset runs its own process.
2. **Commitments are first-class.** Who depends on an asset and what was promised live in the registry, separate from the code that fulfills them.
3. **Reuse comes from definitions.** Two assets share work only when their specifications prove the same meaning. Column names, samples, and matching outputs never prove it.
4. **Verification does not trust the author.** Checks are generated from the semantic model. They never read a candidate's SQL. Evidence is graded and uncertainty is stated.
5. **Models nominate and draft; they never authorize.** Two kinds of model appear, each where it is strongest, and only at the call sites listed under "Model roles" below. Deterministic planners, type checks, access enforcement, cost controls, decision tables, and verification keep their jobs. A model output is an input to a decision, never the decision.
6. **Decisions are data.** Every request, clarification, choice, proposal, verification, and deployment is an event with an actor, an authority, and its causes. The log is append-only.
7. **Autonomy has an off switch.** Operators can freeze reorganization, pin implementations, and replay proposals. Nothing ships without those.
8. **Build only what is needed.** A direct implementation serves the first consumer. A later request may justify a shared intermediate. The engine needs a minimum expected benefit before it reorganizes and a cap on how often it does.

## Model roles

Veena uses two kinds of model together, because each fails where the other is strong.

| Kind | Example | What it does well | What it must not do |
|---|---|---|---|
| Classifier | Jev (TypeSafe System One) | Picks from a closed set of typed options in under a second and returns a calibrated probability. Cannot invent an option because the output type forbids it. | Generate SQL or prose. Authorize anything. Count as evidence. |
| Generative | Claude | Drafts SQL from a plan, writes the narration of a failure, phrases a question for an owner. | Choose semantic refs unsupervised. Report its own confidence as a number the engine acts on. Authorize anything. |

The classifier decides what the engine is looking at and how sure it is. The generative model writes the artifact a person or a backend reads. A decision table consumes the probability and decides what happens. Verification checks the result against data regardless of what either model said.

Every call site is listed here and nowhere else. Adding one means editing this table.

| Call site | Phase | Kind | Output |
|---|---|---|---|
| Resolve request text to semantic refs | 4 | Classifier | Refs from the semantic model, with probabilities |
| Judge a drafted spec against the request | 4 | Classifier | Match or mismatch, with probability |
| Detect injection or out-of-policy intent in request text | 4 | Classifier | Flag, with probability |
| Draft the question an owner sees | 4 | Generative | Text, from structured alternatives and evidence |
| Draft SQL from a plan when the deterministic renderer cannot | 2 | Generative | SQL, validated by parse, lineage, and type checks before use |
| Infer a spec for an imported model | 5 | Classifier | Grain, entity, measures as refs, with probabilities |
| Write the rationale for an inferred spec draft | 5 | Generative | Text, from the chosen slots and the imported SQL |
| Classify reads from query history | 5 | Classifier | Managed, unmanaged, ad hoc, scheduled |
| Triage a failed check | 6 | Classifier | Cause class, with probability |
| Narrate a failure or a proposal for a person | 6 | Generative | Text, from the structured trail |

Rules that hold at every call site:

* The output is recorded as an event with the model id, the structured output, and the probability where there is one.
* Thresholds that turn a probability into an action live in policy tables, never in code.
* Tests run offline with scripted adapters. No test calls a model.
* Calibration is measured, not assumed. Each classifier call site keeps a labeled set and reports whether a stated 0.8 is right about 80 percent of the time.

## What success looks like

Not table counts and not pipeline counts. Maintained assets that meet their commitments at acceptable cost. Concretely, by the end of phase 6 on the synthetic scenario:

* Two consumers register overlapping requests and the engine ends with three assets, one rewiring deployment, and both reconciliation checks passing.
* A planted double-count bug is caught by verification before activation, every time, without a hand-written test.
* An ambiguous request produces one question with alternatives, not a guess.
* Releasing one commitment retires one output and keeps the shared intermediate.
* An operator can trace any unmet commitment to the deployment, proposal, verification, specification, and decision behind it in five steps or fewer.

On a real warehouse in shadow mode: a team sees every version of a repeated metric on one screen with the specifications that prove which ones mean the same thing, and the engine's proposals are ones a senior engineer would have made.

## Non-goals

* Replacing dbt or Airflow on day one. Phase 5 runs beside dbt and proposes. Autonomy is a dial turned up as trust accumulates.
* General SQL equivalence. Reuse is decided on the specification IR. A restricted-dialect prover may add proof-grade evidence later; it is not a dependency.
* Building a semantic layer product. Veena imports meaning from where it already lives and helps bootstrap it by recording gaps as open questions. It never auto-approves a definition.
* Human-readable physical models. The physical graph is optimized for reuse, cost, and correctness. Understanding lives in the decision record, not the star schema.
* Hosting or executing on anything but SQL backends in the first six phases. Compute and storage engines do the physical work; veena decides why it exists.

## Constraints

* Python 3.14 or newer, built and run only through uv. See AGENTS.md.
* Deterministic by default. Anything random takes a seed.
* The synthetic DuckDB scenario is the integration suite and the demo. Every phase adds its chapter of the story to it.
* Every model, classifier or generative, sits behind a protocol in `veena.models` with a scripted adapter for tests. The suite passes with the network off.
