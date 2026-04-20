import { useId } from "react";

import {
  formatCompact,
  formatDateRangeLabel,
  formatNumber,
  formatPercent,
  formatSeconds,
} from "../lib/api";

export const BLIND_RETRY_LOOP_HINT =
  "On the Same Revision, if a job experiences 1 or more consecutive failures eventually followed by a new commit, the builds starting from the 2nd attempt onwards (up to the new commit) are classified as a blind retry loop.";

const DONUT_COLORS = [
  "#315772",
  "#2a9d8f",
  "#d1495b",
  "#bc6c25",
  "#7d8597",
  "#0f7c82",
  "#d88b3d",
  "#457b9d",
  "#8d5a97",
  "#4f772d",
];

const SERIES_COLORS = {
  total_count: "#315772",
  success_count: "#2a9d8f",
  failure_count: "#d1495b",
  success_rate_pct: "#f4a261",
  gcp_build_count: "#315772",
  idc_build_count: "#bc6c25",
  queue_avg_s: "#7f5539",
  run_avg_s: "#2a9d8f",
  total_avg_s: "#315772",
  flaky_rate_pct: "#d1495b",
  retry_loop_rate_pct: "#e9c46a",
  noisy_rate_pct: "#2a9d8f",
  new_case_count: "#d1495b",
  resolved_case_count: "#2a9d8f",
  issue_created_count: "#d1495b",
  issue_closed_count: "#2a9d8f",
  issue_reopened_count: "#e9c46a",
  total_failure_like_count: "#264653",
  issue_filtered_flaky_rate_pct: "#0f7c82",
  FLAKY_TEST: "#d1495b",
  UNCLASSIFIED: "#7d8597",
};

export function PageIntro({ eyebrow, title, description, kicker }) {
  return (
    <header className="page-intro">
      <div>
        <span className="page-intro__eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      {kicker ? <div className="page-intro__kicker">{kicker}</div> : null}
    </header>
  );
}

export function StatCard({ label, value, detail, delta, tone = "default" }) {
  return (
    <article className={`stat-card stat-card--${tone}`}>
      <span className="stat-card__label">{label}</span>
      <strong className="stat-card__value">{value}</strong>
      <div className="stat-card__meta">
        <span>{detail}</span>
        {delta ? <span className="stat-card__delta">{delta}</span> : null}
      </div>
    </article>
  );
}

export function InfoHint({ text }) {
  const tooltipId = useId();

  return (
    <span className="info-hint">
      <button
        type="button"
        className="info-hint__button"
        aria-label="Show metric definition"
        aria-describedby={tooltipId}
      >
        i
      </button>
      <span id={tooltipId} role="tooltip" className="info-hint__tooltip">
        {text}
      </span>
    </span>
  );
}

export function Panel({ title, subtitle, children, loading, error, actions }) {
  return (
    <section className="panel">
      <header className="panel__header">
        <div>
          <h3>{title}</h3>
          {subtitle ? <p>{subtitle}</p> : null}
        </div>
        {actions ? <div className="panel__actions">{actions}</div> : null}
      </header>
      {loading ? <LoadingState /> : null}
      {error ? <ErrorState error={error} /> : null}
      {!loading && !error ? <div className="panel__body">{children}</div> : null}
    </section>
  );
}

