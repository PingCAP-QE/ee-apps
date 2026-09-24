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
- eight completed weekly points for each source and both list/net metrics; and
- `meta.cost_metric: "budget_basis_spend"`.

For every plan window, actual cost is measured using that plan's `cost_basis`.
Budgets are prorated across the overlap with the completed period. Each
cumulative series uses its account's active plan basis and is labeled
**budget-basis spend**, not list cost.

Cost values are normalized to USD before comparison. This keeps GCP list cost
and Tencent net cost comparable to their USD budget amounts.

## Presentation contract

The Overall panel has a GCP list cost block and a Tencent net cost block.
Each contains one equal-height row of last-week and last-natural-month spend
cards, completed-week and completed-month gauges, and a cumulative chart that
fills the remaining row width. The cost basis appears only in the block title.
The Cost trend panel places a last-week utilization gauge on the left. It sums
GCP list cost and Tencent net cost for both actual and budget, so its percentage
uses a consistent basis. On the right, the last eight completed weeks appear as
two bars per week (list cost and net cost). GCP uses the darker shade and
Tencent the lighter shade; blue identifies list cost and teal identifies net
cost. Selecting a legend item focuses that series; selecting it again restores
all series. A final panel shows team and repository list-cost shares for the
last complete week across both accounts.

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
  list and net metrics, and the share panels use both accounts' list cost; and
- CI Cost Weekly renders the fixed endpoint, basis labels, charts, and shares.

QA Cost Weekly keeps its existing list-cost contract and shared component
defaults.
