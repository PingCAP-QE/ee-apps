# Cost Budget Scope Design

## Decision

Keep `cost_budgets` as the only budget table. One row is one `budget_amount`,
one inclusive `period_start_date` / `period_end_date`, and exactly one scope.

| Scope | `vendor`, `account_id` | `label_filters` | Actual cost |
| --- | --- | --- | --- |
| Account | both non-NULL | `NULL` | That account's attribution List Cost |
| Project set | both NULL | `{"project":["project-a","project-b"]}` | List Cost of those attribution projects across all accounts |

`project` is `cost_attribution_daily.project`, not a cloud project ID. The
project array is a set: a fact contributes once even if its account also has an
account budget. Account and project-set budgets are independent choices and are
never added together.

## Schema and configuration

Add `cost-insight/sql/022_add_cost_budget_scope.sql`. After a manual preflight
has confirmed the existing rows are supported, it performs this backfill:

```sql
ALTER TABLE cost_budgets
  MODIFY COLUMN vendor VARCHAR(32) NULL,
  MODIFY COLUMN account_id VARCHAR(128) NULL,
  ADD COLUMN scope_key CHAR(64) NULL AFTER account_id;

UPDATE cost_budgets
SET scope_key = SHA2(CONCAT('account', CHAR(0), vendor, CHAR(0), account_id), 256)
WHERE vendor IS NOT NULL
  AND account_id IS NOT NULL;

ALTER TABLE cost_budgets
  MODIFY COLUMN scope_key CHAR(64) NOT NULL,
  DROP INDEX uk_cost_budgets_scope,
  ADD UNIQUE KEY uk_cost_budgets_scope_period (
    scope_key, period_start_date, period_end_date
  );
```

`scope_key` has one byte-exact contract, shared by the migration, manual SQL,
and `cost_insight.budgets.build_scope_key`:

```text
account:     SHA-256(UTF-8("account\0" + vendor + "\0" + account_id))
project set: SHA-256(UTF-8("project_set\0" + filter_hash))
```

For a project set, `filter_hash` remains the existing
`build_filter_hash(label_filters)` value, so its canonical project-array
behavior remains the source of truth. `scope_key` is typed separately from
`filter_hash`, so account and project-set scopes cannot collide. The migration
backfills only account scopes; a manual project-set insert supplies the
canonical `label_filters`, `filter_hash`, and the SQL-equivalent project-set
`scope_key`.

`filter_hash` remains `NOT NULL`; it is not part of the new unique key.
Account rows use `build_filter_hash(None)`; project sets use the hash of their
canonical project filter.

Budget rows remain manual database configuration; this change adds no CLI,
write API, or sync job. The manual insert procedure must reject `start > end`
and run this full-range locking check before its insert in one serializable
transaction:

```sql
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;
START TRANSACTION;

SELECT 1
FROM cost_budgets
WHERE scope_key = :scope_key
  AND period_start_date <= :requested_end_date
  AND period_end_date >= :requested_start_date
FOR UPDATE;

-- Reject and ROLLBACK when the SELECT returned any row; otherwise INSERT and COMMIT.
```

The scan must not be limited: it locks the full relevant range and covers any
later `period_start_date` that could overlap. The unique key blocks exact
duplicates; this check blocks partial overlaps.

Before applying the migration, run these read-only preflight checks and require
both result sets to be empty. (`SHA2('null', 256)` is the current
`build_filter_hash(None)`.)

```sql
SELECT id
FROM cost_budgets
WHERE vendor IS NULL
   OR account_id IS NULL
   OR label_filters IS NOT NULL
   OR group_id IS NOT NULL
   OR manager_id IS NOT NULL
   OR repo IS NOT NULL
   OR period_start_date > period_end_date
   OR filter_hash <> SHA2('null', 256);

SELECT left_budget.id AS left_id, right_budget.id AS right_id
FROM cost_budgets AS left_budget
JOIN cost_budgets AS right_budget
  ON left_budget.vendor = right_budget.vendor
 AND left_budget.account_id = right_budget.account_id
 AND left_budget.id < right_budget.id
 AND left_budget.period_start_date <= right_budget.period_end_date
 AND left_budget.period_end_date >= right_budget.period_start_date;
```

Rows returned by either check require manual cleanup before the migration.

Accepted new shapes are deliberately narrow:

- Account: non-NULL `vendor` and `account_id`, `label_filters IS NULL`.
- Project set: NULL `vendor` and `account_id`, and exactly one non-empty,
  duplicate-free `project` array of non-empty strings.

All other label filters are unsupported.

## Metric and date rules

Every budget comparison uses the existing Dashboard billing-report List Cost
expression, including the GCP Flexible CUD exclusion:

