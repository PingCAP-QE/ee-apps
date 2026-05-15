import { useEffect, useState } from "react";

import {
  formatDelta,
  formatDateRangeLabel,
  formatNumber,
  formatPercent,
  useApiData,
} from "../lib/api";
import {
  BLIND_RETRY_LOOP_HINT,
  BucketFlakyRateTable,
  PageIntro,
  Panel,
  DistinctCaseCountTable,
  InfoHint,
  IssueWeeklyRateTable,
  PeriodComparisonTable,
  RankingList,
  ShareBars,
  StatCard,
  TrendChart,
} from "../components/charts";

function formatCountDelta(value) {
  const numeric = Number(value || 0);
  if (numeric === 0) {
    return "0 vs last week";
  }
  return `${numeric > 0 ? "+" : ""}${formatNumber(numeric)} vs last week`;
}

function buildSnapshotSubtitle(meta) {
  const asOfDate = meta?.as_of_date;
  const comparisonDate = meta?.comparison_as_of_date;
  if (!asOfDate || !comparisonDate) {
    return "Repo + branch snapshot";
  }
  return `As of ${asOfDate}, compared with ${comparisonDate}.`;
}

function buildBucketedFlakyRateSubtitle(meta) {
  const effectiveGranularity = meta?.effective_granularity === "month" ? "month" : "week";
  const startDate = meta?.start_date || "the selected start date";
  const endDate = meta?.end_date || "the selected end date";
  if (meta?.requested_granularity === "day") {
    return `Grouped by ${effectiveGranularity} from ${startDate} to ${endDate}. Day stays rolled up to ${effectiveGranularity} here because daily buckets are too fine for this view.`;
  }
  return `Grouped by ${effectiveGranularity} from ${startDate} to ${endDate}, with failure-like builds as the denominator for each bucket.`;
}

function buildCenteredPercentAxisMax(rows) {
  const values = (rows || [])
    .map((row) => Number(row.flaky_rate_pct || 0))
    .filter((value) => Number.isFinite(value));
  if (!values.length) {
    return 10;
  }
  const peak = Math.max(...values, 0);
  if (peak >= 100) {
    return 100;
  }
  const target = Math.max(10, Math.min(100, peak * 2));
  return Math.ceil(target / 5) * 5;
}

function parseIsoDate(value) {
  const [year, month, day] = String(value || "").split("-").map(Number);
  if (!year || !month || !day) {
    return null;
  }
  return new Date(Date.UTC(year, month - 1, day, 12, 0, 0));
}

function toIsoDate(value) {
  return value.toISOString().slice(0, 10);
}

function resolveSelectedBucketRange(bucketTime, meta) {
  if (!bucketTime) {
    return null;
  }

  const bucketStart = parseIsoDate(bucketTime);
  const selectedStart = parseIsoDate(meta?.start_date);
  const selectedEnd = parseIsoDate(meta?.end_date);
  if (!bucketStart || !selectedStart || !selectedEnd) {
    return null;
  }

  const bucketEnd = new Date(bucketStart);
  if (meta?.effective_granularity === "month") {
    bucketEnd.setUTCMonth(bucketEnd.getUTCMonth() + 1, 0);
  } else {
    bucketEnd.setUTCDate(bucketEnd.getUTCDate() + 6);
  }

  const rangeStart = bucketStart > selectedStart ? bucketStart : selectedStart;
  const rangeEnd = bucketEnd < selectedEnd ? bucketEnd : selectedEnd;
  if (rangeStart > rangeEnd) {
    return null;
  }

  return {
    time: bucketTime,
    start_date: toIsoDate(rangeStart),
    end_date: toIsoDate(rangeEnd),
  };
}

function buildTopJobsSubtitle(selectedBucketRange) {
  if (!selectedBucketRange) {
    return "Jobs ranked by noisy rate, with raw noisy and failure-like build counts kept visible for context.";
  }
  return `Jobs ranked by noisy rate for ${formatDateRangeLabel(selectedBucketRange.start_date, selectedBucketRange.end_date)}. Click the selected bucket again to clear the focus.`;
}

