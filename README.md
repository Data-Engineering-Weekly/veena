# veena

Veena is a self-organizing data stack. A consumer, human or agent, asks for a data outcome and the commitment they need. The engine decides which assets should exist, builds them, verifies them against approved definitions, and reorganizes the graph when later requests overlap. People keep ownership of meaning: definitions, policies, and decisions.

The design comes from "The Self-Organizing Data Stack" (Ananth Packkildurai, Data Engineering Weekly, Sep 28 2026).

## The idea in one example

A sales application asks for each customer's spend over the previous 30 complete UTC days. No such asset exists. The engine resolves the request into a specification, builds a direct aggregation over purchases, verifies it, and publishes `customer_spend_30d`.

A month later a support agent asks for the same metric over 90 days. The engine compares the two specifications and finds they share the same amount definition, identity policy, and calendar boundaries. It proposes a customer-by-day asset, verifies both windows against it, and rewires the 30-day asset to read from it. The sales application sees the same answer through a different implementation.

Veena is the engine that owns that rewiring.

## Status

Early build. Phases 1 to 5 are done. These parts exist and are tested:

| Part | What it does |
|---|---|
| Specification IR | Models what an asset means: population, grain, measures, window, identity policy, service requirements. |
| Subsumption | Decides whether one spec can serve another, from specs alone. Reports the operations needed or every reason it cannot. |
| Semantic model | Entities, sources, approved calculations, time rules, identity policies, all versioned. |
| Registry | Event-sourced log of requests, specs, commitments, questions, decisions, proposals, verifications, deployments. SQLite store with revision checks. |
| Scenario | Synthetic DuckDB workspace that reproduces the article's story, including the double-count trap. |
| Planner | Deterministic compiler from a spec plus the semantic model to a backend-neutral plan. No model call. |
| DuckDB backend | Renders a plan to SQL, materializes an implementation, publishes the asset's view, reports source progress. |
| Verification | Generates uniqueness, population, reconciliation, and conservation checks from the semantic model. Never reads the candidate's SQL. Grades its evidence. |
| Coordinator | Propose, implement, verify, deploy. Re-plans up to a bound, then asks a person. Refuses to deploy if a commitment changed. Switches every affected interface in one deployment and can roll it back. |
| Overlap | Finds which existing asset can serve a new request, or derives a shared daily intermediate that serves two windowed requests. From specs only. |
| Policy | Minimum measured benefit, a reorganization cap, and optional human approval gate every share proposal. Thresholds turn classifier probabilities into questions. |
| Resolver | Turns a request in a consumer's words into a spec, a question, or a refusal. Refs come only from the semantic model's closed sets. Every model call is an event. |
| Models | Two kinds behind two protocols: a classifier (Jev adapter, Claude adapter, offline keyword baseline) and a drafter (Claude). Record, replay, and a calibration report. |
| Importer | Reads a dbt manifest into observed assets and exposure commitments, MetricFlow into a semantic model with gaps as questions, and query history into usage, refresh cost, unmanaged reads, and proposed commitments. Infers a draft spec per asset as a question; confirms batches above a threshold. |
| Reports | Duplicates: equivalence sets across confirmed specs with owners, consumers, and measured credits, plus consolidation proposals recorded in shadow. Usage: readers per asset with cadence and class. |
| Snowflake | A second backend behind the same protocol. Renders Snowflake SQL, stages interface switches because its DDL autocommits. Render is tested offline; execution needs a live connection. |
| CLI | `veena` wraps all of the above. |

Not built yet: the operator view, brakes, repair of live assets, and graded retirement. Phase 6.

## Install

Veena needs Python 3.14 and [uv](https://docs.astral.sh/uv/). From a clone:

```bash
uv sync
```

Optional adapters install as extras. `claude` adds the Anthropic SDK for the Claude classifier and drafter. `postgres` adds psycopg for a shared registry. `all` adds both.

```bash
uv sync --extra all
```

Every command runs as `uv run veena ...`. State lives in `.veena/` under the working directory: the registry at `.veena/registry.sqlite`, the DuckDB scenario at `.veena/scenario.duckdb`, recorded model calls under `.veena/calibration`. Delete the directory to start over. `uv run veena spec examples` prints the bundled specification files.

## Quick start

```bash
uv run veena scenario build
uv run veena asset request src/veena/scenario/specs/customer_spend_30d.yaml --consumer app:sales
uv run veena asset activate customer_spend_30d
uv run veena asset request src/veena/scenario/specs/customer_spend_90d.yaml --consumer agent:support
uv run veena asset activate customer_spend_90d
uv run veena asset request --consumer agent:crm --asset-id household_spend_90d \
  --text "household spending over the previous 90 complete calendar days"
uv run veena asset show customer_spend_30d
```

Shadow mode on a dbt project, using the bundled e-commerce fixture:

```bash
uv run veena scenario dbt
uv run veena import dbt .veena/dbt/manifest.json --history .veena/dbt/query_history.jsonl
uv run veena import infer
uv run veena question confirm --above 0.5
uv run veena report duplicates --propose
```

See [USAGE.md](USAGE.md) for every command and the file formats. See [prd/](prd/README.md) for the requirements of every phase. See [DEVELOPERS.md](DEVELOPERS.md) to work on the code. See [AGENTS.md](AGENTS.md) for the vocabulary and the rules that apply to every change.

## Design in brief

* Assets declare; the engine acts. An asset keeps its identity while its implementation changes.
* Commitments are first-class. The registry records who depends on each asset and what was promised, separately from the code that fulfills it.
* Reuse is decided from definitions, not from SQL or samples. Similar column names never prove the same meaning.
* Verification does not trust the author. Checks are generated from the semantic model and never read a candidate's SQL.
* Decisions are data. Every clarification, choice, proposal, verification, and deployment is an event with an actor, an authority, and the records that caused it.
* Models nominate and draft; they never authorize. A classifier model picks from closed sets with a calibrated probability, a generative model drafts SQL and prose, and a decision table or a validator decides what happens. Every call site is listed in the vision PRD.

## License

Apache-2.0. See [LICENSE](LICENSE).
