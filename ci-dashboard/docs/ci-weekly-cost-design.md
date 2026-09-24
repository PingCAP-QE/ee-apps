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
- an active-plan cumulative series with `budget_basis_cost` and
  `cumulative_budget_basis_cost`;
- `meta.cost_metric: "budget_basis_spend"` and chart components that identify
  each source and basis.

For every plan window, actual cost is measured using that plan's `cost_basis`.
Budgets are prorated across the overlap with the completed period. The cumulative
series sums each active plan using its own basis. Its total is therefore labeled
**budget-basis spend**, not list cost.

Cost values are normalized to USD before comparison. This keeps GCP list cost
and Tencent net cost comparable to their USD budget amounts.

## Presentation contract

The CI page labels every gauge with its plan basis. Its cumulative chart states
that it is CI cumulative budget-basis spend and identifies the mixed aggregate
as GCP list cost plus Tencent net cost.

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
- the cumulative series uses the same mixed plan bases; and
- CI Cost Weekly renders the fixed endpoint, basis labels, and cumulative chart.

QA Cost Weekly keeps its existing list-cost contract and shared component
defaults.
