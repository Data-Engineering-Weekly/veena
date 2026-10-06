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
| `veena scenario dbt` | Write the bundled e-commerce dbt manifest and query history for shadow mode. |
| `veena asset request SPEC --consumer NAME` | Record a request from a spec file, its resolved spec, and the consumer's commitment. |
| `veena asset request --text WORDS --asset-id ID --consumer NAME` | Resolve a request from the consumer's words into a spec, a question, or a refusal. |
| `veena asset resolve ID` | Resume resolution after a question was answered. |
| `veena asset plan ID` | Print the plan and the SQL the backend would run, without running it. |
| `veena asset activate ID` | Propose, implement, verify, and deploy. Shares an intermediate when policy allows. |
| `veena asset verify ID` | Re-run the generated checks against the published interface. |
| `veena asset show ID` | Print everything the registry knows about one asset, as text or as the article's YAML fragment. |
| `veena registry assets` | List assets with status, spec version, and open commitments. |
| `veena registry log` | Print every event after a revision. |
| `veena proposal list|show|approve|reject` | Inspect and decide change proposals. |
| `veena deployment list|rollback` | Inspect deployments and undo the latest one. |
| `veena question list|show|answer|confirm` | Inspect open questions, record a decision, confirm inferred drafts in a batch. |
| `veena models calls|calibration` | List model calls and report how honest a classifier's probabilities are. |
| `veena import dbt MANIFEST [--history FILE]` | Import a dbt manifest, MetricFlow definitions, and query history as observed state. |
| `veena import infer` | Open one inferred draft spec per observed asset as a question. |
| `veena report duplicates [--propose]` | Find equivalence sets across confirmed specs; record consolidation proposals in shadow. |
| `veena report usage` | Readers per asset with cadence and class, plus refresh cost. |
| `veena ops` | The operator view: unmet commitments, affected consumers, recent deployments, what comes next, then lags, brakes, impact. |
| `veena ops trail COMMITMENT [--narrate]` | Deployment, proposal, verification, specification, decisions for one commitment, each with actor and authority. |
| `veena ops freeze|unfreeze|pin|unpin|brakes|ack` | The brakes and the impact acknowledgement, each recorded as an event. |
| `veena asset check ID` | Verify the live interface; on failure triage the cause and repair under policy. |
| `veena proposal replay ID --at INSTANT` | Rebuild a proposal's implementations at a past position and verify them there. |
| `veena deployment show ID` | A deployment with the artifacts that reproduce it, and a warning when one has expired. |
| `veena commitment list|release` | Commitments, and the release that starts retirement. |
| `veena retire plan|advance` | The next retirement step per asset and why it waits; take the steps. |
| `veena export lineage` | OpenLineage run events and a W3C PROV document. |
| `veena scenario drift KIND` | Change the sources after an asset was built, so the next check fails. |

Default locations are under `.veena/` in the current directory and ignored by git:

| Path | Holds |
|---|---|
| `scenario.duckdb` | The synthetic sources and every asset the engine builds. |
| `registry.sqlite` | The event log. |
| `calibration/` | Labeled model predictions for `models calibration`. |
| `semantic.yaml` | The semantic model `import dbt` derives from MetricFlow. |
| `dbt/` | The bundled manifest and query history that `scenario dbt` writes. |
| `lineage/` | The two files `export lineage` writes. |

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
  purchases: 9667
  remapped_accounts: 17
  refunded_purchases: 471
  inactive_customers: 30
```

Purchases arrive as baskets, around six items each, so a daily asset compresses them. Two numbers matter. The 17 remapped accounts each have two identity rows, so a join on account id alone double counts their purchases. The 30 inactive customers have no purchases, so a correct asset must still show them with zero spend.

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
    1  2026-10-06T00:06:20+00:00  asset_requested          actor=app:sales authority=request:app:sales
    2  2026-10-06T00:06:20+00:00  spec_resolved            actor=engine authority=request:app:sales
    3  2026-10-06T00:06:20+00:00  commitment_registered    actor=engine authority=request:app:sales
    ...
```

Both assets are `specified` and neither has an implementation. The planner will move them to `active`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | A file could not be read, failed validation, the registry rejected an event, or policy refused the action. The message starts with `error:`. |
| 2 | `spec compare` ran and the provider cannot serve the consumer. |
| 3 | The engine asked a question instead of acting: `asset request --text` or `asset resolve` opened one, `asset activate` escalated, or `asset verify` found a failing check. |
| 4 | `asset request --text` refused the request, for example because the screen flagged it. |

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
plan customer_spend_30d@1 (semantic e9884a18d25f)
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
verification of customer_spend_30d:
  ok   unique_grain               [structural] one row per customer_id
  ok   population_preserved       [structural] every customer in customers appears exactly once
  ok   reconcile_spend_usd        [reconciliation] spend_usd equals SUM(purchase_amount_usd@4) attributed by customer_identity_point_in_time@2, per customer_id
       uncertainty: absolute tolerance 0.005 USD
  ok   conserve_spend_usd         [reconciliation] total spend_usd equals SUM(purchase_amount_usd@4) over purchases in the window, so every fact counts exactly once
       uncertainty: facts whose key maps to no entity in the window would be missing from both sides of a per-entity reconciliation; this check catches them
