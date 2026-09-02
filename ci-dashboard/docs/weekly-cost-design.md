# QA Cost Weekly Design

## Scope and page contract

Add a `QA Cost Weekly` tab at `/qa-cost-weekly`, backed by `GET /api/v1/pages/weekly-cost`.
It is a fixed weekly-review report for QA cloud accounts:

- summary cards show the previous complete natural week's total, its week-over-week
  change against the preceding complete natural week, and the previous complete
  natural month's total;
- an account table shows cloud/account identity, configured purpose, last-week
  list cost, week-over-week change, QA share, and last-natural-month list cost;
- a fixed eight-week historical **list-cost** stacked chart appears below that
  table; and
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

The history window starts `49` days before `last_week.start_date` and ends on
`last_week.end_date`. It is exactly eight adjacent Monday-through-Sunday UTC
weeks, in ascending order: the seven complete weeks preceding `last_week`, then
`last_week` itself. Each history x-axis bucket is its Monday `start_date`.

`cost-insight/` owns the shared schema, migrations, and writes. `ci-dashboard/`
only has read access to `cost_sources` and `cost_attribution_daily`; it must not
create, migrate, or update either table. Every cost value on this report uses the
dashboard billing-report list-cost accounting expression; it never queries or
displays net cost.

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
zero summary values, no items, and a present `list_cost_history` with its normal
metric, eight date buckets, and `series: []`, with
`meta.purpose_schema_available: false`; the UI says that QA source metadata is
not deployed, rather than claiming there are no QA accounts. Once the column
exists, `purpose_schema_available` is true; a valid schema with no qualifying
sources returns the same zero/empty report (including empty history series) but
with the normal "no QA sources configured" empty state. Rollback leaves the
additive nullable column in place and rolls back only the dashboard binary.

## Metrics and API

Every qualifying QA source and every report period, including all summary,
account-table, and history values, uses the dashboard's established billing-report
list-cost accounting expression: a fact contributes `list_cost`, except a GCP
fact whose `sku_name LIKE 'Compute Flexible Committed Use Discounts%'` contributes
`0`. Null list cost contributes zero to an aggregate; negative adjustments remain
negative. Round only after aggregation for JSON/display values. This report never
uses `net_cost`, `effective_cost`, or an allocation adjustment.

- **WoW %** = `(last_week_cost - previous_week_cost) / previous_week_cost * 100`.
  It is `null` when the preceding-week total is zero.
- **QA share %** = `source.last_week_cost / SUM(last_week_cost for all qualifying
  QA sources) * 100`. The denominator includes every qualifying QA source, not
  all cloud accounts, and is not changed by chart selection. It is `null` when
  that QA total is zero.

`list_cost_history.metric` is `list_cost`. Each point and `total_list_cost` is
rounded only after its respective aggregate; `total_list_cost` is the source's
aggregate across all eight weeks.

For `D = 2026-07-20`, the response shape (shown with one source) is:

```json
{
  "meta": {
    "calendar_timezone": "UTC",
    "cost_metric": "list_cost",
    "purpose_schema_available": true
  },
  "last_week": {"start_date": "2026-07-13", "end_date": "2026-07-19"},
  "previous_week": {"start_date": "2026-07-06", "end_date": "2026-07-12"},
  "previous_month": {"start_date": "2026-06-01", "end_date": "2026-06-30"},
  "summary": {
    "last_week_cost": 1200.0,
    "previous_week_cost": 0.0,
    "week_wow_pct": null,
    "previous_month_cost": 1400.0
  },
  "items": [
    {
      "cost_source": "aws:946646677266",
      "vendor": "aws",
      "account_id": "946646677266",
      "display_name": "qa-infra-dev",
      "purpose": "机器统一资源池及重点项目测试",
      "last_week_cost": 1200.0,
      "previous_week_cost": 0.0,
      "week_wow_pct": null,
      "last_week_share_pct": 100.0,
      "previous_month_cost": 1400.0
    }
  ],
  "list_cost_history": {
    "metric": "list_cost",
    "start_date": "2026-05-25",
    "end_date": "2026-07-19",
    "weeks": [
      {"start_date": "2026-05-25", "end_date": "2026-05-31"},
      {"start_date": "2026-06-01", "end_date": "2026-06-07"},
      {"start_date": "2026-06-08", "end_date": "2026-06-14"},
      {"start_date": "2026-06-15", "end_date": "2026-06-21"},
      {"start_date": "2026-06-22", "end_date": "2026-06-28"},
      {"start_date": "2026-06-29", "end_date": "2026-07-05"},
      {"start_date": "2026-07-06", "end_date": "2026-07-12"},
      {"start_date": "2026-07-13", "end_date": "2026-07-19"}
    ],
    "series": [
      {
        "cost_source": "aws:946646677266",
        "vendor": "aws",
        "account_id": "946646677266",
        "display_name": "qa-infra-dev",
        "purpose": "机器统一资源池及重点项目测试",
        "total_list_cost": 2600.0,
        "points": [
          {"week_start": "2026-05-25", "list_cost": 0.0},
          {"week_start": "2026-06-01", "list_cost": 0.0},
          {"week_start": "2026-06-08", "list_cost": 0.0},
          {"week_start": "2026-06-15", "list_cost": 1400.0},
          {"week_start": "2026-06-22", "list_cost": 0.0},
          {"week_start": "2026-06-29", "list_cost": 0.0},
          {"week_start": "2026-07-06", "list_cost": 0.0},
          {"week_start": "2026-07-13", "list_cost": 1200.0}
        ]
      }
    ]
  }
}
```

