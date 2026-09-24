import { useState } from "react";
import { formatCurrency, formatDateRangeLabel, formatPercent, useApiData } from "../lib/api";
import { LabeledDonutShareChart, PageIntro, Panel, StatCard, TrendChart } from "../components/charts";
import {
  BudgetUtilizationGauge,
  CumulativeWeeklyCostChart,
} from "../components/WeeklyCostBudget";

export default function CICostWeeklyPage() {
  const report = useApiData("/api/v1/pages/ci-weekly-cost");
  const [focusedHistorySeriesKey, setFocusedHistorySeriesKey] = useState(null);
  const accounts = report.data?.accounts || [];
  const lastCompleteWeek = report.data?.last_complete_week || {};
  const lastCompleteMonth = report.data?.last_complete_month || {};
  const accountPeriodCosts = report.data?.budget_period_cost?.accounts || [];
  const lastWeekCostShare = report.data?.last_week_cost_share || {};
  const weeklyCostHistory = report.data?.weekly_cost_history?.series || [];
  const weeklyCostSeries = weeklyCostHistory.map((item) => ({
    key: `${item.cost_source}:${item.cost_metric}`,
    label: `${formatAccountLabelForSource(item.cost_source)} ${formatCostBasis(item.cost_metric)}`,
    color: weeklyCostColor(item.cost_source, item.cost_metric),
    stackGroup: item.cost_metric,
    type: "bar",
    points: item.points.map((point) => [point.week_start, point.cost]),
  }));
  const visibleWeeklyCostSeries = focusedHistorySeriesKey
    ? weeklyCostSeries.filter((item) => item.key === focusedHistorySeriesKey)
    : weeklyCostSeries;
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
              series={visibleWeeklyCostSeries}
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
    </div>
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

function weeklyCostColor(costSource, costMetric) {
  const isGcp = costSource.startsWith("gcp:");
  if (costMetric === "list_cost") {
    return isGcp ? "#1f4e79" : "#9cc9e7";
  }
  return isGcp ? "#0f7c82" : "#93d6d1";
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