activated customer_spend_30d via customer_spend_30d_d1 (direct) after 1 attempt(s); grade reconciliation; registry at revision 9
```

Four checks ran and nobody wrote them. Two are structural and gate pass or fail. Two are value checks and set the grade. The reconciliation compares every customer's total against a query verification built itself from the identity policy and the approved calculation. The conservation check compares the asset's total against the raw settled total in the window, so a purchase counted twice or dropped shows up even when every per-customer row looks plausible.

Six events were written: the proposal opened, the proposal approved under the routine activation policy, the implementation planned, the implementation refreshed, the verification, and the deployment. The deployment is recorded before the interface is published, so a crash between the two leaves a record to repair from rather than an unexplained view. The asset's interface is now a view named `customer_spend_30d` over the table `customer_spend_30d__v1__i1`. Consumers read the interface. When the engine rewires the asset, the interface moves and the consumer does not.

Read the trail back.

```bash
uv run veena asset show customer_spend_30d
```

```
request by app:sales: "spend over the previous 30 complete UTC days"
asset customer_spend_30d  status=active  impl=customer_spend_30d__v1__i1
spec customer_spend_30d@1: one row per semantic:customer@1, 30 complete days in UTC, identity semantic:customer_identity_point_in_time@2
  population.entity <- message:m1
  window.complete_calendar_days <- message:m1
  measures.spend_usd.calculation <- policy:approved_spending_semantics@4
  identity_policy <- policy:approved_spending_semantics@4
commitment customer_spend_30d:app:sales: app:sales, incorporate within 300s
deployment customer_spend_30d_d1 (active): customer_spend_30d__v1__i1 via customer_spend_30d_p1, verified by customer_spend_30d_p1_v1, assumed revision 3
proposal customer_spend_30d_p1 (direct, activated): implement customer_spend_30d@1 directly from approved sources
  implementation customer_spend_30d__v1__i1 on duckdb; reads purchases@e9884a18d25f, identity_mappings@e9884a18d25f, customers@e9884a18d25f
  verification customer_spend_30d_p1_v1: passed, grade reconciliation, 4/4 checks
```

Every line points to its origin. The deployment names the proposal, the verification, and the registry revision it assumed. The spec names the message or policy each requirement came from.

Re-run the checks at any time against the published interface:

```bash
uv run veena asset verify customer_spend_30d --as-of 2026-10-01T00:00:00+00:00
```

It exits 3 when a check fails.

### What happens when the implementation is wrong

The scenario plants the article's bug: some accounts moved between customers inside the window. A join on account id alone counts those purchases twice. The engine's planner never produces that join, so the CLI cannot show the failure directly. The test suite can. `tests/test_coordinator.py::test_repair_bound_escalates_with_a_question` forces the naive join through an injected planner with the repair bound at 1. The result in the registry is one failed verification whose reconciliation names the affected customers and the total discrepancy, and one open question asking a person how to proceed. The asset stays `specified` and nothing is published.

## Walkthrough, continued: a second request reshapes the graph

Register the support agent's 90-day request the same way, then activate it.

```bash
uv run veena asset request src/veena/scenario/specs/customer_spend_90d.yaml \
  --consumer agent:support --text "same metric over 90 days"
uv run veena asset activate customer_spend_90d --as-of 2026-10-01T00:00:00+00:00
```

The engine prints three verifications, one per affected asset, and ends with:

```
activated customer_spend_90d via customer_spend_daily_d1 (share) after 1 attempt(s); grade reconciliation; registry at revision 27
```

The word `share` is the architectural decision. The engine did not build a second direct aggregation. It compared the two specifications, found they agree on everything but the window, derived a customer-by-day asset with 90 days of history, built it, rewired the 30-day asset to read from it, built the 90-day asset on it, verified all three, and switched all three interfaces in one transaction. Look at what it decided and why.

```bash
uv run veena proposal show customer_spend_daily_p1
```

```
proposal customer_spend_daily_p1 (share) status=activated
  create customer_spend_daily@1 and serve customer_spend_30d@1 and customer_spend_90d@1 from it
  reason: benefit 881 rows >= minimum 1 under policy:reorganization@1
  change: create customer_spend_daily@1 with 90 days of history
  change: rewire customer_spend_30d@1 to read customer_spend_daily
  change: create customer_spend_90d@1 reading customer_spend_daily
  affects: customer_spend_daily, customer_spend_30d, customer_spend_90d; assumed revision 12
  cost: 9550 fact rows processed per refresh now, 8669 proposed, benefit 881
  evidence required: verification of customer_spend_daily
  evidence required: verification of customer_spend_30d
  evidence required: verification of customer_spend_90d
  evidence required: equivalence of customer_spend_30d at matching inputs
  considered: customer_spend_30d@1 -> customer_spend_90d@1: cannot because window differs: provider covers 30 complete days, consumer needs 90
  considered: customer_spend_90d@1 -> customer_spend_30d@1: cannot because window differs: provider covers 90 complete days, consumer needs 30
  ...
```

Three things to read here. The cost is measured, not estimated: the engine built the daily candidate, counted the fact rows each plan processes per refresh, and compared. The policy that allowed it is named. And the alternatives it considered first are recorded, so a reviewer can see that reusing the 30-day asset was checked and refused for a stated reason.

The sales application never changed anything. Its interface still answers:

```bash
uv run veena asset show customer_spend_30d
```

```
asset customer_spend_30d  status=active  impl=customer_spend_30d__v1__i2  reads customer_spend_daily
...
deployment customer_spend_30d_d1 (active): customer_spend_30d__v1__i1 via customer_spend_30d_p1, verified by customer_spend_30d_p1_v1, assumed revision 3
deployment customer_spend_daily_d1 (active): customer_spend_30d__v1__i2 via customer_spend_daily_p1, verified by customer_spend_daily_p1_v2, assumed revision 12
...
  implementation customer_spend_30d__v1__i2 on duckdb; reads customer_spend_daily@customer_spend_daily__v1__i1, customers@e9884a18d25f
  verification customer_spend_daily_p1_v2: passed, grade reconciliation, 5/5 checks
