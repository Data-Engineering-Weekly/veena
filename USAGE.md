# Using veena

This guide covers the CLI and the two file formats it reads: specifications and the semantic model. Every command runs through `uv run veena`.

## Commands at a glance

| Command | What it does |
|---|---|
| `veena version` | Print the installed version. |
| `veena spec validate FILE` | Load a spec and print it back in canonical YAML. |
| `veena spec compare PROVIDER CONSUMER` | Report whether the provider spec can serve the consumer spec. |
| `veena spec examples` | Print the paths of the bundled example specs and semantic model. |
| `veena scenario build` | Generate the synthetic sources into a DuckDB file. |
| `veena scenario semantic` | Validate the bundled semantic model and list its definitions. |
| `veena asset request SPEC --consumer NAME` | Record a request, its resolved spec, and the consumer's commitment. |
| `veena registry assets` | List assets with status, spec version, and open commitments. |
| `veena registry log` | Print every event after a revision. |

Default locations are under `.veena/` in the current directory: `scenario.duckdb` for the sources and `registry.sqlite` for the event log. Both are ignored by git.

## Walkthrough: the customer-spend story

Build the synthetic workspace. The defaults give 200 customers and 120 days of history ending on 2026-09-30.

```bash
uv run veena scenario build
```

```
built .veena/scenario.duckdb
  customers: 200
  accounts: 170
  identity_mappings: 187
  purchases: 1857
  remapped_accounts: 17
  refunded_purchases: 104
  inactive_customers: 30
```

Two numbers matter. The 17 remapped accounts each have two identity rows, so a join on account id alone double counts their purchases. The 30 inactive customers have no purchases, so a correct asset must still show them with zero spend.

Register the first request. The spec file is the resolved form of "one row per customer with spending over the previous 30 complete UTC days".

```bash
uv run veena asset request src/veena/scenario/specs/customer_spend_30d.yaml \
  --consumer app:sales \
  --text "spend over the previous 30 complete UTC days"
```

```
registered customer_spend_30d@1 for app:sales; registry at revision 3
```

Three events were written: the request, the resolved spec, and the commitment. Register the second request the same way with `customer_spend_90d.yaml` and `--consumer agent:support`.

Now ask whether the first asset can serve the second.

```bash
uv run veena spec compare src/veena/scenario/specs/customer_spend_30d.yaml \
  src/veena/scenario/specs/customer_spend_90d.yaml
```

```
customer_spend_30d@1 -> customer_spend_90d@1: cannot
  cannot: window differs: provider covers 30 complete days, consumer needs 90
```

The command exits with code 2 when the provider cannot serve. Compare the proposed daily asset instead.

```bash
uv run veena spec compare src/veena/scenario/specs/customer_spend_daily.yaml \
  src/veena/scenario/specs/customer_spend_90d.yaml
```

```
customer_spend_daily@1 -> customer_spend_90d@1: serves_with
  needs population_join
  needs window
```

The daily asset can serve the 90-day window if the planner sums its daily rows over the window and joins the customer registry back in, because the daily asset omits customers with no activity. The same result holds for the 30-day spec. That shared intermediate is the architectural change the engine will own once the planner exists.

Read the registry back.

```bash
uv run veena registry assets
uv run veena registry log
```

```
revision 6
customer_spend_30d           specified  spec=customer_spend_30d@1     commitments=1 impl=-
customer_spend_90d           specified  spec=customer_spend_90d@1     commitments=1 impl=-
    1  2026-10-05T15:22:39+00:00  asset_requested          actor=app:sales authority=request:app:sales
    2  2026-10-05T15:22:39+00:00  spec_resolved            actor=engine authority=request:app:sales
    3  2026-10-05T15:22:39+00:00  commitment_registered    actor=engine authority=request:app:sales
    ...
```

Both assets are `specified` and neither has an implementation. The planner will move them to `active`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | A file could not be read, failed validation, or the registry rejected an event. The message starts with `error:`. |
| 2 | `spec compare` ran and the provider cannot serve the consumer. |

## The specification file

A spec says what an asset means. It says nothing about tables, joins, or schedules. Here is the bundled 30-day spec with each field explained.

