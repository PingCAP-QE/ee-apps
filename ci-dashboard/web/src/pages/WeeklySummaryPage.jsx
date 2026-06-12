import { useEffect, useState } from "react";

import {
  formatCompact,
  formatDelta,
  formatNumber,
  formatPercent,
  formatSeconds,
  getLatestBranchValue,
  getPreviousCompleteMondayWeek,
  getPreviousDateRange,
  getStableCostSummaryWeek,
  useApiData,
} from "../lib/api";
import {
  DonutShareChart,
  PageIntro,
  Panel,
  StatCard,
} from "../components/charts";

export default function WeeklySummaryPage() {
  const [summaryRange] = useState(() => getPreviousCompleteMondayWeek());
  const [previousRange] = useState(() =>
    getPreviousDateRange(summaryRange.start_date, summaryRange.end_date),
  );
  const [costRange] = useState(() => getStableCostSummaryWeek());
  const [selectedErrorCatalog, setSelectedErrorCatalog] = useState("INFRA");
  const currentFilters = { ...summaryRange, granularity: "week" };
  const previousFilters = { ...previousRange, granularity: "week" };
  const current = useApiData("/api/v1/pages/ci-status", currentFilters);
  const previous = useApiData("/api/v1/pages/ci-status", previousFilters);
  const flakyFilters = {
    ...currentFilters,
    repo: "pingcap/tidb",
    issue_status: "closed",
  };
  const previousFlakyFilters = {
    ...previousFilters,
    repo: "pingcap/tidb",
    issue_status: "closed",
  };
  const flaky = useApiData("/api/v1/pages/flaky", flakyFilters);
  const previousFlaky = useApiData("/api/v1/pages/flaky", previousFlakyFilters);
  const cost = useApiData(
    "/api/v1/pages/cost-weekly-account-summaries",
    { ...costRange, granularity: "week" },
  );

  const currentMetrics = buildCiMetrics(current.data);
  const previousMetrics = buildCiMetrics(previous.data);
  const hasCiComparison =
    !current.loading && !previous.loading && !current.error && !previous.error;
  const currentMigration = current.data?.cloud_migration_summary || {};
  const previousMigration = previous.data?.cloud_migration_summary || {};
  const errorCatalogShare = current.data?.error_catalog_share || {};
  const errorCatalogItems = errorCatalogShare.items || [];
  const errorDetailsItems = buildErrorDetailsShareItems(
    errorCatalogShare.l2_details,
    selectedErrorCatalog,
  );
  const errorDetailsTitle = selectedErrorCatalog
    ? `${formatErrorCatalogName(selectedErrorCatalog)} Error Details`
    : "Top Error Details";
  const flakyCurrentPeriod = getPeriodValues(flaky.data, "period_a");
  const flakyPreviousPeriod = getPeriodValues(flaky.data, "period_b");
  const hasFlakyComparison =
    !flaky.loading
    && !flaky.error
    && hasPeriodValues(flaky.data, "period_a")
    && hasPeriodValues(flaky.data, "period_b");
  const distinctFlakyCases = getLatestBranchValue(
    flaky.data?.distinct_flaky_case_counts?.rows,
    "master",
  );
  const previousDistinctFlakyCases = getLatestBranchValue(
    previousFlaky.data?.distinct_flaky_case_counts?.rows,
    "master",
  );
  const hasDistinctComparison =
    !flaky.loading
    && !previousFlaky.loading
    && !flaky.error
    && !previousFlaky.error;
  const issueFixProgress = flaky.data?.issue_fix_progress || {};
  const costItems = prioritizeCostAccounts(cost.data?.items);

  useEffect(() => {
    if (current.loading || current.error) {
      return;
    }
    if (!selectedErrorCatalog) {
      return;
    }
    if (!errorCatalogItems.some((item) => item.name === selectedErrorCatalog)) {
      setSelectedErrorCatalog("");
    }
  }, [current.error, current.loading, errorCatalogItems, selectedErrorCatalog]);

  return (
    <div className="page-stack weekly-summary">
      <PageIntro
        eyebrow="Weekly Summary"
        title="The last complete CI week, ready for review"
        description="This page is fixed to the most recent completed natural week, Monday through Sunday. It has no interactive filters so every reader sees the same reporting window."
        kicker={formatSummaryRange(summaryRange)}
      />

      <section className="weekly-summary__section">
        <header className="weekly-summary__section-header">
          <span>CI</span>
          <div>
            <h3>CI status</h3>
          </div>
        </header>

        <section className="stats-grid">
          <StatCard
            label="Total builds"
            value={formatCompact(currentMetrics.totalBuilds)}
            detail={`${formatCompact(currentMetrics.successBuilds)} success · ${formatCompact(currentMetrics.failureBuilds)} failure-like`}
            delta={
              hasCiComparison
                ? formatRelativePercentDelta(
                    currentMetrics.totalBuilds,
                    previousMetrics.totalBuilds,
                  )
                : null
            }
            deltaTone={lowerIsBetterDeltaTone(
              currentMetrics.totalBuilds,
              previousMetrics.totalBuilds,
            )}
          />
          <StatCard
            label="Success rate"
            value={formatPercent(currentMetrics.successRate)}
            detail="Compared with the previous complete week"
            tone="teal"
            delta={
              hasCiComparison
                ? formatDelta(
                    currentMetrics.successRate,
                    previousMetrics.successRate,
                    "%",
                  )
                : null
            }
            deltaTone={higherIsBetterDeltaTone(
              currentMetrics.successRate,
              previousMetrics.successRate,
            )}
          />
          <StatCard
            label="Success avg queue wait"
            value={formatSeconds(currentMetrics.avgQueue)}
            detail={`Success avg run time ${formatSeconds(currentMetrics.avgRun)}`}
            tone="amber"
            delta={
              hasCiComparison
                ? formatDelta(
                    currentMetrics.avgQueue,
                    previousMetrics.avgQueue,
                    "s",
                  )
                : null
            }
            deltaTone={lowerIsBetterDeltaTone(
              currentMetrics.avgQueue,
              previousMetrics.avgQueue,
            )}
          />
          <StatCard
            label="Success avg total duration"
            value={formatSeconds(currentMetrics.avgTotal)}
            detail="Simple average across successful builds"
            delta={
              hasCiComparison
                ? formatRelativePercentDelta(
                    currentMetrics.avgTotal,
                    previousMetrics.avgTotal,
                  )
                : null
            }
            deltaTone={lowerIsBetterDeltaTone(
              currentMetrics.avgTotal,
              previousMetrics.avgTotal,
            )}
          />
          <GcpMigrationCard
            current={currentMigration}
            previous={previousMigration}
            hasComparison={hasCiComparison}
          />
        </section>

        <div className="page-grid page-grid--two-column">
          <Panel
            title="Jenkins Error Catalog Rate"
            loading={current.loading}
            error={current.error}
          >
            <DonutShareChart
              title="Jenkins Error Catalog"
              items={errorCatalogItems}
              totalLabel="failures"
              emptyMessage="No Jenkins Error Catalog data for the summary week."
              onItemSelect={(item) => setSelectedErrorCatalog(item.name)}
            />
          </Panel>

          <Panel
            title="Jenkins Error Details Rate"
            loading={current.loading}
            error={current.error}
            actions={
              selectedErrorCatalog ? (
                <button
                  type="button"
                  className="ghost-button ghost-button--compact"
                  onClick={() => setSelectedErrorCatalog("")}
                >
                  All Catalogs
                </button>
              ) : null
            }
          >
            <DonutShareChart
              title={errorDetailsTitle}
              items={errorDetailsItems}
              totalLabel="failures"
              emptyMessage="No Jenkins Error Details data for the summary week."
            />
          </Panel>
        </div>
      </section>

      <section className="weekly-summary__section">
        <header className="weekly-summary__section-header">
          <span>Cost</span>
          <div>
            <h3>Cloud account spend</h3>
            <p>
              Cost data has a 3-4 day reporting lag. This block uses stable data from{" "}
              <strong>{costRange.start_date}</strong> to{" "}
              <strong>{costRange.end_date}</strong>.
            </p>
          </div>
        </header>

        <section className="weekly-summary__cost-grid">
          {costItems.map((item) => (
            <CostAccountCard key={item.cost_source} item={item} />
          ))}
          {cost.loading ? (
            <div className="weekly-summary__section-state">Loading cost summary...</div>
          ) : null}
          {!cost.loading && cost.error ? (
            <div className="weekly-summary__section-state weekly-summary__section-state--error">
              Could not load cost summary.
            </div>
          ) : null}
        </section>
      </section>

      <section className="weekly-summary__section">
        <header className="weekly-summary__section-header">
          <span>Flaky</span>
        </header>

        <section className="weekly-summary__flaky-grid">
          <StatCard
            label="Combined Noisy Rate"
            value={formatPercent(flakyCurrentPeriod.noisy_rate_pct || 0)}
            detail={`${formatNumber(flakyCurrentPeriod.noisy_build_count || 0)} noisy / ${formatNumber(flakyCurrentPeriod.failure_like_build_count || 0)} failure-like builds`}
            tone="teal"
            delta={
              hasFlakyComparison
                ? formatDelta(
                    flakyCurrentPeriod.noisy_rate_pct,
                    flakyPreviousPeriod.noisy_rate_pct,
                    "%",
                  )
                : null
            }
            deltaTone={lowerIsBetterDeltaTone(
              flakyCurrentPeriod.noisy_rate_pct,
              flakyPreviousPeriod.noisy_rate_pct,
            )}
          />
          <StatCard
            label="Affected PR Rate"
            value={formatPercent(flakyCurrentPeriod.affected_pr_rate_pct || 0)}
            detail={`${formatNumber(flakyCurrentPeriod.affected_pr_count || 0)} / ${formatNumber(flakyCurrentPeriod.total_pr_count || 0)} PRs`}
            delta={
              hasFlakyComparison
                ? formatDelta(
                    flakyCurrentPeriod.affected_pr_rate_pct,
                    flakyPreviousPeriod.affected_pr_rate_pct,
                    "%",
                  )
                : null
            }
            deltaTone={lowerIsBetterDeltaTone(
              flakyCurrentPeriod.affected_pr_rate_pct,
              flakyPreviousPeriod.affected_pr_rate_pct,
            )}
          />
          <StatCard
            label="Distinct flaky cases"
            value={formatNumber(distinctFlakyCases)}
            delta={
              hasDistinctComparison
                ? formatProgressDelta(distinctFlakyCases - previousDistinctFlakyCases)
                : null
            }
            deltaTone={lowerIsBetterDeltaTone(
              distinctFlakyCases,
              previousDistinctFlakyCases,
            )}
          />

          <WeeklyProgressCard
            title="Flaky issue progress"
            tone="rose"
            loading={flaky.loading}
            error={flaky.error}
            items={[
              {
                label: "Filed",
                value: issueFixProgress.filed_issue_count,
                delta: issueFixProgress.filed_issue_delta,
              },
              {
                label: "Fixed",
                value: issueFixProgress.fixed_issue_count,
                delta: issueFixProgress.fixed_issue_delta,
                improvementDirection: "higher",
              },
            ]}
          />
          <WeeklyProgressCard
            title="Fix PR progress"
            tone="teal"
            loading={flaky.loading}
            error={flaky.error}
            items={[
              {
                label: "In review",
                value: issueFixProgress.in_review_pr_count,
                delta: issueFixProgress.in_review_pr_delta,
              },
              {
                label: "Merged",
                value: issueFixProgress.merged_pr_count,
                delta: issueFixProgress.merged_pr_delta,
                improvementDirection: "higher",
              },
            ]}
          />
        </section>
      </section>
    </div>
  );
}