```

The second implementation reads the daily asset at a named implementation, so the version it assumed is on record. Its verification has five checks: the four generated from the spec, plus an equivalence check against the previous implementation at the same inputs, graded `test` because two implementations can agree by sharing a mistake.

### Policy knobs

Sharing is gated by a policy, not by the engine's enthusiasm.

```bash
uv run veena asset activate customer_spend_90d --min-benefit 1000000     # too little benefit: builds direct
uv run veena asset activate customer_spend_90d --max-reorganizations 0   # cap reached: builds direct
uv run veena asset activate customer_spend_90d --require-approval        # waits for a person
```

In the first two cases the engine records the share proposal as rejected with the reason and builds the 90-day asset directly. In the third it opens the proposal and stops. Inspect and decide:

```bash
uv run veena proposal list
uv run veena proposal approve customer_spend_daily_p1 --reason "owner agreed"
uv run veena proposal reject customer_spend_daily_p1 --reason "not this quarter"
```

### Undoing a deployment

```bash
uv run veena deployment list
uv run veena deployment rollback customer_spend_daily_d1 --reason "support saw wrong totals"
```

Rollback repoints every interface the deployment switched to what it replaced, and only the latest deployment can be rolled back. The 30-day interface goes back to its first implementation. The daily and 90-day interfaces are dropped because nothing preceded them. The event records the consumers who may have read the rolled-back data and over what interval, because a rollback fixes future reads and cannot undo past ones.

### When the shared candidate is wrong

`tests/test_coordinator_share.py::test_naive_shared_candidate_is_caught_repaired_and_then_activated` forces the article's bug into the daily candidate on the first attempt. The daily asset's own reconciliation fails, and so does the 30-day asset's, whose failure names `customer_spend_daily` as the cause. Nothing is published. On the second attempt the planner produces the correct join, all three verifications pass, and one deployment switches the three interfaces. The registry keeps both attempts.

### When history is missing

Build a scenario with only 60 days of history and request 90 days. The engine opens a question instead of a proposal:

```
customer_spend_90d@1 needs 90 days of purchases history from 2026-07-03, but the oldest record is 2026-08-02... How should the engine proceed?
  alternatives: accept_shorter_history, load_more_history, abandon_request
```

## Walkthrough, continued: a request in plain words

Until now every request arrived as a spec file. The engine can also take the consumer's words. Resolution uses a classifier to pick each requirement from the closed sets the semantic model defines, so it cannot invent a definition. Numbers come from the text. Anything the text leaves unstated comes from workspace policy and says so.

```bash
uv run veena asset request --consumer app:sales --asset-id customer_spend_30d --threshold 0.6 \
  --text "Return one row per customer with their spending over the previous 30 complete calendar days in UTC. Include customers with no qualifying purchases. Reflect new data within 5 minutes."
```

```
resolved customer_spend_30d@1; registry at revision 7
  population.entity <- message:m1
  measures.calculation <- message:m1
  measures.aggregation <- policy:approved_calculations_are_additive@1
  identity_policy <- message:m1
  window.complete_calendar_days <- message:m1
  window.timezone <- message:m1
  population.include_inactive <- message:m1
  service.incorporate_within <- message:m1
  service.access_policy <- policy:workspace_defaults@1
  service.budget_policy <- policy:workspace_defaults@1
```

Every requirement names where it came from. The two policy lines are the terms the request did not state. The default classifier is the offline keyword baseline, which is why `--threshold 0.6` appears: its probabilities are word-overlap scores, not calibrated, and the calibration report below says so. With `--classifier claude` or `--classifier jev` the default threshold of 0.8 applies.

### When the request is ambiguous

The semantic model has two approved household definitions. A request that says "household" without choosing one gets a question, not a guess.

```bash
uv run veena asset request --consumer agent:crm --asset-id household_spend_90d --threshold 0.6 \
  --text "household spending over the previous 90 complete calendar days"
```

```
question household_spend_90d_q1 on population.entity:
  agent:crm asked for household_spend_90d: "household spending over the previous 90 complete calendar days". The engine cannot settle population.entity. Choose one of: household_by_billing, household_by_residence, customer.
  - household_by_billing: household_by_billing (p=0.50): Groups customers whose billing accounts are linked.; needs households_by_billing; 129 groups over 200 rows; could reuse nothing active
  - household_by_residence: household_by_residence (p=0.50): Groups customers who share a primary residence address.; needs households_by_residence; 99 groups over 200 rows; could reuse nothing active
  - customer: customer (p=0.00): One row per registered customer, the individual account holder.; needs customers; 200 groups over 200 rows; could reuse nothing active
answer with: veena question answer household_spend_90d_q1 --select <alternative>
```

Each alternative carries evidence: the probability the classifier gave it, its definition, the source it needs, how it groups the 200 customers in the scenario, and which active assets it could reuse. The prose is a template here. With `--drafter claude` a model phrases it, and the stored question keeps the structured alternatives beside the prose either way.

The owner answers, and the answer has a scope.

```bash
uv run veena question answer household_spend_90d_q1 --select household_by_residence \
  --rationale "support groups by where people live" --actor user:owner