```yaml
asset_id: customer_spend_30d        # stable identity; lowercase, digits, underscores
spec_version: 1                     # bumps when the meaning changes

population:
  entity: semantic:customer@1       # which entity instances appear as rows
  include_inactive: true            # keep members with no activity, with zero measures

grain:
  keys: [semantic:customer@1]       # one row per combination of these keys
  time_bucket: none                 # none, day, or month

measures:
  - name: spend_usd
    calculation: semantic:purchase_amount_usd@4   # an approved calculation
    aggregation: sum                              # sum, count, min, max, avg, count_distinct

window:
  complete_calendar_days: 30        # trailing N complete days before today
  timezone: UTC

identity_policy: semantic:customer_identity_point_in_time@2   # how facts attach to entities

service:
  incorporate_within: PT5M          # ISO 8601 duration, or seconds as an integer
  access_policy: policy:workspace_access@1
  budget_policy: policy:workspace_budget@1

provenance:                         # where each requirement came from
  population.entity: message:m1
  window.complete_calendar_days: message:m1
  measures.spend_usd.calculation: policy:approved_spending_semantics@4
  identity_policy: policy:approved_spending_semantics@4
```

Rules the loader enforces:

* The population entity must be one of the grain keys.
* Measure names are unique within a spec.
* An asset with `time_bucket: none` must set `window.complete_calendar_days`.
* An asset with a time bucket must set `window.history_days`, which bounds how far back it retains buckets.
* Every `semantic:` ref carries a version. Provenance refs are `message:`, `decision:`, `policy:`, or `conversation:`.

A time-bucketed asset looks like this instead:

```yaml
grain:
  keys: [semantic:customer@1]
  time_bucket: day
window:
  history_days: 120
  timezone: UTC
```

## How `spec compare` decides

The check compares specs only. It collects every blocking reason before it answers, so one run tells you everything in the way.

A provider cannot serve a consumer when any of these differ: population entity, identity policy, access policy, timezone. It also cannot serve when it lacks a grain key or a measure the consumer needs, when it retains less history than the consumer's window, when it filters out rows the consumer needs, or when the consumer's measure uses `avg` or `count_distinct` and the provider is at a finer grain.

When it can serve, the result names the operations the planner must add:

| Operation | When |
|---|---|
| `rollup` | The provider has more grain keys than the consumer. |
| `window` | The provider is daily and the consumer wants a trailing window of complete days. |
| `population_join` | The consumer wants inactive members and the provider omits them. |
| `filter` | The consumer has extra filters, or wants inactive members dropped. |

`equal` means the two specs have the same meaning. `serves` means the provider can be read directly, for example because it has extra measures the consumer ignores.

## The semantic model file

The semantic model is the meaning people own. The engine reads it and never edits it. See `src/veena/scenario/semantic.yaml` for the full bundled example.

```yaml
sources:                            # physical tables the workspace trusts
  - name: purchases
    table: purchases
    primary_key: [purchase_id]
    columns: [purchase_id, account_id, purchased_at, amount_usd, status]

entities:                           # things rows can be about
  - name: customer
    version: 1
    source: customers
    key: customer_id

calculations:                       # approved expressions with units
  - name: purchase_amount_usd
    version: 4
    source: purchases
    expression: "CASE WHEN status = 'settled' THEN amount_usd ELSE 0 END"
    unit: USD

time_rules:                         # which timestamp attributes a row to a date
  - name: purchase_date
    version: 1
    source: purchases
    timestamp_column: purchased_at
    timezone: UTC

identity_policies:                  # how a fact attaches to an entity instance
  - name: customer_identity_point_in_time
    version: 2
    entity: semantic:customer@1
    fact_source: purchases
    fact_key: account_id
    fact_timestamp: purchased_at
    mapping_source: identity_mappings
    mapping_fact_key: account_id
    mapping_entity_key: customer_id
    valid_from: valid_from
    valid_to: valid_to
```

The loader checks that every named source exists and every named column is in its source. An identity policy with validity columns is what makes point-in-time attribution possible. The 90-day story's bug is a join that ignores those columns.

## Scenario options

```bash
uv run veena scenario build --path ./mine.duckdb --seed 11 --customers 500 \
  --history-days 200 --last-complete-day 2026-09-30
```

The same seed and options always produce the same tables. The command refuses to overwrite an existing file.

## Walkthrough, continued: from spec to active asset

Once a request is registered, the engine can build it. First look at what it would do.

```bash
uv run veena asset plan customer_spend_30d --as-of 2026-10-01T00:00:00+00:00
```