function CostAccountCard({ item }) {
  const hasWeeklyBudget = item.weekly_budget !== null && item.weekly_budget !== undefined;
  const weeklyBudget = Number(item.weekly_budget || 0);
  const delta = Number(item.net_cost_wow_pct || 0);
  const accountLabel = `${String(item.vendor || "").toUpperCase()} · ${
    item.display_name || item.account_id
  }`;

  return (
    <article
      className={`stat-card cost-account-card${
        item.over_budget ? " cost-account-card--over-budget" : ""
      }`}
    >
      <span className="cost-account-card__account">{accountLabel}</span>
      <span className="stat-card__label">Net cost</span>
      <strong className="stat-card__value">{formatWeeklyCost(item.net_cost)}</strong>
      <div className="stat-card__meta">
        <span>
          {hasWeeklyBudget
            ? `Weekly budget ${formatWeeklyCost(weeklyBudget)}`
            : "Weekly budget not configured"}
        </span>
        <span
          className={`stat-card__delta stat-card__delta--${lowerIsBetterDeltaTone(delta, 0)}`}
        >
          {formatCostPercentDelta(delta)}
        </span>
      </div>
    </article>
  );
}

function GcpMigrationCard({ current, previous, hasComparison }) {
  const buildShare = Number(current.gcp_build_share_pct || 0);
  const previousBuildShare = Number(previous.gcp_build_share_pct || 0);
  const durationShare = Number(current.gcp_duration_share_pct || 0);
  const previousDurationShare = Number(previous.gcp_duration_share_pct || 0);

  return (
    <article className="stat-card stat-card--teal migration-rate-card">
      <span className="stat-card__label">GCP Migration rate</span>
      <div className="migration-rate-card__metrics">
        <div className="migration-rate-card__metric">
          <span>GCP job runs</span>
          <div>
            <strong>{formatPercent(buildShare)}</strong>
            <span
              className={`stat-card__delta stat-card__delta--${higherIsBetterDeltaTone(buildShare, previousBuildShare)}`}
            >
              {hasComparison ? formatDelta(buildShare, previousBuildShare, "%") : ""}
            </span>
          </div>
        </div>
        <div className="migration-rate-card__metric">
          <span>Total duration</span>
          <div>
            <strong>{formatPercent(durationShare)}</strong>
            <span
              className={`stat-card__delta stat-card__delta--${higherIsBetterDeltaTone(durationShare, previousDurationShare)}`}
            >
              {hasComparison ? formatDelta(durationShare, previousDurationShare, "%") : ""}
            </span>
          </div>
        </div>
      </div>
      <span className="migration-rate-card__detail">
        {formatCompact(current.gcp_build_count || 0)} of{" "}
        {formatCompact(current.total_build_count || 0)} GCP or IDC job runs
      </span>
    </article>
  );
}