uv run veena asset resolve household_spend_90d --threshold 0.6
```

```
question household_spend_90d_q2 on identity_policy:
  ...
  - define_identity_policy: no identity policy attaches purchases to semantic:household_by_residence@1
  - choose_different_entity: known identity policies: customer_identity_point_in_time@2 -> semantic:customer@1
  - abandon_request: a new policy needs a mapping source with validity columns for household_id
```

The entity is settled by the decision and will not be asked again for this asset. The next gap is real: nothing in the semantic model says how a purchase attaches to a household. The engine records that as a question for the owner instead of inventing a join. Decisions scoped `--scope workspace` apply to every later request, but only when recorded under a role authority such as `role:data_owner`.

### Looking at what the models said

```bash
uv run veena models calls
```

```
    2 classify screen_request   -                        customer_spend_30d     benign p=0.33 [keyword]
    3 classify resolve_refs     population.entity        customer_spend_30d     customer p=0.60 [keyword]
    4 classify resolve_refs     measures.calculation     customer_spend_30d     purchase_amount_usd p=1.00 [keyword]
    5 classify judge_draft      -                        customer_spend_30d     match p=0.50 [keyword]
```

Four calls per resolved request: a screen for injection, one pick per ref slot, and a judge of the assembled draft. Each is an event with the model id, the input hash, the value, and the probability. The keyword baseline abstains on the screen and the judge with a uniform probability, which is why the injection example in `tests/test_resolver.py` uses a scripted classifier: screening only works with a real one.

### The article's fragment

```bash
uv run veena asset show customer_spend_30d --format yaml
```

```yaml
asset_id: customer_spend_30d
spec_version: 1
requirements:
  population.entity:
    ref: semantic:customer@1
    source: message:m1
  measures.calculation:
    ref: semantic:purchase_amount_usd@4
    source: message:m1
  window.complete_calendar_days:
    ref: '30'
    source: message:m1
  service.access_policy:
    ref: policy:workspace_access@1
    source: policy:workspace_defaults@1
  ...
```

After activation the fragment also carries `change_proposal_ref`, `verification_ref`, and `deployment_ref`.

### Is the classifier honest about its confidence?

```bash
uv run veena models calibration resolve_refs
```

```
calibration of resolve_refs (keyword): 10 labeled predictions, accuracy 1.00, expected calibration error 0.21
  stated         count  mean stated  observed    gap
  0.4 to 0.6          3         0.60      1.00  -0.40
  0.6 to 0.8          3         0.69      1.00  -0.31
  0.8 to 1.0          4         1.00      1.00  +0.00
```

The bundled labeled set is small, and on it the keyword baseline is right every time but underconfident below 0.8. A threshold of 0.8 would ask a person on six of ten requests it would have answered correctly. That is the number to beat with `--classifier claude` or `--classifier jev`, and the reason thresholds live in policy rather than code. Labeled predictions accumulate under `.veena/calibration/`.

### Model adapters

| Flag | Needs | What it is |
|---|---|---|
| `--classifier keyword` | nothing | Word overlap between the request and each option's name and description. Deterministic. Abstains on verdicts. |
| `--classifier claude` | `ANTHROPIC_API_KEY`; install with `uv sync --extra claude` | Claude with a structured output schema whose `value` field is the closed set. Model id from `VEENA_CLAUDE_MODEL`, default `claude-fable-5-1`. |
| `--classifier jev` | `JEV_BASE_URL`, `JEV_API_KEY` | Posts to an assumed `POST /v1/decide` contract. Jev's API is not public yet; the adapter is one place to correct. |
| `--drafter claude` | as above | Phrases questions from structured alternatives and evidence. |

No test calls a model. Tests use scripted adapters, and `RecordingClassifier` / `ReplayClassifier` let a real model's answers drive a test offline.

## Walkthrough, continued: shadow mode on a dbt project

Everything so far ran on assets the engine built. Most warehouses already have hundreds of models nobody dares refactor. Shadow mode imports them as observed state, infers what each one means as a question, and finds the duplicates from specifications. It activates nothing.

No real project was available, so a generated one stands in: shoply, an e-commerce project with 41 models, 15 raw sources, 5 exposures, MetricFlow definitions, and two weeks of warehouse query history. Three teams own three 30-day customer spend models.

```bash
uv run veena scenario dbt
uv run veena import dbt .veena/dbt/manifest.json --history .veena/dbt/query_history.jsonl
```

```
imported shoply: 41 assets added, 0 updated, 0 unchanged, 0 ephemeral skipped; 14 commitments added, 0 kept
semantic model: 4 entities, 4 calculations, 3 time rules, 4 identity policies -> .veena/semantic.yaml
semantic gaps recorded as questions: 2
history: 5419 queries, 5419 matched assets, 22 usage rows, 26 refresh costs, 1 unmanaged reads, 8 commitments proposed
```

Four things happened. Every model is now an asset with status `observed`, its SQL and dependencies on record and no spec. Every exposure edge became a commitment from `exposure:<name>`. The MetricFlow semantic models became a semantic model written to `.veena/semantic.yaml`: primary entities, additive measures as calculations, aggregation time dimensions as time rules, and foreign entities as direct identity policies. The two ratio metrics could not be represented and were recorded as questions on their models. The query history became usage per reader, rebuild cost per model, one unmanaged read of an intermediate table, and eight proposed commitments for readers who hold none.

Run the import again and nothing changes. A model whose checksum changed is updated. Ephemeral models have no table, so they are skipped and counted. Versioned models import as `name_vN`; seeds and snapshots import like models.

### Inferring what each model means

```bash
uv run veena import infer
```

```
cust_spend_last_30d          weakest slot p=0.62
customer_spend_30d           weakest slot p=0.58
customer_spend_90d           weakest slot p=0.58
customer_spend_30d_v2        weakest slot p=0.56
campaign_attribution         weakest slot p=0.00 open: identity_policy, population.entity, window.complete_calendar_days
...
stg_orders                   weakest slot p=0.00 open: identity_policy, shape, window.complete_calendar_days
41 drafts opened as questions
```

One question per asset, slot `inferred_spec`, with a draft: entity and calculation from the classifier with probabilities, window and time bucket from the name and SQL, inactive members from the join shape, identity policy from the semantic model. A slot the engine cannot fill is `open` with the reason. Staging views have `shape` open because the spec IR describes aggregates. The household model has its entity open because its output has no `customer_id`: the grain is a postal code, and the engine says so instead of guessing.

### Confirming in a batch

```bash
uv run veena question confirm --above 0.5
```

```
confirmed cust_spend_last_30d
confirmed customer_spend_30d
confirmed customer_spend_30d_v2
confirmed customer_spend_90d
4 specs resolved; registry at revision 245
```

Each confirmation is a `DecisionRecorded` scoped to the asset, then a `SpecResolved` whose provenance points at the decision. Drafts with an open slot are never confirmed by a threshold. The bar is 0.5 here because the keyword baseline splits four USD measures almost evenly; with `--classifier claude` the default 0.9 applies. Answer any question by hand with `veena question answer <id> --select confirm_draft` and the next `confirm` resolves it.

### The duplicates nobody dared to clean up

```bash
uv run veena report duplicates --propose
```

```
4 assets with confirmed specifications
equivalence set 1: 3 assets, 0.0282 credits rebuilding them over the window
  cust_spend_last_30d          cust_spend_last_30d@1        owner marketing-analytics    14 rebuilds, 0.0087 credits; exposure:marketing_dashboard
  customer_spend_30d           customer_spend_30d@1         owner sales-engineering      14 rebuilds, 0.0096 credits; exposure:sales_app
  customer_spend_30d_v2        customer_spend_30d_v2@1      owner support-tools          14 rebuilds, 0.0099 credits; exposure:support_agent
  evidence: same population semantic:customer@1, identity policy semantic:fct_orders_customer_direct@1, measures sum(semantic:net_order_usd@1), window 30 days in UTC
  evidence: decided by specification equality, not by column names or sample rows
