# CI Cost Weekly Design

## Status

Accepted for implementation on 2026-09-23.

The initial CI scope contains two accounts:

| Account | Budget plan | Budget basis |
| --- | --- | --- |
| GCP `pingcap-testing-account` | `PingCAP CICD H2 GCP 2026` | `list_cost` |
| Tencent `100050658403` | `PingCAP CICD H2 Tencent 2026` | `net_cost` |

The Tencent H2 budget amount, `$84,000`, is confirmed as a USD net-cost budget.

## Problem and first principle

A budget utilization ratio is meaningful only when its numerator and denominator
use the same accounting basis:

```
utilization = actual cost on the budget basis / budget amount on that basis
```

The existing dashboard billing-report metric is list cost. Tencent's CICD H2
budget is instead a net-cost commitment. Dividing Tencent list cost by that net
budget is not a conservative estimate; it is an invalid comparison.

`vendor` is not the accounting basis. A future Tencent plan may use list cost,
and a future plan for another vendor may use net cost. Therefore the basis belongs
to the **budget plan**, not to the vendor or cost source.

## Scope

The CI Cost Weekly tab remains a fixed, filterless report at
`/ci-cost-weekly` and keeps its existing completed-period behavior:

- one utilization gauge for each account for the last complete UTC week;
- one utilization gauge for each account for the last complete UTC month; and
- one UTC-weekly cumulative chart for the active CI budget period.

The change is limited to choosing the correct actual-cost basis for a budget
plan and labelling mixed-basis output truthfully.

Out of scope:

- changing QA Cost Weekly or the general Cost page;
- changing the source billing data, attribution rules, or historical budget
  amounts;
- inferring net cost from the vendor;
- adding per-budget currencies, FX history, forecasts, or a new allocation
  system.

## Budget-plan data contract

`cost-insight` owns the shared `insight.cost_budgets` table.

### Migration baseline and plan identity

The deployed budget-plan schema already has `accounts`, `projects`, and
`platform`; existing CICD plans use those columns. This change does not recreate
or migrate that pre-existing plan scope. It requires the target plan to have
verified JSON `accounts` membership and `platform = 'CICD'`; there is no legacy
`account_id` fallback.

`sql/029_add_ci_budget_cost_basis.sql` adds the only new column:

```sql
ALTER TABLE cost_budgets
  ADD COLUMN IF NOT EXISTS cost_basis VARCHAR(16) NOT NULL DEFAULT 'list_cost'
  AFTER budget_amount;
```

Allowed values are exactly:

| value | Actual amount |
| --- | --- |
| `list_cost` | dashboard billing-report list-cost expression |
| `net_cost` | `cost_attribution_daily.net_cost`, converted to USD |

Budget creation/upsert validation must reject every other value. The dashboard
must also treat an unknown value as a configuration error rather than silently
falling back to list cost.

The default preserves the meaning of every existing budget row without a bulk
backfill. The migration seeds the one exceptional plan by the same stable plan
identity used by the CI query, never by an auto-increment ID or legacy
`account_id`:

```sql
WHERE vendor = 'tencent'
  AND LOWER(TRIM(platform)) = 'cicd'
  AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
  AND budget_name = 'PingCAP CICD H2 Tencent 2026'
  AND period_start_date = '2026-09-01'
  AND period_end_date = '2027-03-31'
```

The seed phase is performed with budget writers paused (or otherwise serialized)
so the count and update cannot race. The migration executes this sentinel and
its `UPDATE` in one script. It prints `CI_BUDGET_TARGET_COUNT_OK` only for one
target; otherwise the invalid JSON passed to `JSON_EXTRACT` causes a statement
error and prevents the following `UPDATE`. The `UPDATE` repeats this exact
predicate and embeds the same count check so an error-continuing runner cannot
seed multiple rows:

```sql
SELECT CASE
  WHEN COUNT(*) = 1 THEN 'CI_BUDGET_TARGET_COUNT_OK'
  ELSE JSON_EXTRACT(CONCAT('CI_BUDGET_TARGET_COUNT_INVALID:', COUNT(*)), '$')
END AS seed_guard
FROM cost_budgets
WHERE vendor = 'tencent'
  AND LOWER(TRIM(platform)) = 'cicd'
  AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
  AND budget_name = 'PingCAP CICD H2 Tencent 2026'
  AND period_start_date = '2026-09-01'
  AND period_end_date = '2027-03-31';
```

```sql
UPDATE cost_budgets
SET cost_basis = 'net_cost'
WHERE vendor = 'tencent'
  AND LOWER(TRIM(platform)) = 'cicd'
  AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
  AND budget_name = 'PingCAP CICD H2 Tencent 2026'
  AND period_start_date = '2026-09-01'
  AND period_end_date = '2027-03-31'
  AND 1 = (
    SELECT COUNT(*)
    FROM (
      SELECT id
      FROM cost_budgets
      WHERE vendor = 'tencent'
        AND LOWER(TRIM(platform)) = 'cicd'
        AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
        AND budget_name = 'PingCAP CICD H2 Tencent 2026'
        AND period_start_date = '2026-09-01'
        AND period_end_date = '2027-03-31'
      GROUP BY id
    ) AS ci_budget_target
  );
```

