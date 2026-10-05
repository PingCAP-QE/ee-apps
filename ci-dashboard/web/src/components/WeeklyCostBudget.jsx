import {
  formatCompactCurrency,
  formatCurrency,
  formatPercent,
} from "../lib/api";
import { TrendChart } from "./charts";

export function BudgetUtilizationGauge({
  title,
  item = {},
  showMonthlyCumulativeCost = false,
  actualCostKey = "actual_list_cost",
}) {
  const budget = item.period_budget;
  const isConfigured = budget !== null && budget !== undefined;
  const utilization = Number(item.utilization_pct || 0);
  const progress = Math.min(Math.max(utilization, 0), 100);
  const tone = budgetUtilizationTone(item.utilization_pct);
  const gaugeEndAngle = Math.PI + (progress / 100) * Math.PI;
  const dailyListCost = item.daily_list_cost || [];
  const showCumulativeCost = showMonthlyCumulativeCost && dailyListCost.length > 0;
  const highestDailyCost = dailyListCost.reduce(
    (highest, point) =>
      !highest || Number(point.cumulative_list_cost) > Number(highest.cumulative_list_cost)
        ? point
        : highest,
    null,
  );
  const highestBudgetUsage =
    isConfigured && highestDailyCost
      ? (Number(highestDailyCost.cumulative_list_cost) / Number(budget)) * 100
      : null;

  return (
    <article className="weekly-cost__budget-card weekly-cost__budget-card--overall">
      <span className="weekly-cost__budget-title">{title}</span>
      {showCumulativeCost ? (
        <div className="weekly-cost__budget-cumulative">
          <TrendChart
            series={[
              {
                key: "cumulative-cost",
                label: "Cumulative cost",
                color: "#0f7c82",
                type: "line",
                points: dailyListCost.map((point) => [point.date, point.cumulative_list_cost]),
              },
            ]}
            ariaLabel={`${title} cumulative cost chart`}
            bucketAnnotations={
              highestDailyCost && highestBudgetUsage !== null
                ? [{ label: highestDailyCost.date, text: formatNullablePercent(highestBudgetUsage) }]
                : null
            }
            yFormatter={formatCompactCurrency}
            height={190}
            compactY
            leftPadding={48}
            bottomLabelSize={9}
            xLabelFormatter={formatMonthlyCostDay}
            tooltipLabelFormatter={formatMonthlyCostDay}
            annotationLabelSize={12}
            showLegend={false}
          />
        </div>
      ) : isConfigured ? (
        <div
          className="weekly-cost__budget-gauge"
          role="progressbar"
          aria-label={`${title} gauge`}
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
        {formatCurrency(item[actualCostKey])} actual
        {isConfigured ? ` / ${formatCurrency(budget)} budget` : ""}
      </span>
    </article>
  );
}

export function CumulativeWeeklyCostChart({
  item,
  title = "2026 H2 overall cumulative cost",
  cumulativeCostKey = "cumulative_list_cost",
}) {
  const points = item?.points || [];
  if (!points.length) {
    return null;
  }
  const totalBudget = Number(item.total_budget || 0);
  const peak = points.reduce(
    (highest, point) =>
      !highest || Number(point[cumulativeCostKey]) > Number(highest[cumulativeCostKey])
        ? point
        : highest,
    null,
  );
  const peakUtilization =
    totalBudget > 0 ? (Number(peak[cumulativeCostKey]) / totalBudget) * 100 : null;

  return (
    <article className="weekly-cost__cumulative-cost-trend">
      <header>
        <div className="weekly-cost__cumulative-cost-title">
          <strong>{title}</strong>
          <span>{formatIsoDateRange(item.period || {})}</span>
        </div>
        <div className="weekly-cost__cumulative-cost-summary">
          <strong>Budget Cumulative Spend {formatCurrency(peak[cumulativeCostKey])}</strong>
          <span>{formatCurrency(totalBudget)} budget · {formatNullablePercent(peakUtilization)}</span>
        </div>
      </header>
      <TrendChart
        series={[
          {
            key: "cumulative-cost",
            label: "Cumulative cost",
            color: "#0f7c82",
            type: "line",
            points: points.map((point) => [point.week_start, point[cumulativeCostKey]]),
          },
        ]}
        ariaLabel="Budget period cumulative cost chart"
        yFormatter={formatCompactCurrency}
        height={180}
        compactY
        leftPadding={52}
        bottomLabelSize={11}
        xLabelFormatter={formatMonthlyCostDay}
        tooltipLabelFormatter={formatWeeklyTooltipLabel}
        showLegend={false}
      />
    </article>
  );
}

export function budgetUtilizationTone(value) {
  const utilization = Number(value);
  if (!Number.isFinite(utilization) || utilization < 80) {
    return "healthy";
  }
  if (utilization <= 95) {
    return "warning";
  }
  return "danger";
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

export function formatNullablePercent(value) {
  return value === null || value === undefined ? "—" : formatPercent(value);
}

export function formatMonthlyCostDay(value) {
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) {
    return String(value || "");
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

export function formatIsoDateRange(period) {
  if (!period.start_date || !period.end_date) {
    return "";
  }
  return `${period.start_date} – ${period.end_date}`;
}

export function formatWeeklyTooltipLabel(weekStart) {
  const start = String(weekStart || "");
  const end = new Date(`${start}T00:00:00Z`);
  if (Number.isNaN(end.getTime())) {
    return start;
  }
  end.setUTCDate(end.getUTCDate() + 6);
  return `${start} – ${end.toISOString().slice(0, 10)}`;
}
