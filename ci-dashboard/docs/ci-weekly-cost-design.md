# CI Cost Weekly

## Status

Implemented decision and rollout contract for CI Cost Weekly.

## Decision

CI Cost Weekly compares CI spending with CI budget plans for the fixed GCP and
Tencent billing accounts. It is not a filtered version of the general Cost page
or QA Cost Weekly.

`cost_budgets.cost_basis` is the source of truth for the cost metric attached to
each CI plan:

| Source | Basis |
| --- | --- |
| `gcp:pingcap-testing-account` | `list_cost` |
| `tencent:100050658403` | `net_cost` |

The migration adds `cost_basis` with `list_cost` as its default and updates the
stable Tencent H2 2026 CI plan to `net_cost`. Existing CI plan scope metadata is
not changed by this migration.

## Report contract

`GET /api/v1/pages/ci-weekly-cost` is a fixed CI report. It returns:

- completed-week and completed-month utilization for each configured source;
- each account's `cost_basis`, actual cost, prorated plan budget, and utilization;
- one active-plan cumulative series per account, each with
  `budget_basis_cost` and `cumulative_budget_basis_cost`;
- eight completed weekly points for each source and both list/net metrics;
- `weekly_cost_history.budget_points`, with the combined GCP and Tencent
  `period_budget` prorated across each completed week;
- `last_week_category_share`, with per-account category amounts, shares,
  cost basis, grouping evidence, and observed/missing billing dates; and
- `meta.cost_metric: "budget_basis_spend"`.

For every plan window, actual cost is measured using that plan's `cost_basis`.
Budgets are prorated across the overlap with the completed period. Each
cumulative series uses its account's active plan basis and is labeled
**budget-basis spend**, not list cost.

Cost values are normalized to USD before comparison. This keeps GCP list cost
and Tencent net cost comparable to their USD budget amounts.

## Progressive loading

The UI requests `GET /api/v1/pages/ci-weekly-cost` in three stages:

1. `section=overview`: account cards, gauges, cumulative spend, and eight-week
   history. Both top panels reuse one daily-cost aggregation.
2. `section=share`: last-week team and repository list-cost shares only.
3. `section=categories`: last-week billing categories only.

Each response includes the UTC calendar metadata and only its requested section
fields. The backend skips unrelated queries, not just unrelated JSON fields.
Omitting `section` (or using `section=all`) preserves the full report contract;
unsupported sections return HTTP 422 before any cost query.

Lower panels show loading placeholders until requested and completed. A failed
section shows its own error but still allows subsequent sections to load;
already rendered panels remain visible. Navigating away aborts pending requests.
The fixed accounts, accounting expressions, and calendar windows do not change.
The share query groups only by source/date/team/repository, not unused
owner/project subdivisions; the shared dimension helper receives NULL values
for the two unused fields. See the [loading and SQL validation evidence](validation-artifacts/2026-10-23/ci-cost-weekly-loading.md)
for measurements and post-deployment checks.

## Presentation contract

The Overall panel has a GCP list cost block and a Tencent net cost block.
Each contains one equal-height row of **Previous calendar week** and
**Previous calendar month** spend cards, **Last week's budget utilization** and
**Previous month's budget utilization** gauges, and a cumulative chart that
fills the remaining row width. The cost basis appears only in the block title.
The Cost trend panel places a **Last week's budget utilization** gauge on the left. It sums
GCP list cost and Tencent net cost for both actual and budget, so its percentage
uses a consistent basis. On the right, the last eight completed weeks appear as
one stacked bar per week, combining only GCP list cost (blue) and Tencent net
cost (teal). GCP net cost and Tencent list cost are not displayed in the chart,
legend, or tooltip totals. A red dashed line shows the combined weekly budget
on the same USD axis. It uses each week's overlapping plans, not the latest
week's budget repeated across history. A missing plan for either account leaves
that week's total budget unavailable rather than plotting a partial total.
The tooltip shows spend and budget separately; its spend sum excludes the
budget. Selecting a cost legend item focuses that series; selecting it again
restores both series. The total-budget reference line remains visible.
The team/repository panel shows list-cost shares for the last complete week
across both accounts. The final **Last week's cost categories** panel contains
separate GCP list-cost and Tencent net-cost pies and full category tables.
Only categories strictly greater than 1% of their account total appear in the
pies. The cutoff uses unrounded amounts, not rounded display percentages.
The full account total remains the denominator and the donut center; omitted
categories leave a blank arc rather than being redistributed or grouped into a
visible Others slice. Tables retain all nonzero categories, including small
and negative adjustments, with amounts to two decimal places. When negative
adjustments make displayed slices exceed the net total, the table remains
available but the pie is not drawn.

