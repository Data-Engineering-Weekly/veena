# Phase 4: Request resolution

Status: Next. Depends on phase 2, which is done.

## Goal

Accept a request in a consumer's own words and resolve it into a specification, or into a question when the meaning is not in the semantic model. This phase introduces both model kinds: a classifier picks semantic refs from a closed set with calibrated probabilities, and a generative model phrases the question an owner reads. The phase proves the engine asks instead of guessing, and that it asks because a probability said so, not because a prompt did.

## The story this phase adds

A third consumer asks for household spending over 90 days. The semantic model has two approved household definitions: one groups by primary residence, the other by linked billing accounts. The request names neither. The engine does not pick. It opens one question to the owner with examples of how each definition groups the same customers, which existing assets each could reuse, and which new dependencies each would need. The owner chooses. The engine derives the spec, records the decision with its scope, and reuses it for later household requests only inside that scope.

## Requirements

### Interface

* **R4.1** A request is text plus a consumer identity plus an optional spec fragment. Requests arrive through the CLI and through a Python API; both record `AssetRequested` with the original text.
  Acceptance: `veena asset request --text "..."` without a spec file records the text and opens resolution.
* **R4.2** Resolution produces one of three outcomes: a spec, a question, or a refusal with reasons. Nothing else.
  Acceptance: the outcome type is a closed union; a test covers each branch.

### Resolution

* **R4.3** The resolver may choose only refs that exist in the semantic model. It cannot invent a calculation, entity, or policy, and a column name is never promoted to a definition.
  Acceptance: a request mentioning "revenue" when only `purchase_amount_usd` exists yields a question offering `purchase_amount_usd` or "define a new calculation", not a spec.
* **R4.4** Every resolved requirement carries provenance: the message it came from or the policy that filled it in.
  Acceptance: the resolved 30-day spec from text has provenance on population, window, measures, and identity policy, and `veena asset show` prints each origin.
* **R4.5** When two or more definitions could satisfy the request, the resolver opens a question rather than ranking them.
  Acceptance: the household request yields exactly one `QuestionOpened` with two alternatives and no spec.
* **R4.6** A question includes evidence: for each alternative, example groupings computed from the scenario, assets it could reuse (from subsumption), and new dependencies it would need.
  Acceptance: the question payload has an `evidence` field with three parts per alternative, and the CLI prints them side by side.
* **R4.7** A decision has a scope: this asset, this consumer, or the workspace. The resolver reuses a decision for a later request only when the request falls inside the scope and the deciding actor had authority for that scope.
  Acceptance: after a decision scoped to one asset, a second household request from another consumer opens a new question; after a workspace-scoped decision it does not.
* **R4.8** Service requirements the request does not state are filled from workspace policy and marked as such in provenance.
  Acceptance: a request without freshness gets the policy default with `policy:` provenance.
* **R4.9** When the request's freshness cannot be met within the budget policy, the resolver returns a question listing feasible alternatives and their consequences, not a spec.
  Acceptance: a request for one-minute freshness against a policy allowing five yields a question with at least two alternatives.

### Model boundary

* **R4.10** Two protocols live in `veena.models`. `Classifier` has one method, classify: typed context plus a closed output type in, a value from that type plus a probability out. `Drafter` has one method, draft: structured inputs in, text out. The resolver is the only importer of either. Neither receives other consumers' data, verification checks, or the planner's output.
  Acceptance: both are `Protocol` types with one method each; a static test confirms the importer set.
* **R4.11** Resolution runs classifier first. For each requirement slot (population entity, measures, identity policy, time rule) the classifier picks a ref from the semantic model. The output type is the set of refs of that kind, so an invented ref is a type error, not a validation failure.
  Acceptance: a scripted classifier cannot return a ref outside the set; the adapter raises before the resolver sees it.
* **R4.12** A probability below the policy threshold for its slot opens a question. The question lists the top alternatives with their probabilities as evidence. The threshold is a policy table ref, not a constant.
  Acceptance: with the threshold at 0.8 and a scripted 0.55 versus 0.40 split between two household definitions, one `QuestionOpened` appears with both probabilities; with the threshold at 0.5 a spec is resolved.
* **R4.13** A second classifier call judges the assembled draft spec against the request text and returns match or mismatch with probability. A mismatch above the policy threshold sends the draft back once, then opens a question.
  Acceptance: a scripted judge returning mismatch twice yields a question naming the slots in doubt.
* **R4.14** A classifier call screens request text for injection or out-of-policy intent before resolution. A flag above threshold returns a refusal outcome with the reason, and the text is still recorded.
  Acceptance: a request containing instructions to read an unapproved table produces a refusal, not a spec or a question.
* **R4.15** The generative model phrases the question an owner reads from structured alternatives and evidence. It never chooses among them and never sees the probabilities as instructions.
  Acceptance: the drafter receives a typed `QuestionDraftInput` without a chosen alternative; the stored question keeps the structured alternatives separate from the prose.
* **R4.16** Every model call is an event: `ClassificationProduced` with model id, slot, output, probability, and input hash; `DraftProduced` with model id, input hash, and text. A reviewer can see what each model said and what the engine did with it.
  Acceptance: both event kinds appear in `veena registry log` for the household request.
* **R4.17** Tests run offline with scripted adapters for both protocols. A recording adapter replays real outputs. No test makes a network call.
  Acceptance: the suite passes with network disabled.
* **R4.18** Each classifier call site keeps a labeled set and a calibration report: for outputs stated at probability p, the observed accuracy. The report is a CLI command and a stored artifact.
  Acceptance: `veena models calibration resolve_refs` prints a reliability table from the scenario's labeled requests; the scripted adapter yields a known curve the test asserts.

### Conversation record

* **R4.19** Requests, clarifications, questions, answers, participants, and timestamps form a conversation record linked from the spec.
  Acceptance: `veena asset show household_spend_90d` prints the conversation in order with actors.
* **R4.20** The spec fragment in the article renders from the registry: requirement, ref, source for each, plus proposal, verification, and deployment refs.
  Acceptance: `veena asset show --format yaml` prints a document matching the article's shape for the household asset.

## Out of scope

Chat UI. Multi-turn negotiation beyond question and answer. Learning from decisions beyond scoped reuse. Fine-tuning. Any model-driven verification.

## Risks and what settles them

* The classifier is confidently wrong and well above threshold. Settled only by phase 2 verification downstream; a resolved spec from text goes through the same activate path as a hand-written one, and a test confirms it. Calibration reports (R4.18) show whether this happens often.
* The classifier's probabilities are not calibrated on our data, so thresholds mean nothing. Settled by R4.18 before any threshold is tuned. If a stated 0.8 is right 60 percent of the time, the threshold table is wrong, not the data.
* Question fatigue: too many questions and owners stop answering. Settled by R4.7 scoping, R4.8 policy defaults, and the threshold table; measure questions per request on the scenario and set a target once there is data.
* Prompt injection through request text. Settled by R4.14 screening and R4.11's closed output type. A test sends a request containing instructions to use an unapproved table and confirms a refusal.
* Cardinality. Jev takes up to 255 options per call. A semantic model with more refs of one kind needs a first pass that narrows by entity. Settled by a test with 300 synthetic calculations.
* Vendor availability. Jev is early access. The adapter boundary means a scripted or alternative classifier keeps the suite and the CLI working.