```
plan customer_spend_30d@1 (semantic 54430b48dacb)
  1. scan purchases (purchases)
  2. attribute time by semantic:purchase_date@1: purchased_at as event_at
  3. window trailing 30 complete days in UTC
  4. join identity_mappings on account_id at fact time -> customer_id
  5. aggregate by customer_id: sum(semantic:purchase_amount_usd@4) as spend_usd
  6. left join customers so every customer_id appears
  -> customer_id, spend_usd
```

The SQL follows, one CTE per step. Step 4 is the point-in-time join: a purchase attaches to the customer who owned the account when the purchase happened. Nobody wrote that join. The planner derived it from the identity policy. The twelve-character code after `semantic` is the fingerprint of the semantic model the plan assumed; it appears again on every dependency.

Now run the loop: propose, implement, verify, deploy.

```bash
uv run veena asset activate customer_spend_30d --as-of 2026-10-01T00:00:00+00:00
```

```
  ok   unique_grain               [structural] one row per customer_id
  ok   population_preserved       [structural] every customer in customers appears exactly once
  ok   reconcile_spend_usd        [reconciliation] spend_usd equals SUM(purchase_amount_usd@4) attributed by customer_identity_point_in_time@2, per customer_id
       uncertainty: absolute tolerance 0.005 USD
  ok   conserve_spend_usd         [reconciliation] total spend_usd equals SUM(purchase_amount_usd@4) over purchases in the window, so every fact counts exactly once
       uncertainty: facts whose key maps to no entity in the window would be missing from both sides of a per-entity reconciliation; this check catches them
activated customer_spend_30d via customer_spend_30d:d1 after 1 attempt(s); grade reconciliation; registry at revision 8
```

Four checks ran and nobody wrote them. Two are structural and gate pass or fail. Two are value checks and set the grade. The reconciliation compares every customer's total against a query verification built itself from the identity policy and the approved calculation. The conservation check compares the asset's total against the raw settled total in the window, so a purchase counted twice or dropped shows up even when every per-customer row looks plausible.

Five events were written: proposal, implementation, refresh, verification, deployment. The asset is now a view named `customer_spend_30d` over the table `customer_spend_30d__v1__i1`. Consumers read the view. When the engine rewires the asset in phase 3, the view moves and the consumer does not.

Read the trail back.

```bash
uv run veena asset show customer_spend_30d
```

```
asset customer_spend_30d  status=active  impl=customer_spend_30d__v1__i1
spec customer_spend_30d@1: one row per semantic:customer@1, 30 complete days in UTC, identity semantic:customer_identity_point_in_time@2
  population.entity <- message:m1
  window.complete_calendar_days <- message:m1
  measures.spend_usd.calculation <- policy:approved_spending_semantics@4
  identity_policy <- policy:approved_spending_semantics@4
commitment customer_spend_30d:app:sales: app:sales, incorporate within 300s
deployment customer_spend_30d:d1 (active): customer_spend_30d__v1__i1 via customer_spend_30d:p1, verified by customer_spend_30d:p1:v1, assumed revision 3
proposal customer_spend_30d:p1: implement customer_spend_30d@1 directly from approved sources
  implementation customer_spend_30d__v1__i1 on duckdb; reads purchases@54430b48dacb, identity_mappings@54430b48dacb, customers@54430b48dacb
  verification customer_spend_30d:p1:v1: passed, grade reconciliation, 4/4 checks
```

Every line points to its origin. The deployment names the proposal, the verification, and the registry revision it assumed. The spec names the message or policy each requirement came from.

Re-run the checks at any time against the published view:

```bash
uv run veena asset verify customer_spend_30d --as-of 2026-10-01T00:00:00+00:00
```

It exits 3 when a check fails.

### What happens when the implementation is wrong

The scenario plants the article's bug: some accounts moved between customers inside the window. A join on account id alone counts those purchases twice. The engine's planner never produces that join, so the CLI cannot show the failure directly. The test suite can. `tests/test_coordinator.py::test_repair_bound_escalates_with_a_question` forces the naive join through an injected planner with the repair bound at 1. The result in the registry is one failed verification whose reconciliation names the affected customers and the total discrepancy, and one open question asking a person how to proceed. The asset stays `specified` and nothing is published.

### Exit codes added in this phase

| Code | Meaning |
|---|---|
| 3 | `asset activate` escalated with a question instead of activating, or `asset verify` found a failing check. |
