import { useState } from "react";
import { formatCurrency, formatDateRangeLabel, formatPercent, useApiData } from "../lib/api";
import { EmptyState, LabeledDonutShareChart, PageIntro, Panel, StatCard, TrendChart } from "../components/charts";
import {
  BudgetUtilizationGauge,
  CumulativeWeeklyCostChart,
} from "../components/WeeklyCostBudget";

const categoryCurrency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export default function CICostWeeklyPage() {
  const report = useApiData("/api/v1/pages/ci-weekly-cost");
  const [focusedHistorySeriesKey, setFocusedHistorySeriesKey] = useState(null);
  const accounts = report.data?.accounts || [];
  const lastCompleteWeek = report.data?.last_complete_week || {};
  const lastCompleteMonth = report.data?.last_complete_month || {};
  const accountPeriodCosts = report.data?.budget_period_cost?.accounts || [];
  const lastWeekCostShare = report.data?.last_week_cost_share || {};
  const categoryAccounts = report.data?.last_week_category_share?.accounts || [];
  const weeklyCostHistory = report.data?.weekly_cost_history?.series || [];
  const weeklyCostSeries = weeklyCostHistory
    .filter((item) =>
      (item.cost_source.startsWith("gcp:") && item.cost_metric === "list_cost")
      || (item.cost_source.startsWith("tencent:") && item.cost_metric === "net_cost"),
    )
    .map((item) => ({
      key: `${item.cost_source}:${item.cost_metric}`,
      label: `${formatAccountLabelForSource(item.cost_source)} ${formatCostBasis(item.cost_metric)}`,
      color: item.cost_source.startsWith("gcp:") ? "#1f4e79" : "#93d6d1",
      type: "bar",
      points: item.points.map((point) => [point.week_start, point.cost]),
    }));
  const visibleWeeklyCostSeries = focusedHistorySeriesKey
    ? weeklyCostSeries.filter((item) => item.key === focusedHistorySeriesKey)
    : weeklyCostSeries;
  const weeklyBudgetPoints = report.data?.weekly_cost_history?.budget_points || [];
  const weeklyBudgetSeries = weeklyBudgetPoints.some((point) => point.period_budget != null)
    ? [{
        key: "budget_target",
        label: "GCP + Tencent weekly budget",
        color: "#d1495b",
        type: "line",
        dash: true,
        points: weeklyBudgetPoints.map((point) => [point.week_start, point.period_budget]),
      }]
    : [];
  const lastWeekBudgetUtilization = totalBudgetUtilization(accounts);

  return (
    <div className="page-stack weekly-cost ci-weekly-cost">
      <PageIntro
        eyebrow="CI Cost Weekly"
        kicker={formatDateRangeLabel(lastCompleteWeek.start_date, lastCompleteWeek.end_date)}
      />

      <Panel
        title="Overall"
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
        <div className="ci-weekly-cost__accounts">
          {accounts.map((account) => {
            const periodCost = accountPeriodCosts.find(
              (item) => item.cost_source === account.cost_source,
            );
            return (
              <section className="ci-weekly-cost__account-block" key={account.cost_source}>
                <h4>{`${formatAccountLabel(account)} ${formatCostBasis(account.cost_basis)}`}</h4>
                <div className="ci-weekly-cost__account-row">
                  <StatCard
                    label={<PeriodLabel title="Last week" period={lastCompleteWeek} />}
                    value={formatCurrency(account.last_complete_week?.actual_cost)}
                    delta={formatWeekOverWeek(account.week_wow_pct)}
                    deltaTone={costDeltaTone(account.week_wow_pct)}
                  />
                  <StatCard
                    label={<PeriodLabel title="Last natural month" period={lastCompleteMonth} />}
                    value={formatCurrency(account.last_complete_month?.actual_cost)}
                    tone="amber"
                  />
                  <BudgetUtilizationGauge
                    title="Last complete week"
                    item={account.last_complete_week}
                    actualCostKey="actual_cost"
                  />
                  <BudgetUtilizationGauge
                    title="Last complete month"
                    item={account.last_complete_month}
                    actualCostKey="actual_cost"
                  />
                  <CumulativeWeeklyCostChart
                    item={periodCost}
                    title="Cumulative spend"
                    cumulativeCostKey="cumulative_budget_basis_cost"
                  />
                </div>
              </section>
            );
          })}
        </div>
      </Panel>

      <Panel
        title="Cost trend"
        loading={report.loading}
        error={report.error}
      >
        <div className="ci-weekly-cost__trend-layout">
          <BudgetUtilizationGauge
            title="Last week budget utilization · GCP list + Tencent net"
            item={lastWeekBudgetUtilization}
            actualCostKey="actual_cost"
          />
          <div>
            <TrendChart
              series={[...visibleWeeklyCostSeries, ...weeklyBudgetSeries]}
              yFormatter={formatCurrency}
              stackBars
              showLegend={false}
              showTooltipSum
              ariaLabel="Last 8 complete weeks stacked CI cost chart"
            />
            <div className="chart-legend ci-weekly-cost__history-legend">
              {weeklyCostSeries.map((item) => {
                const focused = focusedHistorySeriesKey === item.key;
                return (
                  <button
                    key={item.key}
                    type="button"
                    className={`chart-legend__item${focused ? " ci-weekly-cost__history-legend-item--focused" : ""}`}
                    aria-pressed={focused}
                    onClick={() => setFocusedHistorySeriesKey(focused ? null : item.key)}
                  >
                    <span className="chart-legend__swatch" style={{ backgroundColor: item.color }} />
                    <span>{item.label}</span>
                  </button>
                );
              })}
              {weeklyBudgetSeries.map((item) => (
                <div className="chart-legend__item" key={item.key}>
                  <span className="chart-legend__swatch" style={{ backgroundColor: item.color }} />
                  <span>{item.label}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </Panel>

      <Panel
        title="Last week list-cost share"
        subtitle="GCP and Tencent CI accounts combined."
        loading={report.loading}
        error={report.error}
      >
        <div className="donut-grid">
          <LabeledDonutShareChart
            title="Team share"
            items={lastWeekCostShare.teams?.items || []}
            totalValue={lastWeekCostShare.total_list_cost}
            totalLabel="list cost"
            centerValue={formatCurrency(lastWeekCostShare.total_list_cost)}
            centerLabel="list cost"
            metricValueFormatter={formatCurrency}
            emptyMessage="No team list-cost allocation for the last complete week."
          />
          <LabeledDonutShareChart
            title="Repository share"
            items={lastWeekCostShare.repos?.items || []}
            totalValue={lastWeekCostShare.total_list_cost}
            totalLabel="list cost"
            centerValue={formatCurrency(lastWeekCostShare.total_list_cost)}
            centerLabel="list cost"
            metricValueFormatter={formatCurrency}
            emptyMessage="No repository list-cost allocation for the last complete week."
          />
        </div>
      </Panel>

      <Panel
        title="Last week cost categories"
        subtitle="Grouped by billing service, GKE allocation scope, resource/bucket name, and service/project metadata—not a dynamic/fixed split. Charts show only categories > 1% of each account total; small categories remain in the tables and are not renormalized."
        loading={report.loading}
        error={report.error}
      >
        <div className="donut-grid">
          {categoryAccounts.map((account) => (
            <CategoryCostBreakdown key={account.cost_source} account={account} />
          ))}
        </div>
        {!categoryAccounts.length ? <EmptyState message="No cost category data available." /> : null}
        <p className="ci-weekly-cost__category-note">
          GKE/TKE and disks can include idle or shared services. Artifact Registry is not self-hosted
          Artifactory; GCP Jenkins cache has not been separately identified. Zot COS is storage only;
          shared compute and any external dedicated-line invoices are not separately identified here.
        </p>
      </Panel>
    </div>
  );
}

function CategoryCostBreakdown({ account }) {
  const title = `${formatAccountLabel(account)} ${formatCostBasis(account.cost_basis)} categories`;
  const items = account.items || [];
  const total = Number(account.total_cost || 0);
  const ciResourceKeys = account.vendor === "gcp" ? ["gke", "gcp_pvc"] : ["tke", "cbs"];
  const ciResourceCost = items.reduce(
    (sum, item) => sum + (ciResourceKeys.includes(item.key) ? Number(item.value || 0) : 0),
    0,
  );
  const visibleItems = total > 0 ? items.filter((item) => Number(item.value) > total / 100) : [];
  const visibleTotal = visibleItems.reduce((sum, item) => sum + Number(item.value), 0);
  const hasNegativeAdjustments = items.some((item) => Number(item.value) < 0);
  const missingDates = account.missing_dates || [];

  return (
    <section className="ci-weekly-cost__category-account" aria-label={title}>
      {!hasNegativeAdjustments || visibleTotal <= total ? (
        <LabeledDonutShareChart
          title={title}
          subtitle="Blank arc represents omitted categories ≤ 1%."
          items={visibleItems}
          totalValue={total}
          totalLabel="USD"
          centerValue={categoryCurrency.format(total)}
          centerLabel={formatCostBasis(account.cost_basis)}
          metricValueFormatter={categoryCurrency.format}
          emptyMessage={`No categories > 1% available for ${formatAccountLabel(account)}.`}
        />
      ) : (
        <EmptyState message="Cost adjustments prevent a proportional pie; see the table." compact />
      )}
      <p className="ci-weekly-cost__category-note" role={missingDates.length ? "status" : undefined}>
        {missingDates.length
          ? `${account.observed_dates?.length || 0}/7 dates with billing records. No records for ${missingDates.join(", ")}; totals may be incomplete.`
          : "Billing records cover all 7 dates; amounts may still be revised."}
      </p>
      <div className="table-scroll">
        <table className="data-table data-table--compact ci-weekly-cost__category-table" aria-label={`${title} table`}>
          <thead>
            <tr>
              <th scope="col">Category</th>
              <th scope="col">Cost (USD)</th>
              <th scope="col">Share</th>
              <th scope="col">Grouping basis</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.key}>
                <th scope="row">{item.name}</th>
                <td>{categoryCurrency.format(item.value)}</td>
                <td>{total ? `${Number(item.share_pct || 0).toFixed(2)}%` : "—"}</td>
                <td className="ci-weekly-cost__category-evidence">{item.evidence}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">Total</th>
              <td>{categoryCurrency.format(total)}</td>
              <td>{total ? "100.00%" : "—"}</td>
              <td />
            </tr>
            <tr aria-label="Dynamic CI resources subtotal">
              <th scope="row">Dynamic CI resources</th>
              <td>{categoryCurrency.format(ciResourceCost)}</td>
              <td>{total ? `${(ciResourceCost / total * 100).toFixed(2)}%` : "—"}</td>
              <td className="ci-weekly-cost__category-evidence">
                {account.vendor === "gcp" ? "GKE + Kubernetes PVC / Hyperdisk" : "TKE / Serverless + CBS disks"}.
                Included in Total; includes idle/shared costs.
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}

function PeriodLabel({ title, period }) {
  const range = period?.start_date && period?.end_date
    ? `${period.start_date} – ${period.end_date}`
    : "Date range unavailable";

  return (
    <span className="weekly-cost__period-label">
      <span>{title}</span>
      <span className="weekly-cost__period-range">{range}</span>
    </span>
  );
}

function formatAccountLabel(account) {
  return account.vendor === "gcp" ? "GCP" : "Tencent";
}

function formatAccountLabelForSource(costSource) {
  return costSource.startsWith("gcp:") ? "GCP" : "Tencent";
}

function totalBudgetUtilization(accounts) {
  const totals = accounts.reduce(
    (current, account) => {
      const item = account.last_complete_week || {};
      if (item.period_budget === null || item.period_budget === undefined) {
        return current;
      }
      return {
        actual_cost: current.actual_cost + Number(item.actual_cost || 0),
        period_budget: current.period_budget + Number(item.period_budget),
      };
    },
    { actual_cost: 0, period_budget: 0 },
  );
  return {
    ...totals,
    utilization_pct: totals.period_budget
      ? (totals.actual_cost / totals.period_budget) * 100
      : null,
  };
}

function formatWeekOverWeek(value) {
  if (value === null || value === undefined) {
    return "WoW —";
  }
  const numeric = Number(value);
  return `WoW ${numeric > 0 ? "+" : ""}${formatPercent(numeric)}`;
}

function costDeltaTone(value) {
  if (value === null || value === undefined) {
    return "neutral";
  }
  if (Number(value) < 0) {
    return "improved";
  }
  if (Number(value) > 0) {
    return "regressed";
  }
  return "neutral";
}

function formatCostBasis(value) {
  if (value === "list_cost") {
    return "list cost";
  }
  if (value === "net_cost") {
    return "net cost";
  }
  return "Not configured";
}