proposed cust_spend_last_30d_consolidate_p1 (shadow: recorded, not activated)
```

Three owners, three tables, one meaning. The evidence is the specification, not the column names. The cost is measured from the rebuilds in query history. The proposal is recorded with kind `consolidate` and stays open. In shadow autonomy `veena asset activate` and `veena proposal approve` refuse with a policy error after recording the proposal.

### Who reads what

```bash
uv run veena report usage --asset-id customer_spend_30d
```

```
customer_spend_30d           user:svc_sales_app       managed    p=0.90  3385 queries  241.84/day 24h proposed
refresh cost over the window:
  customer_spend_30d            14 rebuilds     4.19 GB    0.0096 credits
```

The sales application reads the table around the clock. The reader class comes from cadence rules, the offline baseline for this call site. The engine proposes a commitment for the observed user because dbt declared the exposure as `exposure:sales_app` and nothing links that name to the warehouse principal `svc_sales_app`. That mapping is an owner's to provide.

## Walkthrough, continued: operating the graph

Everything so far built assets. This part is about running them when something goes wrong, and shrinking the graph when a consumer leaves. Start from the state at the end of the second request: `customer_spend_30d` and `customer_spend_90d` both read `customer_spend_daily`.

```bash
uv run veena ops --as-of 2026-10-01T00:00:00+00:00
```

```
operator view as of 2026-10-01T00:00:00+00:00

unmet commitments
  none

affected consumers
  none

recent deployments touching them
  customer_spend_daily_d1        2026-10-01T00:00:00+00:00 active      customer_spend_daily -> customer_spend_daily__v1__i1, customer_spend_30d -> customer_spend_30d__v1__i2, customer_spend_90d -> customer_spend_90d__v1__i1 by engine (policy:routine_activation@1)
  customer_spend_30d_d1          2026-10-01T00:00:00+00:00 active      customer_spend_30d -> customer_spend_30d__v1__i1 by engine (policy:routine_activation@1)

what the engine proposes next
  nothing open

lag per asset and source (source lag | incorporation lag)
  customer_spend_30d         purchases                1.3h |         0s
  customer_spend_90d         purchases                1.3h |         0s
  customer_spend_daily       purchases                1.3h |         0s

active brakes
  none