```sql
CASE
  WHEN c.vendor = 'gcp'
   AND c.sku_name LIKE 'Compute Flexible Committed Use Discounts%'
  THEN 0
  ELSE c.list_cost
END
```

This applies to actuals, bucket targets, pace, forecast, weekly comparison, and
`over_budget`. It never uses `net_cost`. Null List Cost is zero; negative
adjustments remain negative.

A period amount is prorated over its inclusive days. A chart bucket crossing
adjacent renewals sums only the overlapping day slices. A missing or expired
period has no target and no pace; remove the current 31-day expired-period
fallback. Pace still observes through today minus the existing four-day cost lag
and forecasts from up to the existing 14 observed days.

## Dashboard contract

Cost Insight owns the schema and manual configuration. CI Dashboard is read-only.

- `GET /api/v1/pages/cost-budget-scopes` lists one choice per `scope_key`: key,
  label, account identity when applicable, and project list for project sets.
  Its label is the newest period's non-empty `budget_name`; otherwise it is
  `vendor / account_id` or the comma-joined project list in the canonical sorted
  order from `canonicalize_label_filters`.
- Existing `GET /api/v1/pages/cost-trend` accepts `budget_scope=<scope_key>` in
  Budget mode. It aggregates that one scope's List Cost and returns its targets.
  `budget_scope` is exclusive of `cost_source`: requests that provide both are
  invalid and return 400; the frontend omits `cost_source` in Budget mode.
- Existing `GET /api/v1/pages/cost-budget-pace` accepts `budget_scope` and
  returns that scope's List-Cost pace or `budget_health: null` when no effective
  period contains the observed-through date.

The Cost page has local **Explore** (default) and **Budget** modes. Explore
keeps the current source filter and Project breakdown chips. Budget mode selects
one configured scope and shows its List-Cost trend, target, pace, and forecast.
Project chips never configure, select, or filter a budget.

Weekly account cards remain account-only. They resolve only the matching account
scope and change actual, WoW, target, and `over_budget` from Net Cost to
billing-report List Cost. The existing response field names `net_cost`,
`previous_net_cost`, and `net_cost_wow_pct` remain for compatibility, but in
these cards their semantics are List-Cost actual, previous actual, and WoW
percentage. When no account period contains the card end date, annual/period/
weekly budget are `NULL` and `over_budget` is false; no expired period is used.
Project-set budgets do not appear in those cards.

## Implementation and rollout

Affected files:

- `cost-insight/sql/022_add_cost_budget_scope.sql`
- `cost-insight/src/cost_insight/budgets.py` and `cost-insight/tests/test_budgets.py`
- `cost-insight/docs/system-design.md` (update the `cost_budgets` schema documentation)
- `ci-dashboard/src/ci_dashboard/api/queries/cost.py`,
  `api/queries/pages.py`, and `api/routes/pages.py`
- `ci-dashboard/tests/conftest.py` and `ci-dashboard/tests/api/test_routes.py`
- `ci-dashboard/web/src/pages/CostPage.jsx`, `components/charts.jsx`,
  `pages/WeeklySummaryPage.jsx`, and frontend tests

Rollout:

1. Back up and preflight `cost_budgets` for supported rows and non-overlapping
   derived account scopes.
2. Apply the migration. Do not insert project sets yet.
3. Deploy Dashboard API/UI; verify existing account budgets compare List Cost.
4. Add project-set budgets manually after the Dashboard release is live, using
   the specified hashes and overlap check.

Rollback stops new project-set inserts and rolls back the Dashboard binary. The
backfilled `scope_key` and nullable columns remain: the old Dashboard continues
to read unchanged account rows. Do not reverse the schema migration until new
rows have been exported and removed.

## Acceptance tests

- `build_scope_key` and the migration SQL yield the same account key; project
  ordering produces the same project-set `filter_hash` and `scope_key`.
- The manual preflight rejects invalid dates, unsupported legacy filters,
  mismatched hashes, and overlapping periods; the migration backfills supported
  account rows.
- Account scope includes only its account; a project set includes listed
  projects across multiple accounts exactly once; independent budgets are not
  summed.
- Targets prorate inclusive dates across renewals; gaps and expired periods
  produce no target or pace.
- Flexible CUD, null, negative, and differing Net/List Cost fixtures prove all
  budget outputs use billing-report List Cost.
- Budget mode requires a configured scope, uses List-Cost labels, and leaves
  Explore Project chips independent. Weekly account cards use List Cost only and
  do not use the expired-period fallback.

## Non-goals

- New scope/member/period tables.
- Arbitrary label filters.
- Configuration CLI or HTTP API.
- Lark synchronization or a budget sync job.
- Aggregating account and project-set budgets.