`weeks` and each series' `points` are ascending by Monday and contain exactly
eight entries. The first history week is `last_week.start_date - 49 days`; the
last is the existing `last_week` exactly, so `list_cost_history.start_date` and
`end_date` are inclusive. Every active, non-blank-purpose source appears in both
`items` and `series`, even with no facts, with eight zero-valued points. Order
`series` by descending unrounded eight-week `total_list_cost`, then ascending
stored `vendor`, then ascending stored `account_id`; the frontend renders that
returned order with the first (largest) series at the bottom of each stack.

A qualifying source with no matching cost rows remains in `items` with `0.0`
amounts, so configuration is visible; its WoW and QA-share percentages are
`null` when their denominators are zero. With no qualifying sources, `items` and
`list_cost_history.series` are `[]`, the three summary amounts are `0.0`, and
`summary.week_wow_pct` is `null`. Zero denominators always use `null`, never a
fabricated `0%`.

## Table and chart presentation

The account table displays list-cost fields: **Account**, **Purpose**, **Last
week**, **WoW**, **QA share**, and **Last natural month**. The compatible
`previous_week_cost` and `week_wow_pct` response fields supply the comparison.

- The **Last week** and **Last natural month** headers each use two lines: the
  metric title first, then its inclusive fixed UTC range (`YYYY-MM-DD –
  YYYY-MM-DD`) from `last_week` or `previous_month` directly beneath it.
- Account and Purpose are left-aligned. Every money or percentage header and
  cell is right-aligned, including Last week, WoW, QA share, and Last natural
  month.
- A row's WoW contains only the existing percent formatter's percentage text,
  with no `WoW` prefix. Only a strictly positive value greater than `30%` is
  bold red; zero, null, negative values, and values up to exactly `30%` are
  normal. Summary-card WoW styling is unchanged.
- Treat the Account column width as `A`. Purpose may be at most `1.5A` and uses
  normal wrapping plus `overflow-wrap: anywhere`, so unbroken account notes or
  long words cannot expand the table.

Below the table, render one fixed stacked bar chart titled **Cost trend** from
`list_cost_history`. The x-axis labels are the eight `weeks[].start_date` Monday
dates, in response order; there is no date picker, filter, or arbitrary time
selection. In the unfocused **All** state, render every returned series in order
with a deterministically assigned, high-contrast, visually distinct palette color.
Beside or below the chart, provide an **All** control followed by one account
control per series. Selecting an account replaces the rendered stack with only
that account while retaining its original palette color; selecting All restores
every series. This is browser presentation state keyed by `cost_source`, not a
URL value or API query parameter, and it does not change the table, cards,
QA-share denominator, or history data fetched.

Hovering any week shows a tooltip with its Monday-to-Sunday date range, every
visible source's formatted list-cost value, and the formatted total of those
visible sources. Thus the All tooltip totals the full stack and an account-focus
tooltip totals that one source. The chart panel subtitle identifies the metric as
billing-report list cost and discloses the GCP flexible committed-use-discount
exclusion.

## Acceptance and validation

Backend acceptance:

- freeze the UTC clock and verify Monday-through-Sunday, preceding-week,
  previous-month, and all eight history boundaries, including month/year
  transitions; the eighth history bucket is exactly `last_week`;
- verify only active, non-blank-purpose sources are selected, including the two
  initial accounts and a newly configured QA source; a configured source without
  facts has a normal zero-cost item and eight zero history points;
- verify facts outside every fixed report period or the eight-week history
  window, and non-QA sources, are excluded;
- verify every summary, item, WoW, QA-share, history point, and source total
  uses billing-report list cost, including exclusion of the exact GCP
  flexible-CUD SKU prefix, null/negative list-cost handling, and series sorting
  by total descending then vendor and account ascending;
- verify WoW and QA-share denominators, including zero-denominator `null`; and
  verify old-schema fallback returns `cost_metric: list_cost` and
  `purpose_schema_available: false` without querying `purpose`, plus the normal
  eight-bucket history shape with no series.

Frontend acceptance:

- `QA Cost Weekly` is reachable from navigation and renders billing-report
  list-cost summary and table values; the Last week and Last natural month table
  headers have the title over the fixed UTC range, numeric/percentage cells are
  right-aligned, and long Purpose text wraps within the `1.5 × Account` limit;
- the restored row-level WoW column is between Last week and QA share, contains
  only percentage text, and is bold red only above `30%` (with `30%`, `30.01%`,
  and null covered by regression tests);
- below the table **Cost trend** renders eight Monday labels and every history
  series as a returned-order stack, with the largest eight-week series at the
  bottom and an explicit distinct palette color that remains stable on focus;
- its All and account controls focus only the chart in client state, restore the
  full stack with All, issue no filtered/refetched request, and provide a weekly
  tooltip with each visible source value and total; and
- it makes the fixed report request without global filters or a time-selection
  control, and distinguishes the old-schema state, no configured QA sources, and
  a populated zero-cost source without crashing.

Verification commands for implementation are:

```bash
cd ci-dashboard && make test-cov
cd ci-dashboard/web && npm test && npm run build
```

`make test-cov` is the repository's enforced backend coverage check and must
finish at or above its configured 90% threshold; the frontend commands must
pass before release.