function WeeklyProgressCard({ title, tone, items, loading, error }) {
  return (
    <article className={`progress-card progress-card--${tone}`}>
      <header className="progress-card__header">
        <span className="progress-card__title">{title}</span>
      </header>
      {loading ? <div className="progress-card__state">Loading snapshot...</div> : null}
      {!loading && error ? (
        <div className="progress-card__state progress-card__state--error">
          Could not load snapshot.
        </div>
      ) : null}
      {!loading && !error ? (
        <div className="progress-card__rows">
          {items.map((item) => (
            <div key={item.label} className="progress-card__row">
              <div className="progress-card__row-head">
                <span className="progress-card__metric-label">{item.label}</span>
                <span
                  className={`progress-card__metric-delta progress-card__metric-delta--${
                    item.improvementDirection === "higher"
                      ? higherIsBetterDeltaTone(item.delta, 0)
                      : lowerIsBetterDeltaTone(item.delta, 0)
                  }`}
                >
                  {formatProgressDelta(item.delta)}
                </span>
              </div>
              <strong className="progress-card__metric-value">
                {formatNumber(item.value || 0)}
              </strong>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}

function buildCiMetrics(data) {
  const outcomeSummary = data?.outcome_trend?.meta?.summary || {};
  const durationSummary = data?.duration_trend?.meta?.summary || {};
  const totalBuilds = Number(outcomeSummary.total_count || 0);
  const successBuilds = Number(outcomeSummary.success_count || 0);
  const failureBuilds = Number(outcomeSummary.failure_count || 0);

  return {
    totalBuilds,
    successBuilds,
    failureBuilds,
    successRate: Number(
      outcomeSummary.success_rate_pct
        ?? (totalBuilds ? (successBuilds * 100) / totalBuilds : 0),
    ),
    avgQueue: Number(durationSummary.queue_avg_s || 0),
    avgRun: Number(durationSummary.run_avg_s || 0),
    avgTotal: Number(durationSummary.total_avg_s || 0),
  };
}

function getPeriodValues(data, name) {
  return data?.period_comparison?.groups?.find((group) => group.name === name)?.values || {};
}

function hasPeriodValues(data, name) {
  return Boolean(
    data?.period_comparison?.groups?.some(
      (group) => group.name === name && group.values,
    ),
  );
}

function lowerIsBetterDeltaTone(current, previous) {
  const delta = Number(current || 0) - Number(previous || 0);
  if (delta < 0) {
    return "improved";
  }
  if (delta > 0) {
    return "regressed";
  }
  return "neutral";
}

function higherIsBetterDeltaTone(current, previous) {
  const delta = Number(current || 0) - Number(previous || 0);
  if (delta > 0) {
    return "improved";
  }
  if (delta < 0) {
    return "regressed";
  }
  return "neutral";
}

function formatProgressDelta(value) {
  const numeric = Number(value || 0);
  if (numeric === 0) {
    return "0 vs last week";
  }
  return `${numeric > 0 ? "+" : ""}${formatNumber(numeric)} vs last week`;
}

function formatCostPercentDelta(value) {
  const numeric = Number(value || 0);
  const sign = numeric > 0 ? "+" : "";
  return `WoW ${sign}${formatPercent(numeric)}`;
}

function formatRelativePercentDelta(current, previous) {
  const baseline = Number(previous || 0);
  if (!baseline) {
    return "0.0%";
  }
  const delta = ((Number(current || 0) - baseline) * 100) / baseline;
  return `${delta > 0 ? "+" : ""}${formatPercent(delta)}`;
}

function formatWeeklyCost(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value || 0);
}

function prioritizeCostAccounts(items) {
  return [...(items || [])].sort((left, right) => {
    const leftPriority = left.cost_source === "gcp:pingcap-testing-account" ? 0 : 1;
    const rightPriority = right.cost_source === "gcp:pingcap-testing-account" ? 0 : 1;
    return leftPriority - rightPriority;
  });
}

function buildErrorDetailsShareItems(l2Details, selectedCatalog, limit = 10) {
  if (!l2Details) {
    return [];
  }

  const rows = selectedCatalog
    ? (l2Details[selectedCatalog] || []).map((item) => ({
        name: item.name,
        value: Number(item.value || 0),
      }))
    : Object.entries(l2Details).flatMap(([catalog, items]) =>
        (items || []).map((item) => ({
          name: `${formatErrorCatalogName(catalog)}/${item.name}`,
          value: Number(item.value || 0),
        })),
      );

  return rows
    .filter((item) => item.value > 0)
    .sort((a, b) => b.value - a.value || a.name.localeCompare(b.name))
    .slice(0, selectedCatalog ? rows.length : limit)
    .map((item, _index, visibleRows) => {
      const total = visibleRows.reduce((sum, row) => sum + row.value, 0) || 1;
      return {
        ...item,
        share_pct: (item.value * 100) / total,
        interactive: false,
      };
    });
}

function formatErrorCatalogName(value) {
  const labels = {
    INFRA: "Infra",
    BUILD: "Build",
    UT: "UT",
    IT: "IT",
    OTHERS: "Others",
  };
  return labels[value] || value;
}

function formatSummaryRange(range) {
  const formatter = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  const start = new Date(`${range.start_date}T00:00:00`);
  const end = new Date(`${range.end_date}T00:00:00`);
  return `${formatter.format(start)} - ${formatter.format(end)}`;
}
