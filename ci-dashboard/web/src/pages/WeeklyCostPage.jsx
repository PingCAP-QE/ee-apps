import { useState } from "react";

import {
  formatCompactCurrency,
  formatCurrency,
  formatDateRangeLabel,
  formatPercent,
  useApiData,
} from "../lib/api";
import { PageIntro, Panel, StatCard, TrendChart } from "../components/charts";
import { buildDimensionChipClassName } from "../components/controls";

const WEEKLY_COST_SERIES_COLORS = [
  "#0072b2",
  "#d55e00",
  "#009e73",
  "#cc79a7",
  "#6a3d9a",
  "#a65628",
  "#1b9e77",
  "#e7298a",
  "#7570b3",
  "#66a61e",
  "#b2182b",
  "#2166ac",
];

export default function WeeklyCostPage() {
  const report = useApiData("/api/v1/pages/weekly-cost");
  const summary = report.data?.summary || {};
  const lastWeek = report.data?.last_week || {};
  const previousWeek = report.data?.previous_week || {};
  const previousMonth = report.data?.previous_month || {};
  const historySeries = report.data?.list_cost_history?.series || [];
  const [selectedCostSource, setSelectedCostSource] = useState("");
  const hasSelectedCostSource = historySeries.some(
    (series) => series.cost_source === selectedCostSource,
  );
  const chartSeries = historySeries
    .map((series, index) => ({
      key: series.cost_source,
      label: formatAccountLabel(series),
      color: weeklyCostSeriesColor(index),
      type: "bar",
      points: (series.points || []).map((point) => [point.week_start, point.list_cost]),
    }))
    .filter((series) => !hasSelectedCostSource || series.key === selectedCostSource);

  return (
    <div className="page-stack weekly-cost">
      <PageIntro
        eyebrow="QA Cost Weekly"
        title="QA cloud spend, ready for the weekly review"
        description="This page is fixed to the previous complete natural week, Monday through Sunday in UTC. QA accounts are sources with a configured purpose."
        kicker={formatDateRangeLabel(lastWeek.start_date, lastWeek.end_date)}
      />

      <section className="stats-grid weekly-cost__summary">
        <StatCard
          label={<PeriodLabel title="Last week" period={lastWeek} />}
          value={formatCurrency(summary.last_week_cost)}
          delta={formatWeekOverWeek(summary.week_wow_pct)}
          deltaTone={costDeltaTone(summary.week_wow_pct)}
        />
        <StatCard
          label="Previous week cost"
          value={formatCurrency(summary.previous_week_cost)}
          detail={formatDateRangeLabel(previousWeek.start_date, previousWeek.end_date)}
          tone="teal"
        />
        <StatCard
          label={<PeriodLabel title="Last natural month" period={previousMonth} />}
          value={formatCurrency(summary.previous_month_cost)}
          tone="amber"
        />
      </section>

      <Panel
        title="QA account breakdown"
        subtitle="Costs use the billing-report list-cost expression. QA share is each account's share of all QA accounts in the previous complete week."
        loading={report.loading}
        error={report.error}
      >
        {report.data?.items?.length ? (
          <div className="table-scroll">
            <table className="data-table weekly-cost__table">
              <thead>
                <tr>
                  <th scope="col">Account</th>
                  <th scope="col">Purpose</th>
                  <th scope="col" className="weekly-cost__number">
                    <PeriodLabel title="Last week" period={lastWeek} />
                  </th>
                  <th scope="col" className="weekly-cost__number">WoW</th>
                  <th scope="col" className="weekly-cost__number">QA share</th>
                  <th scope="col" className="weekly-cost__number">
                    <PeriodLabel title="Last natural month" period={previousMonth} />
                  </th>
                </tr>
              </thead>
              <tbody>
                {report.data.items.map((item) => (
                  <tr key={item.cost_source}>
                    <th scope="row">
                      <span className="weekly-cost__account">
                        <strong>{formatAccountLabel(item)}</strong>
                        <small>{item.account_id}</small>
                      </span>
                    </th>
                    <td className="weekly-cost__purpose">{item.purpose}</td>
                    <td className="weekly-cost__number">{formatCurrency(item.last_week_cost)}</td>
                    <td className={weekWowClassName(item.week_wow_pct)}>
                      {formatNullablePercent(item.week_wow_pct)}
                    </td>
                    <td className="weekly-cost__number">{formatNullablePercent(item.last_week_share_pct)}</td>
                    <td className="weekly-cost__number">{formatCurrency(item.previous_month_cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : !report.loading && !report.error ? (
          <div className="empty-state">
            {report.data?.meta?.purpose_schema_available === false
              ? "QA source metadata is not deployed yet."
              : "No QA cost sources with a configured purpose."}
          </div>
        ) : null}
      </Panel>

      <Panel
        title="Cost trend"
        subtitle="List cost by account for the last eight complete UTC weeks. GCP Compute Flexible Committed Use Discounts are excluded."
        loading={report.loading}
        error={report.error}
      >
        <div className="weekly-cost__trend-layout">
          <TrendChart
            series={chartSeries}
            yFormatter={formatCompactCurrency}
            stackBars
            preserveLabelOrder
            xLabelFormatter={(value) => value}
            tooltipLabelFormatter={formatWeeklyTooltipLabel}
            bottomLabelSize={9}
            rotateBottomLabels
            showAllBottomLabels
            showTooltipSum
            showLegend={false}
          />
          {historySeries.length ? (
            <div
              className="dimension-selector weekly-cost__account-selector"
              aria-label="List cost account selector"
            >
              <button
                type="button"
                className={buildDimensionChipClassName(!hasSelectedCostSource)}
                aria-label="Show all accounts"
                aria-pressed={!hasSelectedCostSource}
                onClick={() => setSelectedCostSource("")}
              >
                <span>All</span>
              </button>
              {historySeries.map((series, index) => (
                <button
                  key={series.cost_source}
                  type="button"
                  className={buildDimensionChipClassName(series.cost_source === selectedCostSource)}
                  aria-label={`Show ${formatAccountLabel(series)}`}
                  aria-pressed={series.cost_source === selectedCostSource}
                  onClick={() => setSelectedCostSource(series.cost_source)}
                >
                  <span
                    className="weekly-cost__account-selector-dot"
                    style={{ backgroundColor: weeklyCostSeriesColor(index) }}
                    aria-hidden="true"
                  />
                  <span>{formatAccountLabel(series)}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </Panel>
    </div>
  );
}

function PeriodLabel({ title, period }) {
  const range = formatIsoDateRange(period);

  return (
    <span className="weekly-cost__period-label">
      <span>{title}</span>
      {range ? <span className="weekly-cost__period-range">{range}</span> : null}
    </span>
  );
}

function formatIsoDateRange(period) {
  if (!period.start_date || !period.end_date) {
    return "";
  }
  return `${period.start_date} – ${period.end_date}`;
}

function formatAccountLabel(item) {
  return `${String(item.vendor || "").toUpperCase()} · ${item.display_name || item.account_id}`;
}

function formatWeeklyTooltipLabel(weekStart) {
  const start = String(weekStart || "");
  const end = new Date(`${start}T00:00:00Z`);
  if (Number.isNaN(end.getTime())) {
    return start;
  }
  end.setUTCDate(end.getUTCDate() + 6);
  return `${start} – ${end.toISOString().slice(0, 10)}`;
}

function weeklyCostSeriesColor(index) {
  if (WEEKLY_COST_SERIES_COLORS[index]) {
    return WEEKLY_COST_SERIES_COLORS[index];
  }
  const hue = (280 + (index - WEEKLY_COST_SERIES_COLORS.length) * 137) % 360;
  return `hsl(${hue} 68% 38%)`;
}

function weekWowClassName(value) {
  return [
    "weekly-cost__number",
    "weekly-cost__wow",
    value !== null && value !== undefined && Number(value) > 30
      ? "weekly-cost__wow--alert"
      : "",
  ]
    .filter(Boolean)
    .join(" ");
}

function formatWeekOverWeek(value) {
  if (value === null || value === undefined) {
    return "WoW —";
  }
  const numeric = Number(value);
  return `WoW ${numeric > 0 ? "+" : ""}${formatPercent(numeric)}`;
}

function formatNullablePercent(value) {
  return value === null || value === undefined ? "—" : formatPercent(value);
}

function costDeltaTone(value) {
  if (value === null || value === undefined) {
    return "neutral";
  }
  const numeric = Number(value);
  if (numeric < 0) {
    return "improved";
  }
  if (numeric > 0) {
    return "regressed";
  }
  return "neutral";
}