`budget_amount` continues to mean USD. Tencent billing facts are CNY today; the
actual net amount uses the same configured CNY-to-USD conversion as the existing
billing-report values. A currency field is not introduced because all current
budget amounts and dashboard money presentation are already USD.

`filter_hash` and the existing `uk_cost_budgets_scope` uniqueness key do not
change. They identify *where* cost belongs (vendor, accounts, projects, and
filters), while `cost_basis` identifies *how* that scoped actual is measured.
There is no `scope_key` column on `cost_budgets`. Two simultaneous budgets with
the same scope but different bases would be conflicting plans, not distinct
scopes.

## Calculation rules

### Fact expressions

For every relevant `cost_attribution_daily` fact, the CI query computes both
USD-normalized candidates before aggregation:

- **list cost:** the existing billing-report list-cost expression. It excludes
  GCP SKUs beginning `Compute Flexible Committed Use Discounts`; this is existing
  list-cost policy and remains unchanged.
- **net cost:** `net_cost`, converted from CNY to USD when `currency = 'CNY'`.
  It does not inherit the list-only GCP CUD exclusion.

Null amounts contribute zero. Negative adjustments remain negative. Rounding
occurs only after the requested account/period aggregate is calculated.

For a plan `P`, the actual is selected by `P.cost_basis`; no vendor conditional
selects the metric. The existing inclusive date-overlap proration of
`budget_amount` is unchanged.

CI Cost Weekly requires a single basis for each fixed source in one response.
Before aggregation, it validates every plan contributing to the completed
week, completed month, or active chart period. If a source would contribute
more than one basis—including overlapping plans with different bases—the
endpoint returns the configuration-error response defined below, rather than
combining the plans. Overlapping plans with the same basis are also a
configuration error, preventing duplicate facts from inflating utilization.
Non-overlapping same-basis plans retain the existing behavior. This makes
`accounts[].cost_basis` unambiguous.

### Completed-period gauges

For each fixed account and completed week/month:

1. find its source-wide CICD budget plans that overlap the period;
2. prorate each overlapping plan to its inclusive overlap days;
3. aggregate fact amounts for the plan's account scope using that plan's
   `cost_basis`; and
4. calculate `actual / prorated_budget`.

If a plan does not overlap the completed month, `period_budget` and
`utilization_pct` stay `null`; they must not be replaced by zero. For example,
Tencent has no August 2026 plan, so its last-complete-month card remains
**Not configured**, even though its August actual cost can be observed.

### Active-period cumulative chart

The active CI plans define the chart's source/date scope. A fact contributes
only when it falls within that source's active plan interval, and uses that
plan's `cost_basis`.

The two plan amounts are both USD, so their chosen actuals may be added only as
**budget-basis spend**:

```
GCP list cost + Tencent net cost
```

This is useful for the aggregate CI budget envelope but is not a universal
cloud-cost metric. The chart must not be called "list cost" or simply "cost"
without qualification.

## API and presentation contract

The CI endpoint returns the selected basis with every account, for example:

```json
{
  "meta": {
    "calendar_timezone": "UTC",
    "cost_metric": "budget_basis_spend",
    "budget_basis_schema_available": true
  },
  "accounts": [
    {
      "cost_source": "gcp:pingcap-testing-account",
      "cost_basis": "list_cost",
      "last_complete_week": {
        "actual_cost": 7115.05,
        "period_budget": 2438.44,
        "utilization_pct": 291.79
      }
    },
    {
      "cost_source": "tencent:100050658403",
      "cost_basis": "net_cost",
      "last_complete_week": {
        "actual_cost": 0.0,
        "period_budget": 2773.58,
        "utilization_pct": 0.0
      }
    }
  ],
  "budget_period_cost": {
    "metric": "budget_basis_spend",
    "components": [
      {"cost_source": "gcp:pingcap-testing-account", "cost_basis": "list_cost"},
      {"cost_source": "tencent:100050658403", "cost_basis": "net_cost"}
    ],
    "points": [
      {
        "week_start": "2026-09-14",
        "budget_basis_cost": 300.0,
        "cumulative_budget_basis_cost": 600.0
      }
    ]
  }
}
```

The precise field rename from `actual_list_cost` to `actual_cost` is intentional:
it prevents callers from treating a net-cost value as list cost. The weekly
point fields likewise replace `list_cost` and `cumulative_list_cost` with
`budget_basis_cost` and `cumulative_budget_basis_cost`; the CI endpoint must
not emit the old point keys. The CI metadata likewise changes
`meta.cost_metric` from `list_cost` to `budget_basis_spend` and
`meta.budget_schema_available` to `meta.budget_basis_schema_available`. CI
frontend contract tests must assert all of these renames. This endpoint is new
and private to CI Cost Weekly, so no compatibility layer is needed. The shared
QA endpoint remains unchanged.