```

The four questions come first and in that order, because that is the order an on-call engineer asks them. Everything is computed from events. The lag table keeps two numbers apart: the source is 1.3 hours behind the instant, which is the source's doing, and the assets have incorporated everything the source holds, which is the engine's doing. When a scenario database is present, `veena ops` records the source positions it observed before it computes the view, so the freshness condition rests on an event too.

A commitment is unmet under four conditions, each computed from events: the asset incorporated less than the source holds by more than the commitment's bound; its last verification failed; its interface columns changed while the commitment marked them stable; its last refresh processed more fact rows than the commitment's ceiling.

### A source changes under a live asset

Move one account to another customer, the way identity teams do, and check the 90-day asset.

```bash
uv run veena scenario drift mapping_change
uv run veena asset check customer_spend_90d --as-of 2026-10-01T00:00:00+00:00 --min-probability 0.9
```

```
account a00088 moved from c00099 to c00000 in identity_mappings
  ok   unique_grain               [structural] one row per customer_id
  ok   population_preserved       [structural] every customer in customers appears exactly once
  FAIL reconcile_spend_usd        [reconciliation] spend_usd equals SUM(purchase_amount_usd@4) attributed by customer_identity_point_in_time@2, per customer_id
       2 keys differ, total discrepancy 10237.34; c00000: got 686.75, expected 5805.42, c00099: got 5118.67, expected 0.00
  ok   conserve_spend_usd         [reconciliation] total spend_usd equals SUM(purchase_amount_usd@4) over purchases in the window, so every fact counts exactly once
       uncertainty: facts whose key maps to no entity in the window would be missing from both sides of a per-entity reconciliation; this check catches them
customer_spend_90d_live1 failed; triage identity_mapping_change p=0.75, strategy escalate [rules]
opened question customer_spend_90d_q1; the commitment is degraded
```

Three things happened. The live check ran the same generated checks as activation did, against the interface consumers read, and recorded a `LiveVerificationRecorded`. The failure was triaged: a classifier saw the structured evidence, never the SQL, and said `identity_mapping_change` at 0.75. The default classifier is the rule baseline, and the calibration report below says what its probabilities are worth. Then the repair policy decided. With `--min-probability 0.9` the probability is below the bar, so the policy escalates: one question, no repair attempt, and the commitment is marked degraded. The view now says so.

```bash
uv run veena ops --as-of 2026-10-01T00:00:00+00:00
```

```
unmet commitments
  customer_spend_90d:agent:support   verification [degraded] customer_spend_90d_live1 failed: reconcile_spend_usd: 2 keys differ, total discrepancy 10237.34; c00000: got 686.75, expected 5805.42, c00099: got 5118.67, expected 0.00

affected consumers
  agent:support            customer_spend_90d:agent:support

recent deployments touching them
  customer_spend_daily_d1        2026-10-01T00:00:00+00:00 active      customer_spend_daily -> customer_spend_daily__v1__i1, customer_spend_30d -> customer_spend_30d__v1__i2, customer_spend_90d -> customer_spend_90d__v1__i1 by engine (policy:routine_activation@1)

what the engine proposes next
  question customer_spend_90d_q1 (general): reconcile_spend_usd failed on customer_spend_90d (customer_spend_90d__v1__i1): 2 keys differ, total discrepancy 10237.34; ... triage identity_mapping_change p=0.75, strategy escalate. No repair was attempted: policy:repair@1 escalates identity_mapping_change at p=0.75. Alternatives tried: none. How should the engine proceed?

degraded assets
  customer_spend_90d: consumers read customer_spend_90d__v1__i1; last verified customer_spend_90d__v1__i1 at source position purchases 2026-09-30T22:44:24+00:00
...
```

Consumers keep reading the last verified implementation, and the view states the source position it was verified at. Follow the trail from the unmet commitment.

```bash
uv run veena ops trail customer_spend_90d:agent:support
```

```
deployment
  customer_spend_daily_d1 (active) at 2026-10-01T00:00:00+00:00 by engine (policy:routine_activation@1); assumed revision 12
  customer_spend_daily -> customer_spend_daily__v1__i1 (replaced nothing)
  customer_spend_30d -> customer_spend_30d__v1__i2 (replaced customer_spend_30d__v1__i1)
  customer_spend_90d -> customer_spend_90d__v1__i1 (replaced nothing)
proposal
  customer_spend_daily_p1 (share, activated) by engine (policy:routine_activation@1): create customer_spend_daily@1 and serve customer_spend_30d@1 and customer_spend_90d@1 from it
  change: create customer_spend_daily@1 with 90 days of history
  change: rewire customer_spend_30d@1 to read customer_spend_daily
  change: create customer_spend_90d@1 reading customer_spend_daily
  decided: benefit 881 rows >= minimum 1 under policy:reorganization@1
verification
  customer_spend_daily_p1_v3 (passed, grade reconciliation) by engine (policy:routine_activation@1): 4/4 checks
  customer_spend_90d_live1 live (failed) at 2026-10-01T00:00:00+00:00 by engine (policy:routine_activation@1)
  FAIL reconcile_spend_usd: 2 keys differ, total discrepancy 10237.34; c00000: got 686.75, expected 5805.42, c00099: got 5118.67, expected 0.00
specification
  customer_spend_90d@1 resolved by engine (request:agent:support): one row per semantic:customer@1, identity semantic:customer_identity_point_in_time@2
  population.entity <- message:m2
  window.complete_calendar_days <- message:m2
  measures.spend_usd.calculation <- policy:approved_spending_semantics@4
  identity_policy <- policy:approved_spending_semantics@4
decisions
  none recorded
```

Five sections, in order, each line naming who acted and under what authority. `--narrate` adds prose written by the drafter from exactly this structured trail and records it as a `DraftProduced` event; with `--drafter none` the command says no drafter is configured and the trail above is complete.

### Pin, repair, release

The on-call engineer pins the 30-day asset to the implementation that was good before the rewiring, lets the engine repair, then releases the pin.

```bash
uv run veena ops pin customer_spend_30d customer_spend_30d__v1__i1 --reason "known good while the daily asset is repaired"
uv run veena asset check customer_spend_90d --as-of 2026-10-01T00:00:00+00:00
```

```
pinned customer_spend_30d to customer_spend_30d__v1__i1
  ...
