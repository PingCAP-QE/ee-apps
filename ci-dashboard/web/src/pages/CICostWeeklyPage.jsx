import { formatDateRangeLabel, useApiData } from "../lib/api";
import { PageIntro, Panel } from "../components/charts";
import {
  BudgetUtilizationGauge,
  CumulativeWeeklyCostChart,
} from "../components/WeeklyCostBudget";

export default function CICostWeeklyPage() {
  const report = useApiData("/api/v1/pages/ci-weekly-cost");
  const accounts = report.data?.accounts || [];
  const lastCompleteWeek = report.data?.last_complete_week || {};
  const lastCompleteMonth = report.data?.last_complete_month || {};

  return (
    <div className="page-stack weekly-cost ci-weekly-cost">
      <PageIntro
        eyebrow="CI Cost Weekly"
        title="CI budget pace, ready for the weekly review"
        description="Fixed to the GCP and Tencent CI billing accounts. Each completed period uses its CI plan's cost basis."
        kicker={formatDateRangeLabel(lastCompleteWeek.start_date, lastCompleteWeek.end_date)}
      />

      <Panel
        title="Budget pace"
        subtitle={`Last complete week ${formatDateRangeLabel(
          lastCompleteWeek.start_date,
          lastCompleteWeek.end_date,
        )} and month ${formatDateRangeLabel(
          lastCompleteMonth.start_date,
          lastCompleteMonth.end_date,
        )}.`}
        loading={report.loading}
        error={report.error}
      >
        <div className="weekly-cost__budget-overview">
          {accounts.map((account) => (
            <div className="weekly-cost__budget-lane weekly-cost__budget-lane--summary" key={account.cost_source}>
              <BudgetUtilizationGauge
                title={`${formatAccountLabel(account)} · Last complete week — ${formatCostBasis(account.cost_basis)}`}
                item={account.last_complete_week}
                actualCostKey="actual_cost"
              />
              <BudgetUtilizationGauge
                title={`${formatAccountLabel(account)} · Last complete month — ${formatCostBasis(account.cost_basis)}`}
                item={account.last_complete_month}
                actualCostKey="actual_cost"
              />
            </div>
          ))}
          <div>
            <p>Budget-basis spend: GCP list cost + Tencent net cost</p>
            <CumulativeWeeklyCostChart
              item={report.data?.budget_period_cost}
              title="2026 H2 CI cumulative budget-basis spend"
              cumulativeCostKey="cumulative_budget_basis_cost"
            />
          </div>
        </div>
      </Panel>
    </div>
  );
}

function formatAccountLabel(account) {
  return `${String(account.vendor || "").toUpperCase()} · ${account.display_name || account.account_id}`;
}

function formatCostBasis(value) {
  if (value === "list_cost") {
    return "List cost";
  }
  if (value === "net_cost") {
    return "Net cost";
  }
  return "Not configured";
}