export function TrendChart({
  series,
  yFormatter = formatNumber,
  rightYFormatter = formatNumber,
  yMax = null,
  rightYMax = null,
  bucketAnnotations = null,
  height = 280,
  compactY = false,
  stackBars = false,
  yTickMode = "default",
  axisLabelSize = 11,
  bottomLabelSize = 11,
  annotationLabelSize = 10,
  barGroupWidthFactor = 0.66,
  barMaxWidth = 46,
  leftPadding = 52,
}) {
  if (!series?.length) {
    return <EmptyState message="No chart data for the current filters." />;
  }

  const labels = Array.from(
    new Set(series.flatMap((item) => item.points.map((point) => point[0]))),
  ).sort();
  if (!labels.length) {
    return <EmptyState message="No chart data for the current filters." />;
  }

  const hasRightAxis = series.some((item) => item.axis === "right");
  const pointMaps = new Map(
    series.map((item) => [
      item.key,
      new Map(item.points.map((point) => [point[0], Number(point[1] || 0)])),
    ]),
  );
  const leftSeries = series.filter((item) => item.axis !== "right");
  const rightSeries = series.filter((item) => item.axis === "right");
  const leftLineSeries = leftSeries.filter((item) => item.type !== "bar");
  const leftBarSeries = leftSeries.filter((item) => item.type === "bar");
  const leftLineValues = leftLineSeries.flatMap((item) =>
    labels.map((label) => pointMaps.get(item.key)?.get(label) ?? 0),
  );
  const leftBarValues = stackBars
    ? labels.map((label) =>
        leftBarSeries.reduce(
          (sum, item) => sum + (pointMaps.get(item.key)?.get(label) ?? 0),
          0,
        ),
      )
    : leftBarSeries.flatMap((item) =>
        labels.map((label) => pointMaps.get(item.key)?.get(label) ?? 0),
      );
  const leftValues = [...leftLineValues, ...leftBarValues];
  const rightValues = rightSeries.flatMap((item) =>
    labels.map((label) => pointMaps.get(item.key)?.get(label) ?? 0),
  );
  const rawLeftMaxValue = yMax ?? Math.max(...leftValues, 1);
  let leftMaxValue = rawLeftMaxValue;
  let leftTickValues = [0, leftMaxValue * 0.25, leftMaxValue * 0.5, leftMaxValue * 0.75, leftMaxValue];
  if (yTickMode === "thousands-rounded") {
    const segments = 4;
    const rawStep = rawLeftMaxValue / segments;
    const step = Math.max(1000, Math.round(rawStep / 1000) * 1000);
    leftMaxValue = step * segments;
    leftTickValues = Array.from({ length: segments + 1 }, (_, index) => index * step);
  }
  const resolvedRightYMax = rightYMax ?? Math.max(...rightValues, 1);
  const maxBottomLabels = 8;
  const bottomLabelStep =
    labels.length > maxBottomLabels
      ? Math.ceil((labels.length - 1) / (maxBottomLabels - 1))
      : 1;
  const width = 760;
  const annotationMap = new Map(
    (bucketAnnotations || []).map((annotation) => [annotation.label, annotation.text]),
  );
  const padding = {
    top: annotationMap.size ? 34 : compactY ? 6 : 20,
    right: hasRightAxis ? 58 : 20,
    bottom: compactY ? 22 : 42,
    left: leftPadding,
  };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const xStep = labels.length > 1 ? plotWidth / (labels.length - 1) : plotWidth;
  const barSeries = series.filter((item) => item.type === "bar");
  const lineSeries = series.filter((item) => item.type === "line");

  return (
    <div className="trend-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Trend chart">
        {leftTickValues.map((value) => {
          const ratio = leftMaxValue > 0 ? value / leftMaxValue : 0;
          const y = padding.top + plotHeight - plotHeight * ratio;
          return (
            <g key={value}>
              <line
                x1={padding.left}
                x2={padding.left + plotWidth}
                y1={y}
                y2={y}
                className="chart-grid"
              />
              <text
                x={padding.left - 8}
                y={y + 4}
                textAnchor="end"
                className="chart-axis-label"
                style={{ fontSize: `${axisLabelSize}px` }}
              >
                {yFormatter(value)}
              </text>
              {hasRightAxis ? (
                <text
                  x={width - 8}
                  y={y + 4}
                  textAnchor="end"
                  className="chart-axis-label"
                  style={{ fontSize: `${axisLabelSize}px` }}
                >
                  {rightYFormatter(resolvedRightYMax * ratio)}
                </text>
              ) : null}
            </g>
          );
        })}

        {barSeries.map((item, seriesIndex) =>
          labels.map((label, index) => {
            const value = pointMaps.get(item.key)?.get(label) ?? 0;
            const groupWidth = Math.min(barMaxWidth, xStep * barGroupWidthFactor || barMaxWidth);
            const stackedSeries = stackBars
              ? barSeries.filter((candidate) => (candidate.axis || "left") === (item.axis || "left"))
              : [];
            const axisSeriesIndex = stackBars
              ? stackedSeries.findIndex((candidate) => candidate.key === item.key)
              : seriesIndex;
            const barWidth = stackBars
              ? Math.max(groupWidth - 4, 10)
              : Math.max(groupWidth / Math.max(barSeries.length, 1) - 6, 10);
            const groupStart = padding.left + index * xStep - groupWidth / 2;
            const x = stackBars
              ? padding.left + index * xStep - barWidth / 2
              : groupStart + seriesIndex * (barWidth + 6);
            const axisMax = item.axis === "right" ? resolvedRightYMax : leftMaxValue;
            const baseValue = stackBars
              ? stackedSeries
                  .slice(0, Math.max(axisSeriesIndex, 0))
                  .reduce((sum, candidate) => sum + (pointMaps.get(candidate.key)?.get(label) ?? 0), 0)
              : 0;
            const barHeight = axisMax > 0 ? (value / axisMax) * plotHeight : 0;
            const y = stackBars
              ? padding.top + plotHeight - ((baseValue + value) / axisMax) * plotHeight
              : padding.top + plotHeight - barHeight;
            return (
              <rect
                key={`${item.key}-${label}`}
                x={x}
                y={y}
                width={barWidth}
                height={barHeight}
                rx={stackBars ? 2 : 6}
                fill={seriesColor(item.key)}
                opacity="0.78"
              />
            );
          }),
        )}

        {lineSeries.map((item) => {
          const axisMax = item.axis === "right" ? resolvedRightYMax : leftMaxValue;
          const points = labels
            .map((label, index) => {
              const value = pointMaps.get(item.key)?.get(label) ?? 0;
              const x = padding.left + index * xStep;
              const y = padding.top + plotHeight - (value / axisMax) * plotHeight;
              return `${x},${y}`;
            })
            .join(" ");
          return (
            <g key={item.key}>
              <polyline
                points={points}
                fill="none"
                stroke={seriesColor(item.key)}
                strokeWidth="3"
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {labels.map((label, index) => {
                const value = pointMaps.get(item.key)?.get(label) ?? 0;
                const x = padding.left + index * xStep;
                const y = padding.top + plotHeight - (value / axisMax) * plotHeight;
                return (
                  <circle
                    key={`${item.key}-${label}`}
                    cx={x}
                    cy={y}
                    r="4.5"
                    fill={seriesColor(item.key)}
                    stroke="#fcf7ef"
                    strokeWidth="2"
                  />
                );
              })}
            </g>
          );
        })}

        {labels.map((label, index) => {
          const annotation = annotationMap.get(label);
          if (!annotation) {
            return null;
          }

          const annotationY = getAnnotationY({
            label,
            series,
            pointMaps,
            leftMaxValue,
            resolvedRightYMax,
            padding,
            plotHeight,
          });
          const x = padding.left + index * xStep;
          return (
            <text
              key={`${label}-annotation`}
              x={x}
              y={annotationY}
              className="chart-axis-label chart-axis-label--annotation"
              style={{ fontSize: `${annotationLabelSize}px` }}
            >
              {annotation}
            </text>
          );
        })}

        {labels.map((label, index) => {
          const isFirstLabel = index === 0;
          const isLastLabel = index === labels.length - 1;
          const shouldShowLabel = isLastLabel || index % bottomLabelStep === 0;
          if (!shouldShowLabel) {
            return null;
          }
          const x = padding.left + index * xStep;
          const textAnchor = isLastLabel ? "end" : isFirstLabel ? "start" : "middle";
          return (
            <text
              key={label}
              x={x}
              y={height - 14}
              textAnchor={textAnchor}
              className="chart-axis-label chart-axis-label--bottom"
              style={{ fontSize: `${bottomLabelSize}px` }}
            >
              {label}
            </text>
          );
        })}
      </svg>

      <div className="chart-legend">
        {series.map((item) => (
          <div key={item.key} className="chart-legend__item">
            <span
              className="chart-legend__swatch"
              style={{ backgroundColor: seriesColor(item.key) }}
            />
            <span>{item.label || formatSeriesLabel(item.key)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function CloudComparisonPanel({ groups }) {
  if (!groups?.length) {
    return <EmptyState message="No cloud comparison data yet." />;
  }

  return (
    <div className="comparison-grid">
      {groups.map((group) => (
        <article key={group.name} className="comparison-card">
          <header>
            <span className="comparison-card__eyebrow">{group.name}</span>
            <strong>{formatPercent(group.metrics.success_rate_pct)}</strong>
            <p>success rate</p>
          </header>
          <div className="comparison-card__bar">
            <span
              style={{ width: `${Math.min(group.metrics.success_rate_pct, 100)}%` }}
            />
          </div>
          <dl className="comparison-metrics">
            <div>
              <dt>Builds</dt>
              <dd>{formatCompact(group.metrics.total_builds)}</dd>
            </div>
            <div>
              <dt>Queue</dt>
              <dd>{formatSeconds(group.metrics.queue_avg_s)}</dd>
            </div>
            <div>
              <dt>Run</dt>
              <dd>{formatSeconds(group.metrics.run_avg_s)}</dd>
            </div>
            <div>
              <dt>Total</dt>
              <dd>{formatSeconds(group.metrics.total_avg_s)}</dd>
            </div>
          </dl>
        </article>
      ))}
    </div>
  );
}

export function ShareBars({ categories, groups }) {
  if (!groups?.length) {
    return <EmptyState message="No category share data yet." />;
  }

  return (
    <div className="share-bars">
      {groups.map((group) => {
        const total = group.values.reduce((sum, value) => sum + value, 0) || 1;
        return (
          <article key={group.name} className="share-bars__group">
            <div className="share-bars__header">
              <strong>{group.name}</strong>
              <span>{formatNumber(total)} failure-like builds</span>
            </div>
            <div className="share-bars__track">
              {group.values.map((value, index) => (
                <span
                  key={`${group.name}-${categories[index]}`}
                  title={`${categories[index]}: ${value}`}
                  style={{
                    width: `${(value / total) * 100}%`,
                    backgroundColor: seriesColor(categories[index]),
                  }}
                />
              ))}
            </div>
            <div className="share-bars__legend">
              {categories.map((category, index) => (
                <div key={`${group.name}-${category}`} className="share-bars__legend-item">
                  <span
                    className="chart-legend__swatch"
                    style={{ backgroundColor: seriesColor(category) }}
                  />
                  <span>
                    {category}: {formatNumber(group.values[index])}
                  </span>
                </div>
              ))}
            </div>
          </article>
        );
      })}
    </div>
  );
}

export function RankingList({
  items,
  valueKey = "value",
  valueFormatter = formatNumber,
  renderMeta = null,
}) {
  if (!items?.length) {
    return <EmptyState message="No ranking data for the current scope." />;
  }

  const maxValue = Math.max(...items.map((item) => Number(item[valueKey] || 0)), 1);

  return (
    <div className="ranking-list">
      {items.map((item, index) => {
        const value = Number(item[valueKey] || 0);
        const metaContent = renderMeta ? renderMeta(item) : buildDefaultRankingMeta(item);
        return (
          <article key={item.name} className="ranking-list__item">
            <div className="ranking-list__header">
              <span className="ranking-list__rank">{String(index + 1).padStart(2, "0")}</span>
              <div className="ranking-list__title">
                {item.job_url ? (
                  <a
                    href={item.job_url}
                    target="_blank"
                    rel="noreferrer"
                    className="ranking-list__link"
                    title={item.job_url}
                  >
                    {item.name}
                  </a>
                ) : (
                  <strong>{item.name}</strong>
                )}
                <span>{valueFormatter(value)}</span>
              </div>
            </div>
            <div className="ranking-list__bar">
              <span style={{ width: `${(value / maxValue) * 100}%` }} />
            </div>
            {metaContent ? <div className="ranking-list__meta">{metaContent}</div> : null}
          </article>
        );
      })}
    </div>
  );
}

function buildDefaultRankingMeta(item) {
  const meta = [];
  if ("noisy_rate_pct" in item) {
    meta.push(<span key="noisy-rate">Noisy rate {formatPercent(item.noisy_rate_pct)}</span>);
  }
  if ("noisy_build_count" in item && "failure_like_build_count" in item) {
    meta.push(
      <span key="noisy-build-count">
        {formatNumber(item.noisy_build_count)} noisy / {formatNumber(item.failure_like_build_count)} failure-like builds
      </span>,
    );
  } else if ("failure_like_build_count" in item) {
    meta.push(
      <span key="failure-like-build-count">
        {formatNumber(item.failure_like_build_count)} failure-like builds
      </span>,
    );
  }
  return meta.length ? meta : null;
}

export function RuntimeComparisonBoard({
  improved,
  regressed,
  windowDays,
  minSuccessRuns,
}) {
  if (!improved?.length && !regressed?.length) {
    return <EmptyState message="No migration runtime comparison jobs matched the current scope." />;
  }

  const allItems = [...(improved || []), ...(regressed || [])];
  const maxRunSeconds = Math.max(
    ...allItems.flatMap((item) => [item.idc_baseline_avg_run_s, item.gcp_recent_avg_run_s]),
    1,
  );

  return (
    <div className="runtime-compare-grid">
      <RuntimeChangeList
        title="Top 10 improved jobs"
        subtitle={`${windowDays}d IDC baseline before first GCP success vs latest ${windowDays}d GCP. Min ${minSuccessRuns} success runs each side.`}
        tone="improved"
        items={improved}
        maxRunSeconds={maxRunSeconds}
        emptyMessage="No improved jobs met the migration comparison threshold."
      />
      <RuntimeChangeList
        title="Top 10 regressed jobs"
        subtitle={`${windowDays}d IDC baseline before first GCP success vs latest ${windowDays}d GCP. Min ${minSuccessRuns} success runs each side.`}
        tone="regressed"
        items={regressed}
        maxRunSeconds={maxRunSeconds}
        emptyMessage="No regressed jobs met the migration comparison threshold."
      />
    </div>
  );
}

export function DonutShareChart({
  title,
  subtitle,
  items,
  totalLabel = "builds",
  emptyMessage = "No share data for the current filters.",
  onItemSelect,
}) {
  if (!items?.length) {
    return <EmptyState message={emptyMessage} compact />;
  }

  const total = items.reduce((sum, item) => sum + Number(item.value || 0), 0);
  if (!total) {
    return <EmptyState message={emptyMessage} compact />;
  }

  const radius = 78;
  const innerRadius = 43;
  const size = 220;
  const center = size / 2;
  let startAngle = -Math.PI / 2;

  return (
    <article className="donut-card">
      <header className="donut-card__header">
        <div>
          <strong>{title}</strong>
          {subtitle ? <p>{subtitle}</p> : null}
        </div>
        <span className="panel-badge">
          <strong>{formatCompact(total)}</strong>
          {totalLabel}
        </span>
      </header>

      <div className="donut-card__body">
        <div className="donut-chart">
          <svg viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`${title} share chart`}>
            {items.map((item, index) => {
              const value = Number(item.value || 0);
              const angle = (value / total) * Math.PI * 2;
              const endAngle = startAngle + angle;
              const fill = donutColor(index);
              const path = describeDonutArc(center, center, innerRadius, radius, startAngle, endAngle);
              const percent = Number(item.share_pct || 0);
              const key = `${title}-${item.name}`;
              const interactive = typeof onItemSelect === "function" && item.interactive !== false;
              const element = (
                <path
                  d={path}
                  fill={fill}
                  className={interactive ? "donut-chart__segment donut-chart__segment--interactive" : "donut-chart__segment"}
                  role={interactive ? "button" : undefined}
                  tabIndex={interactive ? 0 : undefined}
                  aria-label={`${item.name}: ${formatCompact(value)} ${totalLabel}, ${formatPercent(percent)}`}
                  onClick={interactive ? () => onItemSelect(item) : undefined}
                  onKeyDown={
                    interactive
                      ? (event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            onItemSelect(item);
                          }
                        }
                      : undefined
                  }
                />
              );
              startAngle = endAngle;
              return <g key={key}>{element}</g>;
            })}
            <circle cx={center} cy={center} r={innerRadius - 3} fill="#fcf7ef" />
            <text x={center} y={center - 6} textAnchor="middle" className="donut-chart__center-value">
              {formatCompact(total)}
            </text>
            <text x={center} y={center + 16} textAnchor="middle" className="donut-chart__center-label">
              {totalLabel}
            </text>
          </svg>
        </div>

        <div className="donut-legend">
          {items.map((item, index) => {
            const interactive = typeof onItemSelect === "function" && item.interactive !== false;
            if (interactive) {
              return (
                <button
                  key={`${title}-${item.name}-legend`}
                  type="button"
                  className="donut-legend__item"
                  onClick={() => onItemSelect(item)}
                >
                  <span className="chart-legend__swatch" style={{ backgroundColor: donutColor(index) }} />
                  <span className="donut-legend__name">{item.name}</span>
                  <span className="donut-legend__value">{formatCompact(item.value)}</span>
                  <span className="donut-legend__share">{formatPercent(item.share_pct)}</span>
                </button>
              );
            }

            return (
              <div
                key={`${title}-${item.name}-legend`}
                className="donut-legend__item donut-legend__item--static"
              >
                <span className="chart-legend__swatch" style={{ backgroundColor: donutColor(index) }} />
                <span className="donut-legend__name">{item.name}</span>
                <span className="donut-legend__value">{formatCompact(item.value)}</span>
                <span className="donut-legend__share">{formatPercent(item.share_pct)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </article>
  );
}

export function DrilldownModal({ title, subtitle, children, onClose }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-card__header">
          <div>
            <h3>{title}</h3>
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          <button type="button" className="ghost-button ghost-button--compact" onClick={onClose}>
            Close
          </button>
        </header>
        <div className="modal-card__body">{children}</div>
      </section>
    </div>
  );
}

export function FreshnessStrip({ jobs, generatedAt }) {
  if (!jobs?.length) {
    return <EmptyState message="No freshness records available yet." compact />;
  }

  return (
    <div className="freshness-strip">
      <div className="freshness-strip__header">
        <span>Freshness</span>
        <strong>{generatedAt}</strong>
      </div>
      <div className="freshness-strip__rows">
        {jobs.map((job) => (
          <article key={job.job_name} className="freshness-chip">
            <div>
              <strong>{job.job_name}</strong>
              <span>{job.last_status}</span>
            </div>
            <span className="freshness-chip__lag">
              {job.lag_minutes == null ? "n/a" : `${job.lag_minutes}m lag`}
            </span>
          </article>
        ))}
      </div>
    </div>
  );
}

export function PeriodComparisonTable({ groups, meta }) {
  if (!groups?.length) {
    return <EmptyState message="No period comparison data yet." />;
  }

  return (
    <div className="period-grid">
      {groups.map((group) => (
        <article key={group.name} className="period-card">
          <header>
            <span>{group.name === "period_a" ? "Current window" : "Previous window"}</span>
            <p className="period-card__range">
              {group.name === "period_a"
                ? formatDateRangeLabel(meta?.period_a_start, meta?.period_a_end)
                : formatDateRangeLabel(meta?.period_b_start, meta?.period_b_end)}
            </p>
            <strong>{formatPercent(group.values.noisy_rate_pct)}</strong>
            <p>noisy rate</p>
          </header>
          <dl>
            <div>
              <dt>Total builds</dt>
              <dd>{formatCompact(group.values.total_build_count)}</dd>
            </div>
            <div>
              <dt>Failure-like</dt>
              <dd>{formatCompact(group.values.failure_like_build_count)}</dd>
            </div>
            <div>
              <dt>Flaky</dt>
              <dd>{formatCompact(group.values.flaky_build_count)}</dd>
            </div>
            <div>
              <dt>
                Blind-retry-loop
                <InfoHint text={BLIND_RETRY_LOOP_HINT} />
              </dt>
              <dd>{formatCompact(group.values.retry_loop_build_count)}</dd>
            </div>
          </dl>
        </article>
      ))}
    </div>
  );
}

export function DistinctCaseCountTable({ weeks, rows, scrollClassName = "" }) {
  if (!rows?.length) {
    return <EmptyState message="No distinct flaky case counts for the current scope." />;
  }

  return (
    <div className={`table-scroll ${scrollClassName}`.trim()}>
      <table className="data-table data-table--compact">
        <thead>
          <tr>
            <th>Branch</th>
            {weeks.map((week) => (
              <th key={week}>{week}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.branch}>
              <th scope="row">{row.branch}</th>
              {row.values.map((value, index) => (
                <td key={`${row.branch}-${weeks[index]}`}>{formatNumber(value)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function IssueWeeklyRateTable({ weeks, rows, scrollClassName = "" }) {
  if (!rows?.length) {
    return <EmptyState message="No flaky issues matched the current repo, branch, and date window." />;
  }

  const highlightStartIndex = Math.max(weeks.length - 2, 0);

  return (
    <div className={`table-scroll ${scrollClassName}`.trim()}>
      <table className="data-table">
        <thead>
          <tr>
            <th>Case name</th>
            {weeks.map((week) => (
              <th key={week}>{week}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const closeTimeLabel = row.issue_closed_at ? formatUtcCloseTime(row.issue_closed_at) : null;
            return (
            <tr key={`${row.issue_number}-${row.case_name}`}>
              <th scope="row">
                <div className="issue-cell">
                  <a href={row.issue_url} target="_blank" rel="noreferrer">
                    {row.display_name}
                  </a>
                  <div className="issue-cell__meta">
                    <span className={`status-pill status-pill--${String(row.issue_status).toLowerCase()}`}>
                      {row.issue_status}
                    </span>
                    {row.issue_branch ? <span>{row.issue_branch}</span> : null}
                    {closeTimeLabel ? <span>{closeTimeLabel}</span> : null}
                  </div>
                </div>
              </th>
              {row.metrics.map((metric, index) => {
                const isRecentWeek = index >= highlightStartIndex;
                const recentTone = metric.flaky_rate_pct > 0 ? "hot" : "cool";
                return (
                  <td
                    key={`${row.issue_number}-${weeks[index]}`}
                    className={isRecentWeek ? `metric-cell metric-cell--${recentTone}` : undefined}
                  >
                    {metric.cell}
                  </td>
                );
              })}
            </tr>
          )})}
        </tbody>
      </table>
    </div>
  );
}

export function EmptyState({ message, compact = false }) {
  return <div className={compact ? "empty-state empty-state--compact" : "empty-state"}>{message}</div>;
}

function LoadingState() {
  return (
    <div className="empty-state">
      <span className="loading-dot" />
      Loading chart data...
    </div>
  );
}

function ErrorState({ error }) {
  return <div className="empty-state empty-state--error">Could not load panel: {error}</div>;
}

function seriesColor(key) {
  return SERIES_COLORS[key] || "#315772";
}

function donutColor(index) {
  return DONUT_COLORS[index % DONUT_COLORS.length];
}

function formatSeriesLabel(key) {
  return key
    .replaceAll("_pct", " %")
    .replaceAll("_s", " (s)")
    .replaceAll("_", " ")
    .replaceAll("retry loop", "Blind-retry-loop")
    .replaceAll("FLAKY TEST", "Flaky test")
    .replaceAll("UNCLASSIFIED", "Unclassified");
}

function formatUtcCloseTime(isoValue) {
  const date = new Date(isoValue);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  const yyyy = date.getUTCFullYear();
  const mm = String(date.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(date.getUTCDate()).padStart(2, "0");
  const hh = String(date.getUTCHours()).padStart(2, "0");
  const min = String(date.getUTCMinutes()).padStart(2, "0");
  return `Closed: ${yyyy}-${mm}-${dd} ${hh}:${min} UTC`;
}

function describeDonutArc(cx, cy, innerRadius, outerRadius, startAngle, endAngle) {
  const outerStart = polarToCartesian(cx, cy, outerRadius, endAngle);
  const outerEnd = polarToCartesian(cx, cy, outerRadius, startAngle);
  const innerStart = polarToCartesian(cx, cy, innerRadius, startAngle);
  const innerEnd = polarToCartesian(cx, cy, innerRadius, endAngle);
  const largeArcFlag = endAngle - startAngle <= Math.PI ? "0" : "1";

  return [
    `M ${outerStart.x} ${outerStart.y}`,
    `A ${outerRadius} ${outerRadius} 0 ${largeArcFlag} 0 ${outerEnd.x} ${outerEnd.y}`,
    `L ${innerStart.x} ${innerStart.y}`,
    `A ${innerRadius} ${innerRadius} 0 ${largeArcFlag} 1 ${innerEnd.x} ${innerEnd.y}`,
    "Z",
  ].join(" ");
}

function polarToCartesian(cx, cy, radius, angleInRadians) {
  return {
    x: cx + radius * Math.cos(angleInRadians),
    y: cy + radius * Math.sin(angleInRadians),
  };
}

function RuntimeChangeList({
  title,
  subtitle,
  tone,
  items,
  maxRunSeconds,
  emptyMessage,
}) {
  return (
    <section className={`runtime-compare-card runtime-compare-card--${tone}`}>
      <header className="runtime-compare-card__header">
        <div>
          <strong>{title}</strong>
          <p>{subtitle}</p>
        </div>
        <span className="panel-badge">
          <strong>{items?.length || 0}</strong>
          jobs
        </span>
      </header>

      <div className="runtime-compare-legend">
        <span className="runtime-compare-legend__item">
          <span className="runtime-compare-track__dot runtime-compare-track__dot--baseline runtime-compare-legend__dot" />
          IDC baseline
        </span>
        <span className="runtime-compare-legend__item">
          <span className={`runtime-compare-track__dot runtime-compare-track__dot--${tone} runtime-compare-legend__dot`} />
          GCP recent
        </span>
        <span className={`runtime-compare-legend__swatch runtime-compare-legend__swatch--${tone}`} />
        <span className="runtime-compare-legend__caption">
          Colored segment = delta between the two averages. Left is shorter runtime, right is longer.
        </span>
      </div>

      {!items?.length ? (
        <EmptyState message={emptyMessage} compact />
      ) : (
        <div className="runtime-compare-list">
          {items.map((item, index) => (
            <article
              key={`${tone}-${item.normalized_job_path}`}
              className="runtime-compare-item"
            >
              <div className="runtime-compare-item__header">
                <span className={`runtime-compare-item__rank runtime-compare-item__rank--${tone}`}>
                  {String(index + 1).padStart(2, "0")}
                </span>
                <div className="runtime-compare-item__title">
                  <strong>{item.job_name}</strong>
                  <span className="runtime-compare-item__path">{item.normalized_job_path}</span>
                </div>
                <span className={`runtime-compare-item__delta runtime-compare-item__delta--${tone}`}>
                  {formatSignedDuration(item.delta_run_s)} ({formatSignedPercent(item.delta_pct)})
                </span>
              </div>

              <div className="runtime-compare-track">
                <span className="runtime-compare-track__axis" />
                <span
                  className={`runtime-compare-track__connector runtime-compare-track__connector--${tone}`}
                  style={buildConnectorStyle(item, maxRunSeconds)}
                />
                <span
                  className="runtime-compare-track__dot runtime-compare-track__dot--baseline"
                  style={{ left: `${ratioPct(item.idc_baseline_avg_run_s, maxRunSeconds)}%` }}
                  title={`IDC baseline ${formatSeconds(item.idc_baseline_avg_run_s)}`}
                />
                <span
                  className={`runtime-compare-track__dot runtime-compare-track__dot--${tone}`}
                  style={{ left: `${ratioPct(item.gcp_recent_avg_run_s, maxRunSeconds)}%` }}
                  title={`GCP recent ${formatSeconds(item.gcp_recent_avg_run_s)}`}
                />
              </div>

              <div className="runtime-compare-item__meta">
                <span>
                  IDC {formatSeconds(item.idc_baseline_avg_run_s)} ({item.idc_success_count})
                </span>
                <span>
                  GCP {formatSeconds(item.gcp_recent_avg_run_s)} ({item.gcp_success_count})
                </span>
                <span>First GCP {formatShortDate(item.first_gcp_success_at)}</span>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function ratioPct(value, maxValue) {
  if (!maxValue) {
    return 0;
  }
  return (Number(value || 0) / Number(maxValue)) * 100;
}

function buildConnectorStyle(item, maxRunSeconds) {
  const start = ratioPct(item.idc_baseline_avg_run_s, maxRunSeconds);
  const end = ratioPct(item.gcp_recent_avg_run_s, maxRunSeconds);
  return {
    left: `${Math.min(start, end)}%`,
    width: `${Math.max(Math.abs(end - start), 0.8)}%`,
  };
}

function formatSignedDuration(seconds) {
  const numeric = Number(seconds || 0);
  const prefix = numeric > 0 ? "+" : "";
  return `${prefix}${formatSeconds(Math.abs(numeric))}`.replace(/^/, numeric < 0 ? "-" : "");
}

function formatSignedPercent(value) {
  const numeric = Number(value || 0);
  const prefix = numeric > 0 ? "+" : "";
  return `${prefix}${numeric.toFixed(1)}%`;
}

function formatShortDate(value) {
  if (!value) {
    return "n/a";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "n/a";
  }
  return date.toISOString().slice(0, 10);
}

function getAnnotationY({
  label,
  series,
  pointMaps,
  leftMaxValue,
  resolvedRightYMax,
  padding,
  plotHeight,
}) {
  const ys = series.map((item) => {
    const value = pointMaps.get(item.key)?.get(label) ?? 0;
    const axisMax = item.axis === "right" ? resolvedRightYMax : leftMaxValue;
    return padding.top + plotHeight - (value / axisMax) * plotHeight;
  });

  const topMostPoint = Math.min(...ys);
  return Math.max(padding.top - 10, topMostPoint - 8);
}