customer_spend_90d_live2 failed; triage identity_mapping_change p=0.75, strategy re_plan_join [rules]
verification of customer_spend_daily:
  ok   ...
verification of customer_spend_30d:
  ok   ...
verification of customer_spend_90d:
  ok   ...
activated customer_spend_90d via customer_spend_daily_d2 (repair) after 1 attempt(s); grade reconciliation; registry at revision 48
```

With the default bar of 0.7 the same triage selects `re_plan_join`. The 90-day asset reads the daily asset, so the repair rebuilds the daily asset first and every dependent after it, verifies all three, and deploys them in one deployment. The 30-day asset's interface did not move: it is pinned, and the deployment record says where it should go when the pin is released.

```bash
uv run veena ops brakes
uv run veena ops unpin customer_spend_30d --reason "repair verified"
```

```
pin    customer_spend_30d              0.0 days  by user:oncall (role:operator)  to customer_spend_30d__v1__i1: known good while the daily asset is repaired
unpinned customer_spend_30d; interface at customer_spend_30d__v1__i3
```

`veena ops brakes` is the weekly report: every freeze and pin with its age, so a brake does not quietly become permanent.

### Freeze

```bash
uv run veena ops freeze customer_spend_daily --subgraph --reason "quarter close"
uv run veena ops --as-of 2026-10-01T00:00:00+00:00
uv run veena ops unfreeze customer_spend_daily
```

```
frozen customer_spend_daily and its dependents
...
active brakes
  freeze customer_spend_daily       age       0s by user:oncall (role:operator) asset and dependents: quarter close
unfrozen customer_spend_daily
```

A freeze stops reorganization of an asset and, with `--subgraph`, of everything that reads it. A later overlapping request builds directly, and the share proposal it would have made is recorded and rejected with a reason that names the freeze, who applied it, and when. A repair proposal touching a frozen asset is recorded and refused the same way. `veena ops unfreeze` ends it.

### Replay a proposal at a past position

```bash
uv run veena proposal replay customer_spend_daily_p1 --at 2026-09-11T00:00:00+00:00
```

```
replay of customer_spend_daily__v1__i1__replay at 2026-09-11T00:00:00+00:00:
  ok   ...
replay of customer_spend_30d__v1__i2__replay at 2026-09-11T00:00:00+00:00:
  ok   ...
replay of customer_spend_90d__v1__i1__replay at 2026-09-11T00:00:00+00:00:
  ok   ...
replay passed; recorded as replay verifications that no deployment can cite
```

Each implementation is rebuilt with the past instant fixing its window, verified there, and dropped. The results are `VerificationRecorded` events marked `replay`; the registry refuses a deployment that cites one.

### What reproduces a deployment

```bash
uv run veena deployment show customer_spend_daily_d1 --as-of 2026-12-01T00:00:00+00:00
```

```
deployment customer_spend_daily_d1 (active) at 2026-10-01T00:00:00+00:00 by engine (policy:routine_activation@1); proposal customer_spend_daily_p1; assumed revision 12
  customer_spend_daily -> customer_spend_daily__v1__i1 (replaced nothing)
  customer_spend_30d -> customer_spend_30d__v1__i2 (replaced customer_spend_30d__v1__i1)
  customer_spend_90d -> customer_spend_90d__v1__i1 (replaced nothing)
  backend duckdb v1.5.6
  source position purchases @ 2026-09-30T22:44:24+00:00
  rendered SQL for customer_spend_daily: implementation customer_spend_daily__v1__i1
  rendered SQL for customer_spend_30d: implementation customer_spend_30d__v1__i2
  rendered SQL for customer_spend_90d: implementation customer_spend_90d__v1__i1
  WARNING retention source:purchases: expired 2026-10-31T00:00:00+00:00; the deployment can no longer be reproduced from the warehouse
  retention sql: kept forever
```

### Rollback and the readers it cannot undo

```bash
uv run veena deployment rollback customer_spend_daily_d2 --reason "support saw wrong totals" --as-of 2026-10-01T00:00:00+00:00
uv run veena ops --as-of 2026-10-01T00:00:00+00:00
uv run veena ops ack customer_spend_daily_d2 --reason "support re-ran its report"
```

```
assess impact
  customer_spend_daily_d2: agent:support, app:sales may have read rolled-back data between 2026-10-01T00:00:00+00:00 and 2026-10-01T00:00:00+00:00 (support saw wrong totals); acknowledge with `veena ops ack customer_spend_daily_d2`
