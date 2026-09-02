# Weekly Cost Design

## Scope and page contract

Add a `Weekly Cost` tab at `/weekly-cost`, backed by `GET /api/v1/pages/weekly-cost`.
It is a fixed weekly-review report for QA cloud accounts:

- summary cards show the previous complete natural week's total, its week-over-week
  change against the preceding complete natural week, and the previous complete
  natural month's total;
- an account table shows cloud/account identity, configured purpose, previous-week
  cost, week-over-week change, QA share, and previous-month cost; and
- account identity is `(vendor, account_id)`; `display_name` is presentation only.

The tab has no date, granularity, vendor, account, repository, branch, or other
filter that can change these report windows. It does not pass global dashboard
filters to the endpoint. The endpoint has no supported filter parameters and
always calculates the periods below.

Out of scope: cost anomaly detection, idle-resource governance, cleanup
recommendations, source CRUD, budget/forecast views, and arbitrary historical
period selection.

## Calendar and data contract

All calendar calculations use **UTC** on the server, matching the dashboard's
UTC date convention and the `DATE`-typed `cost_attribution_daily.usage_date`.
Dates are inclusive; no timestamp conversion is applied to `usage_date`.

For `D = datetime.now(UTC).date()` and Monday numbered `0`:

- last complete week: `start = D - (D.weekday() + 7) days`, `end = start + 6 days`;
- preceding complete week: the seven days ending the day before last-week start;
- last complete natural month: the first through last day of the calendar month
  immediately before `D`'s month.

For example, when `D` is Monday `2026-07-20`, the periods are `2026-07-13` through
`2026-07-19`, `2026-07-06` through `2026-07-12`, and `2026-06-01` through
`2026-06-30`. Thus a report never includes the in-progress week or month.
Late upstream corrections can change an already completed period; the page shows
the shared table's values at request time.

`cost-insight/` owns the shared schema, migrations, and writes. `ci-dashboard/`
only has read access to `cost_sources` and `cost_attribution_daily`; it must not
create, migrate, or update either table. The report aggregates the dashboard's
current-attribution `SUM(COALESCE(net_cost, 0))` by source and period; each
summary amount is the sum of qualifying QA sources only. It does not substitute
list or effective cost and does not apply a different allocation basis.

## QA source metadata and rollout

`cost-insight` adds this nullable source-owned field:

```sql
cost_sources.purpose VARCHAR(255) NULL
```

A QA source is exactly an active source whose `purpose` is non-null after
trimming whitespace. This is intentionally metadata-driven, not a hard-coded
account allowlist. `purpose` is both the user-visible description and the
inclusion signal; clearing it removes a source from this report. At initial
rollout the rows are:

| vendor | account_id | display_name | purpose |
| --- | --- | --- | --- |
| `gcp` | `qa-infra-dev` | `qa-infra-dev` | `机器统一资源池` |
| `aws` | `946646677266` | `qa-infra-dev` | `机器统一资源池及重点项目测试` |

Any later QA account is included by setting its non-blank purpose in
`cost_sources`; no dashboard deployment is required. An inactive source, or one
with NULL, empty, or whitespace-only purpose, is excluded even if its account
name contains `qa`.

Deployment sequence:

1. Apply the nullable `purpose` migration in `cost-insight` before a seed/upsert
   references the column.
2. Populate the two initial purposes and validate the source rows.
3. Deploy the dashboard tab and read endpoint after the shared schema is ready.

During a rolling deployment against the old schema (the column is absent), the
endpoint must not reference `purpose`. It returns the normal period metadata,
zero summary values, and no items with `meta.purpose_schema_available: false`;
the UI says that QA source metadata is not deployed, rather than claiming there
are no QA accounts. Once the column exists, `purpose_schema_available` is true;
a valid schema with no qualifying sources returns the same zero/empty report but
with the normal "no QA sources configured" empty state. Rollback leaves the
additive nullable column in place and rolls back only the dashboard binary.

## Metrics and API

For every qualifying QA source and each fixed period, sum `net_cost`. Round only
for the JSON/display value after aggregation. Negative costs remain negative.

- **WoW %** = `(last_week_cost - previous_week_cost) / previous_week_cost * 100`.
  It is `null` when the preceding-week total is zero.
- **QA share %** = `source.last_week_cost / SUM(last_week_cost for all qualifying
  QA sources) * 100`. The denominator includes every qualifying QA source, not
  all cloud accounts, and is not changed by an account table selection. It is
  `null` when that QA total is zero.

The API response is stable even when cost facts are absent:

```json
{
  "meta": {
    "calendar_timezone": "UTC",
    "cost_metric": "net_cost",
    "purpose_schema_available": true
  },
  "last_week": {"start_date": "2026-07-13", "end_date": "2026-07-19"},
  "previous_week": {"start_date": "2026-07-06", "end_date": "2026-07-12"},
  "previous_month": {"start_date": "2026-06-01", "end_date": "2026-06-30"},
  "summary": {
    "last_week_cost": 200.0,
    "previous_week_cost": 140.0,
    "week_wow_pct": 42.86,
    "previous_month_cost": 500.0
  },
  "items": [
    {
      "cost_source": "aws:946646677266",
      "vendor": "aws",
      "account_id": "946646677266",
      "display_name": "qa-infra-dev",
      "purpose": "机器统一资源池及重点项目测试",
      "last_week_cost": 120.0,
      "previous_week_cost": 80.0,
      "week_wow_pct": 50.0,
      "last_week_share_pct": 60.0,
      "previous_month_cost": 400.0
    }
  ]
}
```

A qualifying source with no matching cost rows remains in `items` with `0.0`
amounts, so configuration is visible; its WoW and QA-share percentages are
`null` when their denominators are zero. With no qualifying sources, `items` is
`[]`, the three summary amounts are `0.0`, and `summary.week_wow_pct` is `null`.
Zero denominators always use `null`, never a fabricated `0%`.

## Acceptance and validation

Backend acceptance:

- freeze the UTC clock and verify Monday-through-Sunday, preceding-week, and
  previous-month boundaries, including month/year transitions;
- verify only active, non-blank-purpose sources are selected, including the two
  initial accounts and a newly configured QA source;
- verify facts outside the three periods and non-QA sources are excluded, a
  configured source without facts remains a zero row, and `net_cost` (including
  credits/negative values) is the only cost metric;
- verify WoW and QA-share denominators, including zero-denominator `null`, and
  the old-schema metadata/empty-state response.

Frontend acceptance:

- `Weekly Cost` is reachable from navigation, renders the fixed UTC period labels,
  three summary values, purpose, WoW, QA share, and previous-month value;
- it makes the fixed report request without global filters and offers no control
  that changes the reporting window; and
- it distinguishes the old-schema state, no configured QA sources, and a
  populated zero-cost source without crashing.

Verification commands for implementation are:

```bash
cd ci-dashboard && make test-cov
cd ci-dashboard/web && npm test && npm run build
```

`make test-cov` is the repository's enforced backend coverage check and must
finish at or above its configured 90% threshold; the frontend commands must
pass before release.