function SnapshotProgressCard({ title, subtitle, tone = "default", items, loading, error }) {
  return (
    <article className={`progress-card progress-card--${tone}`}>
      <header className="progress-card__header">
        <span className="progress-card__title">{title}</span>
        <span className="progress-card__subtitle">{subtitle}</span>
      </header>
      {loading ? <div className="progress-card__state">Loading snapshot...</div> : null}
      {!loading && error ? <div className="progress-card__state progress-card__state--error">Could not load snapshot.</div> : null}
      {!loading && !error ? (
        <div className="progress-card__rows">
          {items.map((item) => (
            <div key={item.label} className="progress-card__row">
              <div className="progress-card__row-head">
                <span className="progress-card__metric-label">{item.label}</span>
                <span className="progress-card__metric-delta">{item.delta}</span>
              </div>
              <strong className="progress-card__metric-value">{item.value}</strong>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}

export default function FlakyPage({ filters }) {
  const page = useApiData("/api/v1/pages/flaky", filters);
  const [selectedBucketTime, setSelectedBucketTime] = useState(null);
  const showPanelActions = !page.loading && !page.error;
  const currentPeriod = page.data?.period_comparison?.groups?.find((group) => group.name === "period_a")?.values;
  const previousPeriod = page.data?.period_comparison?.groups?.find((group) => group.name === "period_b")?.values;
  const distinctRows = page.data?.distinct_flaky_case_counts?.rows || [];
  const issueWeeks = page.data?.issue_case_weekly_rates?.weeks || [];
  const issueRows = page.data?.issue_case_weekly_rates?.rows || [];
  const latestDistinctTotal = distinctRows.reduce(
    (sum, row) => sum + Number(row.values?.[row.values.length - 1] || 0),
    0,
  );
  const openIssueCount = issueRows.filter(
    (row) => String(row.issue_status || "").toLowerCase() === "open",
  ).length;
  const reopenedIssueCount = issueRows.filter((row) => Number(row.reopen_count || 0) > 0).length;
  const noFlakyPastTwoWeeksCases = issueRows.filter((row) => {
    const recentMetrics = (row.metrics || []).slice(-2);
    return recentMetrics.length > 0 && recentMetrics.every((metric) => Number(metric.flaky_rate_pct || 0) === 0);
  }).length;
  const latestIssueWeekLabel = issueWeeks[issueWeeks.length - 1] || "latest bucket";
  const failureLikeBuildCount = Number(currentPeriod?.failure_like_build_count || 0);
  const totalPrCount = Number(currentPeriod?.total_pr_count || 0);
  const issueFixProgress = page.data?.issue_fix_progress || {};
  const snapshotSubtitle = buildSnapshotSubtitle(issueFixProgress.meta);
  const bucketedFlakyRate = page.data?.bucketed_flaky_rate || {};
  const bucketedFlakyRateRows = bucketedFlakyRate.rows || [];
  const bucketedFlakyRateMeta = bucketedFlakyRate.meta || {};
  const bucketedFlakyRateSubtitle = buildBucketedFlakyRateSubtitle(bucketedFlakyRateMeta);
  const bucketedFlakyRateAxisMax = buildCenteredPercentAxisMax(bucketedFlakyRateRows);
  const selectedBucketRange = resolveSelectedBucketRange(selectedBucketTime, bucketedFlakyRateMeta);
  const bucketTopJobs = useApiData(
    "/api/v1/flaky/top-jobs",
    selectedBucketRange
      ? {
          repo: filters.repo,
          branch: filters.branch,
          job_name: filters.job_name,
          cloud_phase: filters.cloud_phase,
          start_date: selectedBucketRange.start_date,
          end_date: selectedBucketRange.end_date,
          granularity: bucketedFlakyRateMeta.effective_granularity || "week",
          limit: 8,
        }
      : {},
    Boolean(selectedBucketRange),
  );
  const topJobsItems = selectedBucketRange ? bucketTopJobs.data?.items : page.data?.top_jobs?.items;
  const topJobsLoading = selectedBucketRange ? bucketTopJobs.loading : page.loading;
  const topJobsError = selectedBucketRange ? bucketTopJobs.error : page.error;
  const topJobsSubtitle = buildTopJobsSubtitle(selectedBucketRange);

  useEffect(() => {
    if (!selectedBucketTime) {
      return;
    }
    if (!bucketedFlakyRateRows.some((row) => row.time === selectedBucketTime)) {
      setSelectedBucketTime(null);
    }
  }, [bucketedFlakyRateRows, selectedBucketTime]);

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="Flaky"
        title="Separate noisy instability from the failures we still cannot classify cleanly"
        description="This page follows the same intent as the pilot logic, but makes the signals explorable by repo, branch, job, and cloud."
      />

      <section className="stats-grid">
        <StatCard
          label="Flaky Rate"
          value={formatPercent(currentPeriod?.flaky_rate_pct || 0)}
          detail={`${formatNumber(currentPeriod?.flaky_build_count || 0)} flaky / ${formatNumber(failureLikeBuildCount)} failure-like builds`}
          tone="rose"
          delta={
            currentPeriod && previousPeriod
              ? formatDelta(currentPeriod.flaky_rate_pct, previousPeriod.flaky_rate_pct, "%")
              : null
          }
        />
        <StatCard
          label={(
            <span className="stat-card__label-row">
              <span>Blind-retry-loop Rate</span>
              <InfoHint text={BLIND_RETRY_LOOP_HINT} />
            </span>
          )}
          value={formatPercent(currentPeriod?.retry_loop_rate_pct || 0)}
          detail={`${formatNumber(currentPeriod?.retry_loop_build_count || 0)} blind-retry-loop / ${formatNumber(failureLikeBuildCount)} failure-like builds`}
          tone="amber"
          delta={
            currentPeriod && previousPeriod
              ? formatDelta(
                  currentPeriod.retry_loop_rate_pct,
                  previousPeriod.retry_loop_rate_pct,
                  "%",
                )
              : null
          }
        />
        <StatCard
          label="Combined Noisy Rate"
          value={formatPercent(currentPeriod?.noisy_rate_pct || 0)}
          detail={`${formatNumber(currentPeriod?.noisy_build_count || 0)} noisy / ${formatNumber(failureLikeBuildCount)} failure-like builds`}
          tone="teal"
          delta={
            currentPeriod && previousPeriod
              ? formatDelta(currentPeriod.noisy_rate_pct, previousPeriod.noisy_rate_pct, "%")
              : null
          }
        />
        <StatCard
          label="Affected PR Rate"
          value={formatPercent(currentPeriod?.affected_pr_rate_pct || 0)}
          detail={`${formatNumber(currentPeriod?.affected_pr_count || 0)} / ${formatNumber(totalPrCount)} PRs`}
          tone="default"
          delta={
            currentPeriod && previousPeriod
              ? formatDelta(currentPeriod.affected_pr_rate_pct, previousPeriod.affected_pr_rate_pct, "%")
              : null
          }
        />
      </section>

      <Panel
        title="Distinct flaky case number"
        subtitle="Weekly distinct flaky testcase count derived from problem_case_runs, aligned by PR target branch."
        loading={page.loading}
        error={page.error}
        actions={showPanelActions ? (
          <div className="panel-badge-row">
            <span className="panel-badge">
              <strong>{formatNumber(distinctRows.length)}</strong>
              <span>branches</span>
            </span>
            <span className="panel-badge">
              <strong>{formatNumber(latestDistinctTotal)}</strong>
              <span>{latestIssueWeekLabel}</span>
            </span>
          </div>
        ) : null}
      >
        <DistinctCaseCountTable
          weeks={page.data?.distinct_flaky_case_counts?.weeks}
          rows={page.data?.distinct_flaky_case_counts?.rows}
          scrollClassName="table-scroll--compact-y"
        />
      </Panel>

      <section className="progress-summary-grid">
        <SnapshotProgressCard
          title="Flaky issue progress"
          subtitle={snapshotSubtitle}
          tone="rose"
          loading={page.loading}
          error={page.error}
          items={[
            {
              label: "Filed",
              value: formatNumber(issueFixProgress.filed_issue_count || 0),
              delta: formatCountDelta(issueFixProgress.filed_issue_delta || 0),
            },
            {
              label: "Fixed",
              value: formatNumber(issueFixProgress.fixed_issue_count || 0),
              delta: formatCountDelta(issueFixProgress.fixed_issue_delta || 0),
            },
          ]}
        />
        <SnapshotProgressCard
          title="Fix PR progress"
          subtitle={snapshotSubtitle}
          tone="teal"
          loading={page.loading}
          error={page.error}
          items={[
            {
              label: "In review",
              value: formatNumber(issueFixProgress.in_review_pr_count || 0),
              delta: formatCountDelta(issueFixProgress.in_review_pr_delta || 0),
            },
            {
              label: "Merged",
              value: formatNumber(issueFixProgress.merged_pr_count || 0),
              delta: formatCountDelta(issueFixProgress.merged_pr_delta || 0),
            },
          ]}
        />
      </section>

      <Panel
        title="Issue lifecycle by week"
        subtitle="Weekly created, closed, reopened, and week-end open issue counts in the current repo and branch scope."
        loading={page.loading}
        error={page.error}
      >
        <TrendChart
          series={page.data?.issue_lifecycle_weekly?.series}
          yFormatter={formatNumber}
          rightYFormatter={formatNumber}
          height={220}
        />
      </Panel>

      <Panel
        title="Filtered-issue weekly case table"
        subtitle="Each row keeps the issue link and shows weekly rate as rate (flaky runs / estimated runs)."
        loading={page.loading}
        error={page.error}
        actions={showPanelActions ? (
          <div className="panel-badge-row">
            <span className="panel-badge">
              <strong>{formatNumber(issueRows.length)}</strong>
              <span>issue cases</span>
            </span>
            <span className="panel-badge">
              <strong>{formatNumber(noFlakyPastTwoWeeksCases)}</strong>
              <span>cases no flaky in past 2 weeks</span>
            </span>
          </div>
        ) : null}
      >
        <IssueWeeklyRateTable
          weeks={page.data?.issue_case_weekly_rates?.weeks}
          rows={page.data?.issue_case_weekly_rates?.rows}
          scrollClassName="table-scroll--tall"
        />
      </Panel>

      <div className="scope-note">
        <span className="scope-note__eyebrow">Scope switch</span>
        <div className="scope-note__grid">
          <div className="scope-note__block">
            <strong>Issue-tracked panels</strong>
            <span>
              The progress cards, lifecycle chart, and filtered case table use tracked flaky
              issues and linked fix PRs. The snapshot cards are repo and branch scoped, as of
              the selected end date.
            </span>
          </div>
          <div className="scope-note__block">
            <strong>Build-scope panels</strong>
            <span>
              The four rate cards and the remaining noisy and failure charts ignore issue status
              and follow the build-side repo, branch, job, cloud, and time filters.
            </span>
          </div>
        </div>
      </div>

      <div className="flaky-highlight-row">
        <div className="flaky-highlight-stack">
          <Panel
            title="Flaky rate trend"
            subtitle={bucketedFlakyRateSubtitle}
            loading={page.loading}
            error={page.error}
            actions={showPanelActions ? (
              <div className="panel-badge-row">
                <span className="panel-badge">
                  <strong>{formatNumber(bucketedFlakyRateRows.length)}</strong>
                  <span>buckets</span>
                </span>
              </div>
            ) : null}
          >
            <div className="chart-table-stack">
              <TrendChart
                series={bucketedFlakyRate.series}
                yFormatter={formatPercent}
                yMax={bucketedFlakyRateAxisMax}
                compactY
                height={208}
                selectedBucketLabel={selectedBucketTime}
                onBucketSelect={setSelectedBucketTime}
              />
              <BucketFlakyRateTable
                rows={bucketedFlakyRateRows}
                effectiveGranularity={bucketedFlakyRateMeta.effective_granularity}
                selectedTime={selectedBucketTime}
                onSelectTime={setSelectedBucketTime}
              />
            </div>
          </Panel>

          <Panel
            title="Filtered-issue flaky rate"
            subtitle="Weekly flaky rate for the testcase set currently tracked by flaky GitHub issues in this scope."
            loading={page.loading}
            error={page.error}
            actions={showPanelActions ? (
              <div className="panel-badge-row">
                <span className="panel-badge">
                  <strong>{formatNumber(issueRows.length)}</strong>
                  <span>tracked cases</span>
                </span>
                <span className="panel-badge">
                  <strong>{formatNumber(openIssueCount)}</strong>
                  <span>open issues</span>
                </span>
                <span className="panel-badge">
                  <strong>{formatNumber(reopenedIssueCount)}</strong>
                  <span>reopened</span>
                </span>
              </div>
            ) : null}
          >
            <TrendChart
              series={page.data?.issue_filtered_weekly_trend?.series}
              yFormatter={formatPercent}
              yMax={100}
              compactY
              height={188}
            />
          </Panel>
        </div>

        <Panel
          title="Top noisy jobs"
          subtitle={topJobsSubtitle}
          loading={topJobsLoading}
          error={topJobsError}
          actions={selectedBucketRange ? (
            <button
              type="button"
              className="ghost-button ghost-button--compact"
              onClick={() => setSelectedBucketTime(null)}
            >
              Show all buckets
            </button>
          ) : null}
          className="panel--full-height"
        >
          <RankingList
            items={topJobsItems}
            valueFormatter={formatPercent}
          />
        </Panel>
      </div>

      <div className="page-grid page-grid--two-column">

        <Panel
          title="Failure category share (draft)"
          subtitle="Current V1 split between explicit flaky evidence and everything else."
          loading={page.loading}
          error={page.error}
        >
          <ShareBars
            categories={page.data?.failure_category_share?.categories}
            groups={page.data?.failure_category_share?.groups}
          />
        </Panel>

        <Panel
          title="Failure category trend (draft)"
          subtitle="Useful for spotting when unclassified failures start dominating the page."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart series={page.data?.failure_category_trend?.series} />
        </Panel>

        <Panel
          title="Period comparison (draft)"
          subtitle="Current window against the immediately preceding window."
          loading={page.loading}
          error={page.error}
        >
          <PeriodComparisonTable
            groups={page.data?.period_comparison?.groups}
            meta={page.data?.period_comparison?.meta}
          />
        </Panel>
      </div>
    </div>
  );
}