`WeeklyCostBudget` remains shared with QA. To preserve its QA list-cost
behavior, its field access is parameterized with
`actualCostKey = 'actual_list_cost'` and
`cumulativeCostKey = 'cumulative_list_cost'` defaults. `CICostWeeklyPage`
passes `actualCostKey="actual_cost"` to each gauge and
`cumulativeCostKey="cumulative_budget_basis_cost"` to the chart; the QA page
passes neither prop. Thus the CI endpoint need not emit deprecated fields and
QA continues to read exactly its current fields.

An unsupported stored basis, or the conflicting-plan condition above, returns
HTTP 409 with no partial account or chart data:

```json
{
  "error": {
    "code": "unsupported_cost_basis",
    "message": "CI budget configuration contains an unsupported cost basis.",
    "plans": [
      {
        "cost_source": "tencent:100050658403",
        "budget_name": "PingCAP CICD H2 Tencent 2026",
        "cost_basis": "gross_cost"
      }
    ]
  }
}
```

`conflicting_cost_basis` and `overlapping_cost_plans` use the same shape and
include each affected plan. The UI replaces the gauges and cumulative chart with a configuration-error
panel showing the message and affected source/plan; it must not render a
fallback or a partial aggregate.

The existing generic API error path discards non-OK response bodies, so the
minimal shared client change is required: `fetchJson` must parse a non-OK JSON
body once and throw an error that carries `status` and `payload`; `useApiData`
must retain its existing generic `error` message for current callers while also
exposing that `errorStatus` and `errorPayload`. `CICostWeeklyPage` recognizes
`errorStatus === 409`, reads `errorPayload.error`, and renders its `message` and
each plan's `cost_source` and `budget_name`. Other failures continue through
the existing generic panel error path.

The UI labels each gauge with its basis, for example:

- `GCP · pingcap-testing-account · Last complete week — List cost`
- `TENCENT · tencent-organization-billing · Last complete week — Net cost`

The cumulative chart title is **2026 H2 CI cumulative budget-basis spend** and
its subtitle discloses `GCP list cost + Tencent net cost`. It continues to show
the aggregate budget and utilization, but never labels this mixed-basis sum as
list cost.

If the new column is absent during a rolling deployment, the endpoint returns
`meta.budget_basis_schema_available: false` and no utilization data. The UI
explains that budget-basis metadata is not deployed. It must not assume
`list_cost`, because that would present an invalid Tencent utilization ratio.

## Implementation boundaries

1. Apply `cost-insight/sql/029_add_ci_budget_cost_basis.sql` and run its
   serialized, sentinel-checked Tencent seed. Extend any budget writer validation
   to accept only the two documented basis values.
2. In `ci-dashboard/src/ci_dashboard/api/queries/cost.py`, make the CI weekly
   query read `cost_basis`, aggregate both USD-normalized candidates, and select
   the candidate per plan for gauges and active-period points.
3. Update the CI weekly API/page contract and only the shared
   `WeeklyCostBudget` field accessors described above. Do not alter QA list-cost
   queries, `WeeklyCostPage`, or its rendered behavior.
4. Make the backward-compatible error-payload carrier change in
   `web/src/lib/api.js`; only `CICostWeeklyPage` consumes its 409 payload.
5. Update the SQLite test schema/fixtures and the CI weekly backend/frontend
   contract tests.

No job, materialized table, cache, or backfill is required: the page reads the
existing daily attribution facts at request time.

## Validation and rollout

Deploy in this order:

1. verify that the existing target plan has `platform = 'CICD'` and JSON
   `accounts` containing `100050658403`;
2. apply `029_add_ci_budget_cost_basis.sql`; its embedded sentinel must print
   `CI_BUDGET_TARGET_COUNT_OK` before the seed `UPDATE` runs. Confirm every
   non-target row reads as `list_cost`, and confirm the
   JSON-identity selector returns exactly one Tencent row with
   `cost_basis = 'net_cost'` and a USD amount;
3. deploy the dashboard change; and
4. compare the CI page's Tencent net-cost aggregate with a direct
   `cost_attribution_daily` net-cost aggregate for the same UTC date range. The
   direct query must use the page's CNY-to-USD conversion and net-cost handling
   (including no list-only GCP flexible-CUD exclusion), so the values are
   comparable.

Required tests:

- a Tencent fixture where `list_cost != net_cost` proves its gauges and
  cumulative contribution use `net_cost`;
- a GCP fixture proves list-cost behavior, including flexible-CUD exclusion,
  is unchanged;
- a source with an unknown basis returns the documented HTTP 409 error and the
  UI configuration-error panel renders its response message and affected plan,
  not a list-cost fallback;
- overlapping source plans with different bases return `conflicting_cost_basis`,
  while same-basis overlaps return `overlapping_cost_plans`;
- an absent `cost_basis` column yields the explicit metadata-not-deployed state;
- the CI contract uses `budget_basis_schema_available` and
  `budget_basis_spend`, and the aggregate chart reports its two component bases
  and only the budget-basis point keys; and
- existing QA Cost Weekly tests remain unchanged and passing.