```

A rollback fixes future reads. The view lists who may have read the rolled-back data and over what interval until an operator records that the impact was assessed.

### Retirement

Months later the sales application drops its commitment.

```bash
uv run veena commitment release customer_spend_30d:app:sales --reason "sales app retired"
uv run veena retire plan --as-of 2026-10-01T00:00:00+00:00
```

```
customer_spend_30d           next: stop_refresh               -
customer_spend_90d           waits before retire              1 open commitment(s): agent:support
customer_spend_daily         waits before retire              read by customer_spend_30d, customer_spend_90d
```

Retirement weighs four things: explicit commitments, observed reads inside the quiet period, the retention policy, and reconstruction cost. A query count alone never retires anything: an asset with zero queries and an open commitment is never eligible. Each `advance` takes one step per eligible asset.

```bash
uv run veena retire advance --as-of 2026-10-01T00:00:00+00:00
uv run veena retire advance --as-of 2026-10-01T00:00:00+00:00
uv run veena retire plan --as-of 2026-10-01T00:00:00+00:00
```

```
customer_spend_30d           stop_refresh           customer_spend_30d__v1__i3 keeps its rows and is no longer refreshed
customer_spend_30d           replace_with_view      customer_spend_30d__v1__i3 is now a view over customer_spend_daily
customer_spend_30d           waits before drop_materialization policy:retention@1 keeps the materialization until 2026-11-05T...
```

The first step is reversible: a new commitment on the asset resumes refreshing. The second replaces the table with a view over the daily asset, so latency rises but nothing is lost. The third waits for the retention policy, 30 days by default, and longer for an asset whose last refresh processed many fact rows.

```bash
uv run veena retire advance --as-of 2026-11-15T00:00:00+00:00 --until-stable
uv run veena asset show customer_spend_30d
```

```
customer_spend_30d           drop_materialization   3 implementation(s) and the interface are gone; the asset is dormant
customer_spend_30d           retire                 retired; specification and history stay readable
request by app:sales: "spend over the previous 30 complete UTC days"
asset customer_spend_30d  status=retired  impl=-
spec customer_spend_30d@1: one row per semantic:customer@1, 30 complete days in UTC, identity semantic:customer_identity_point_in_time@2
  ...
deployment customer_spend_30d_d1 (active): customer_spend_30d__v1__i1 via customer_spend_30d_p1, verified by customer_spend_30d_p1_v1, assumed revision 3
...
```

Only physical work was removed. The daily asset stays active because the support agent still reads through it. Release that commitment too and the outputs retire before the intermediate, which goes last:

```bash
uv run veena commitment release customer_spend_90d:agent:support
uv run veena retire advance --as-of 2026-12-20T00:00:00+00:00 --until-stable
```

```
customer_spend_90d           stop_refresh           customer_spend_90d__v1__i1 keeps its rows and is no longer refreshed
customer_spend_90d           replace_with_view      customer_spend_90d__v1__i1 is now a view over customer_spend_daily
customer_spend_90d           drop_materialization   2 implementation(s) and the interface are gone; the asset is dormant
customer_spend_daily         stop_refresh           customer_spend_daily__v1__i1 keeps its rows and is no longer refreshed
customer_spend_90d           retire                 retired; specification and history stay readable
customer_spend_daily         drop_materialization   2 implementation(s) and the interface are gone; the asset is dormant
customer_spend_daily         retire                 retired; specification and history stay readable
```

### Lineage

```bash
uv run veena export lineage
```

```
wrote .veena/lineage/openlineage.json and .veena/lineage/prov.json
both documents pass the structural checks
```

The OpenLineage file holds one run event per asset per deployment, with the implementation, its inputs, and the output schema. The PROV file holds assets, specifications, implementations, verifications, and decisions as entities, proposals, deployments, and questions as activities, and actors as agents, with the relations between them. The two share asset and implementation ids. The structural checks cover the rules the export relies on; the published JSON schemas were not fetched.

### Repair policy knobs

| Knob | Default | What it does |
|---|---|---|
| `--classifier` on `asset check` | `rules` | Who triages: `rules` (offline baseline), `keyword`, `claude`, `jev`. |
| `--min-probability` | 0.7 | A triage below this escalates to a person whatever the class. `unknown` always escalates. |
| `--repair-bound` | 2 | Attempts before the engine asks, with the failing check, the magnitude, the triage, and what it tried. |
| `--keep-days` on `retire` | 30 | Days a table is kept after its last commitment ends before it may be dropped. |

The cause classes are `source_lag`, `identity_mapping_change`, `schema_drift`, `late_corrections`, `calculation_drift`, and `unknown`. The default table maps the first to `wait_for_source`, the second to `re_plan_join`, the fourth to `rebuild_with_lookback`, and the rest to `escalate`. Check what the rule baseline's probabilities are worth:

```bash
uv run veena models calibration triage_failure --classifier rules
```

```
calibration of triage_failure (rules): 6 labeled predictions, accuracy 0.83, expected calibration error 0.12
  stated         count  mean stated  observed    gap
  0.4 to 0.6          1         0.55      1.00  -0.45
  0.6 to 0.8          4         0.74      0.75  -0.01
  0.8 to 1.0          1         0.80      1.00  -0.20
```

Six labeled failures is a start, not a measurement. Real failures labeled by on-call engineers would settle whether a stated 0.75 is right three times in four.

### Drift kinds

| Kind | What changes | What happens next |
|---|---|---|
| `mapping_change` | One busy account moves to another customer. | The live check fails per customer; a rebuild repairs it. |
| `duplicate_mapping` | The account gets a second, overlapping mapping. | Facts count twice, a rebuild counts them twice too, and the engine asks a person. |
| `late_purchases` | Twenty settled purchases arrive inside the window. | The live check fails; a rebuild repairs it. |

### Snowflake

The same plans render for Snowflake:

```bash
uv run veena asset plan customer_spend_30d   # DuckDB SQL
```

Rendering to Snowflake happens through `SnowflakeBackend` in code, with a connection factory you supply. It uses `TIMESTAMP_TZ` casts and `CONVERT_TIMEZONE`, creates transient tables, and stages interface switches because Snowflake DDL autocommits. Rendering is tested offline against sqlglot's Snowflake dialect. Execution was not run against a live account in this build.