Each table also shows a **Dynamic CI resources** subtotal: GCP sums the GKE
and Kubernetes PVC / Hyperdisk categories; Tencent sums TKE / Serverless and
CBS disks. The subtotal uses unrounded amounts (including disk categories
omitted from the pie) and divides by the full account total. It is already
included in Total, never a new category or pie slice. This is a compute/disk
resource-pool summary, not proof of elastic task-only spending: it still
includes idle/shared cluster costs and CBS disks whose PVC ownership or
lifetime has not been established.

Category grouping is a deterministic, mutually exclusive SQL CASE on
`cost_attribution_daily`, scoped to the two fixed accounts and the report's
last complete calendar week. It is not team attribution or a pure label-based
classification:

- GCP native `gke_direct` / `gke_residual` allocation scopes identify GKE first;
  direct Compute Engine disks named `pvc-*` are separately listed as
  Kubernetes PVC / Hyperdisk. Other services use `service_name`, with known
  Bazel and Prow cache buckets identified by `resource_name`, and OpenClaw
  identified by its Compute resource name.
- Tencent uses billing service names for TKE/Serverless, CBS, networking, CVM,
  DNS/logs, and known COS bucket names for Bazel, Jenkins cache/artifacts, and
  Zot storage. Legacy COS attribution rows can lack bucket identity; in that
  case, `service` / `project` metadata identifies Bazel, Zot, or combined
  Jenkins cache/artifact storage. Do not invent a cache/artifact split for
  those Jenkins rows. Other resources remain in Other services/object storage.
- GKE/TKE include idle/system/shared services. A CI-tagged disk is not proof
  of dynamic lifetime. Artifact Registry is not self-hosted Artifactory,
  Prow cache is not proven GCP Jenkins cache, and Zot COS is not full Zot cost.
  External dedicated-line invoices are not assumed to be included or zero.

Category amounts reuse the report's USD conversion and GCP flexible committed
use discount exclusion, without per-resource thresholds or top-N truncation.
Each category has one classification, so tables reconcile to account totals.
The panel warns about dates without billing rows instead of treating them as
verified zero cost. Even seven observed dates do not guarantee billing
finality; exports may still revise amounts.

The shared weekly-cost gauges and chart retain their default list-cost field
keys. CI supplies the budget-basis field keys, so QA Cost Weekly remains
unchanged.

## Rollout

1. Apply `cost-insight/sql/029_add_ci_budget_cost_basis.sql` before deploying
   the CI Dashboard change.
2. Verify the Tencent H2 2026 CI plan has `cost_basis = 'net_cost'`; all other
   plans retain the `list_cost` default unless deliberately configured otherwise.
3. Deploy CI Dashboard and open `/ci-cost-weekly`.
4. Confirm the page shows the GCP and Tencent gauges and the cumulative
   budget-basis-spend chart.

## Validation

Focused checks cover that:

- GCP uses list cost even when net cost differs;
- Tencent uses USD-normalized net cost when it differs from list cost;
- the cumulative series uses plan basis, the eight-week series includes both
  list and net metrics, and the share panels use both accounts' list cost;
- CI Cost Weekly renders the fixed endpoint, basis labels, charts, and shares,
  with one weekly stacked bar containing only GCP list cost and Tencent net cost;
- weekly budget points use overlapping plans with prorated amounts, and the
  chart compares spend against a dashed budget line without including budget
  in the spend sum;
- progressive request ordering, visible top panels while lower requests are
  pending, independent errors, and request cancellation on navigation;
- section queries skip unrelated work and match the full report's amounts;
- category classification precedence, fixed account/date scope, USD conversion,
  reconciliation, small/negative amounts, and missing billing dates;
- two category pies appear at the bottom, the strict >1% cutoff retains the
  whole-account denominator, and tables retain excluded categories and cents.

QA Cost Weekly keeps its existing list-cost contract and shared component
defaults.
