import { useState } from "react";

import {
  formatCompactCurrency,
  formatCurrency,
  formatDateRangeLabel,
  formatPercent,
  useApiData,
} from "../lib/api";
import { DonutShareChart, PageIntro, Panel, StatCard, TrendChart } from "../components/charts";
import { buildDimensionChipClassName, SegmentedControl } from "../components/controls";

const WEEKLY_COST_VENDOR_ORDER = ["aws", "gcp", "azure"];

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
  const report = useApiData("/api/v1/pages/weekly-cost", { include_trend: false });
  const trendReport = useApiData(
    "/api/v1/pages/weekly-cost/trend",
    {},
    report.data?.meta?.purpose_schema_available === true,
  );
  const summary = report.data?.summary || {};
  const lastWeek = report.data?.last_week || {};
  const previousWeek = report.data?.previous_week || {};
  const previousMonth = report.data?.previous_month || {};
  const historySeries = trendReport.data?.list_cost_history?.series || [];
  const [selectedCostSource, setSelectedCostSource] = useState("");
  const [accountSort, setAccountSort] = useState({ field: null, direction: "desc" });
  const [allocationPeriod, setAllocationPeriod] = useState("week");
  const monthlyAllocation = useApiData(
    "/api/v1/pages/weekly-cost/allocation",
    { period: "month" },
    allocationPeriod === "month",
  );
  const allocation = allocationPeriod === "month" ? monthlyAllocation.data : report.data;
  const budgetPace = allocation?.budget_pace || report.data?.budget_pace;
  const teamCost = budgetPace?.team_cost;
  const teamShare = allocation?.team_share || report.data?.team_share;
  const allocationPeriodRange =
    allocation?.period ||
    (allocationPeriod === "month" ? previousMonth : budgetPace?.period) ||
    budgetPace?.period ||
    lastWeek;
  const allocationLoading = report.loading || (allocationPeriod === "month" && monthlyAllocation.loading);
  const allocationError = allocationPeriod === "month" ? monthlyAllocation.error : report.error;
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
  const accountItems = sortWeeklyCostItems(report.data?.items || [], accountSort);
  const vendorTotals = weeklyCostVendorTotals(report.data?.items || []);
  const handleAccountSort = (field) => {
    setAccountSort((current) => ({
      field,
      direction: current.field === field && current.direction === "desc" ? "asc" : "desc",
    }));
  };

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

      {budgetPace ? (
        <Panel
          title="Budget pace"
          subtitle={`${allocationPeriod === "week" ? "Last natural week" : "Last natural month"} ${formatDateRangeLabel(
            allocationPeriodRange.start_date,
            allocationPeriodRange.end_date,
          )}. Cross-accounts cost allocation.`}
          loading={allocationLoading}
          error={allocationError}
          actions={<AllocationPeriodToggle value={allocationPeriod} onChange={setAllocationPeriod} />}
        >
          <div className="weekly-cost__budget-grid">
            <div className="weekly-cost__budget-lane">
              <BudgetPaceCard title="Overall budget pace" item={budgetPace.overall} />
              <TeamCostList
                title="Team test cost"
                items={teamCost?.items}
                emptyMessage="No Engineering Group team cost was attributed in this week."
              />
            </div>
            <BudgetPaceList
              className="weekly-cost__budget-projects"
              title="Project test budget utilization"
              items={budgetPace.projects}
              emptyMessage="No project cost was attributed in this week."
            />
            <DonutShareChart
              title="Project allocation"
              subtitle="Cross-account QA project allocation"
              items={teamShare?.projects?.items || []}
              totalValue={teamShare?.total_list_cost}
              totalLabel="list cost"
              emptyMessage="No project share data for this week."
              className="weekly-cost__share-card weekly-cost__budget-project-share"
            />
          </div>
        </Panel>
      ) : null}

      {teamShare ? (
        <Panel
          title="Team share"
          subtitle={
            teamShare.root_group_available
              ? `QA list-cost allocation for ${formatDateRangeLabel(allocationPeriodRange.start_date, allocationPeriodRange.end_date)}.`
              : `QA list-cost allocation for ${formatDateRangeLabel(allocationPeriodRange.start_date, allocationPeriodRange.end_date)}. Engineering Group roster metadata is not available yet.`
          }
          loading={allocationLoading}
          error={allocationError}
          actions={<AllocationPeriodToggle value={allocationPeriod} onChange={setAllocationPeriod} />}
        >
          <div className="weekly-cost__share-grid">
            <DonutShareChart
              title="Level 1 groups"
              subtitle="Direct Engineering Group children"
              items={teamShare.level1?.items || []}
              totalValue={teamShare.total_list_cost}
              totalLabel="list cost"
              emptyMessage="No Level 1 group share data for this week."
              className="weekly-cost__share-card"
            />
            <DonutShareChart
              title="Level 2 teams"
              subtitle="Teams below each Level 1 group"
              items={teamShare.level2?.items || []}
              totalValue={teamShare.total_list_cost}
              totalLabel="list cost"
              emptyMessage="No Level 2 team share data for this week."
              className="weekly-cost__share-card"
            />
            <DonutShareChart
              title="Owner share"
              subtitle="Cross-account QA owner allocation"
              items={teamShare.owners?.items || []}
              totalValue={teamShare.total_list_cost}
              totalLabel="list cost"
              emptyMessage="No owner share data for this week."
              className="weekly-cost__share-card"
            />
          </div>
        </Panel>
      ) : null}

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
                  <SortableCostHeader
                    field="last_week_cost"
                    label={<PeriodLabel title="Last week" period={lastWeek} />}
                    sortLabel="Last week"
                    sort={accountSort}
                    onSort={handleAccountSort}
                  />
                  <SortableCostHeader
                    field="week_wow_pct"
                    label="WoW"
                    sortLabel="WoW"
                    sort={accountSort}
                    onSort={handleAccountSort}
                  />
                  <SortableCostHeader
                    field="last_week_share_pct"
                    label="QA share"
                    sortLabel="QA share"
                    sort={accountSort}
                    onSort={handleAccountSort}
                  />
                  <SortableCostHeader
                    field="previous_month_cost"
                    label={<PeriodLabel title="Last natural month" period={previousMonth} />}
                    sortLabel="Last natural month"
                    sort={accountSort}
                    onSort={handleAccountSort}
                  />
                </tr>
              </thead>
              <tbody>
                {vendorTotals.map((item) => (
                  <tr className="weekly-cost__vendor-total" key={`vendor-total:${item.vendor}`}>
                    <th scope="row">
                      <span className="weekly-cost__account">
                        <strong>{item.vendor.toUpperCase()} Sum</strong>
                      </span>
                    </th>
                    <td className="weekly-cost__purpose">Vendor total</td>
                    <td className="weekly-cost__number">{formatCurrency(item.last_week_cost)}</td>
                    <td className={weekWowClassName(item.week_wow_pct)}>
                      {formatNullablePercent(item.week_wow_pct)}
                    </td>
                    <td className="weekly-cost__number">{formatNullablePercent(item.last_week_share_pct)}</td>
                    <td className="weekly-cost__number">{formatCurrency(item.previous_month_cost)}</td>
                  </tr>
                ))}
                {accountItems.map((item) => (
                  <tr className="weekly-cost__account-row" key={item.cost_source}>
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
        loading={report.loading || trendReport.loading}
        error={report.error || trendReport.error}
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

function AllocationPeriodToggle({ value, onChange }) {
  return (
    <SegmentedControl
      ariaLabel="Budget pace and team share period"
      value={value}
      onChange={onChange}
      options={[
        { key: "week", label: "Last natural week" },
        { key: "month", label: "Last natural month" },
      ]}
    />
  );
}

function SortableCostHeader({ field, label, sortLabel, sort, onSort }) {
  const isActive = sort.field === field;
  const direction = isActive ? sort.direction : null;
  const nextDirection = direction === "desc" ? "ascending" : "descending";

  return (
    <th scope="col" className="weekly-cost__number weekly-cost__sortable-header">
      <button
        type="button"
        className="weekly-cost__sort-button"
        aria-label={`Sort ${sortLabel} ${nextDirection}`}
        aria-pressed={isActive}
        onClick={() => onSort(field)}
      >
        <span>{label}</span>
        <span className="weekly-cost__sort-indicator" aria-hidden="true">
          {direction === "desc" ? "↓" : direction === "asc" ? "↑" : "↕"}
        </span>
      </button>
    </th>
  );
}

function BudgetPaceCard({ title, item = {} }) {
  const budget = item.period_budget;
  const isConfigured = budget !== null && budget !== undefined;
  const utilization = Number(item.utilization_pct || 0);
  const progress = Math.min(Math.max(utilization, 0), 100);
  const tone = budgetUtilizationTone(item.utilization_pct);
  const gaugeEndAngle = Math.PI + (progress / 100) * Math.PI;

  return (
    <article className="weekly-cost__budget-card weekly-cost__budget-card--overall">
      <span className="weekly-cost__budget-title">{title}</span>
      {isConfigured ? (
        <div
          className="weekly-cost__budget-gauge"
          role="progressbar"
          aria-label={`${title} utilization gauge`}
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={Math.round(progress)}
        >
          <svg viewBox="0 0 220 158" role="img" aria-label={`${title} gauge`}>
            <path
              d={describeBudgetGaugeArc(Math.PI, Math.PI * 2)}
              className="weekly-cost__budget-gauge-track"
            />
            {progress > 0 ? (
              <path
                d={describeBudgetGaugeArc(Math.PI, gaugeEndAngle)}
                className={`weekly-cost__budget-gauge-fill weekly-cost__budget-gauge-fill--${tone}`}
              />
            ) : null}
            <text x="110" y="112" textAnchor="middle" className="weekly-cost__budget-gauge-value">
              {formatNullablePercent(item.utilization_pct)}
            </text>
          </svg>
        </div>
      ) : (
        <strong className="weekly-cost__budget-value">Not configured</strong>
      )}
      <span className="weekly-cost__budget-amount">
        {formatCurrency(item.actual_list_cost)} actual
        {isConfigured ? ` / ${formatCurrency(budget)} budget` : ""}
      </span>
    </article>
  );
}

function BudgetPaceList({ title, items = [], emptyMessage, className = "" }) {
  return (
    <article className={["weekly-cost__budget-card", className].filter(Boolean).join(" ")}>
      <h4>{title}</h4>
      {items.length ? (
        <div className="weekly-cost__budget-list">
          {items.map((item) => {
            const isConfigured = item.period_budget !== null && item.period_budget !== undefined;
            const utilization = Number(item.utilization_pct || 0);
            const progress = Math.min(Math.max(utilization, 0), 100);
            const tone = budgetUtilizationTone(item.utilization_pct);

            return (
              <div className="weekly-cost__budget-row" key={item.key || item.name}>
                <div className="weekly-cost__budget-row-head">
                  <strong>{item.name}</strong>
                  <span>
                    {formatCurrency(item.actual_list_cost)}
                    {isConfigured
                      ? ` / ${formatCurrency(item.period_budget)} · ${formatNullablePercent(item.utilization_pct)}`
                      : " · Not configured"}
                  </span>
                </div>
                {isConfigured ? (
                  <div
                    className="weekly-cost__budget-meter"
                    role="progressbar"
                    aria-label={`${item.name} budget utilization`}
                    aria-valuemin="0"
                    aria-valuemax="100"
                    aria-valuenow={Math.round(progress)}
                  >
                    <span
                      className={`weekly-cost__budget-fill weekly-cost__budget-fill--${tone}`}
                      style={{ width: `${progress}%` }}
                    />
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="weekly-cost__budget-empty">{emptyMessage}</p>
      )}
    </article>
  );
}

function TeamCostList({ title, items = [], emptyMessage }) {
  return (
    <article className="weekly-cost__budget-card">
      <h4>{title}</h4>
      {items.length ? (
        <div className="weekly-cost__budget-list">
          {items.map((item) => {
            const share = Math.min(Math.max(Number(item.share_pct || 0), 0), 100);

            return (
              <div className="weekly-cost__budget-row" key={item.key || item.name}>
                <div className="weekly-cost__budget-row-head">
                  <strong>{item.name}</strong>
                  <span>
                    {formatCurrency(item.actual_list_cost)} · {formatPercent(item.share_pct)} of QA cost
                  </span>
                </div>
                <div
                  className="weekly-cost__budget-meter"
                  role="progressbar"
                  aria-label={`${item.name} QA team cost share`}
                  aria-valuemin="0"
                  aria-valuemax="100"
                  aria-valuenow={Math.round(share)}
                >
                  <span
                    className="weekly-cost__team-cost-fill"
                    style={{ width: `${share}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="weekly-cost__budget-empty">{emptyMessage}</p>
      )}
    </article>
  );
}

function describeBudgetGaugeArc(startAngle, endAngle) {
  const center = 110;
  const radius = 74;
  const start = budgetGaugePoint(center, radius, startAngle);
  const end = budgetGaugePoint(center, radius, endAngle);
  const largeArc = endAngle - startAngle > Math.PI ? 1 : 0;

  return `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${largeArc} 1 ${end.x} ${end.y}`;
}

function budgetGaugePoint(center, radius, angle) {
  return {
    x: center + radius * Math.cos(angle),
    y: center + radius * Math.sin(angle),
  };
}

function budgetUtilizationTone(value) {
  const utilization = Number(value);
  if (!Number.isFinite(utilization) || utilization < 80) {
    return "healthy";
  }
  if (utilization <= 95) {
    return "warning";
  }
  return "danger";
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

function weeklyCostVendorTotals(items) {
  const totalLastWeekCost = items.reduce((total, item) => total + Number(item.last_week_cost || 0), 0);

  return WEEKLY_COST_VENDOR_ORDER.map((vendor) => {
    const vendorItems = items.filter((item) => String(item.vendor || "").toLowerCase() === vendor);
    const lastWeekCost = vendorItems.reduce((total, item) => total + Number(item.last_week_cost || 0), 0);
    const previousWeekCost = vendorItems.reduce(
      (total, item) => total + Number(item.previous_week_cost || 0),
      0,
    );
    const previousMonthCost = vendorItems.reduce(
      (total, item) => total + Number(item.previous_month_cost || 0),
      0,
    );

    return {
      vendor,
      last_week_cost: lastWeekCost,
      previous_week_cost: previousWeekCost,
      previous_month_cost: previousMonthCost,
      week_wow_pct: previousWeekCost ? ((lastWeekCost - previousWeekCost) / previousWeekCost) * 100 : null,
      last_week_share_pct: totalLastWeekCost ? (lastWeekCost / totalLastWeekCost) * 100 : null,
    };
  });
}

function sortWeeklyCostItems(items, sort) {
  if (!sort.field) {
    return items;
  }
  const direction = sort.direction === "asc" ? 1 : -1;

  return [...items].sort((left, right) => {
    const leftValue = left[sort.field];
    const rightValue = right[sort.field];
    const leftMissing = leftValue === null || leftValue === undefined;
    const rightMissing = rightValue === null || rightValue === undefined;
    if (leftMissing || rightMissing) {
      if (leftMissing && rightMissing) {
        return formatAccountLabel(left).localeCompare(formatAccountLabel(right));
      }
      return leftMissing ? 1 : -1;
    }
    const difference = Number(leftValue) - Number(rightValue);
    return difference === 0
      ? formatAccountLabel(left).localeCompare(formatAccountLabel(right))
      : difference * direction;
  });
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
