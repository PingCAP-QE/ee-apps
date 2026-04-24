import {
  formatDelta,
  formatNumber,
  formatPercent,
  useApiData,
} from "../lib/api";
import {
  BLIND_RETRY_LOOP_HINT,
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

export default function FlakyPage({ filters }) {
  const page = useApiData("/api/v1/pages/flaky", filters);
  const weeklyFlakyTrend = useApiData("/api/v1/flaky/composition", {
    repo: filters.repo,
    branch: filters.branch,
    job_name: filters.job_name,
    cloud_phase: filters.cloud_phase,
    issue_status: "",
    start_date: filters.start_date,
    end_date: filters.end_date,
    granularity: "week",
  });
  const showPanelActions = !page.loading && !page.error;
  const currentPeriod = page.data?.period_comparison?.groups?.find((group) => group.name === "period_a")?.values;
  const previousPeriod = page.data?.period_comparison?.groups?.find((group) => group.name === "period_b")?.values;
  const distinctRows = page.data?.distinct_flaky_case_counts?.rows || [];
  const issueWeeks = page.data?.issue_case_weekly_rates?.weeks || [];
  const issueRows = page.data?.issue_case_weekly_rates?.rows || [];
  const issueLifecycle = page.data?.issue_lifecycle || {};
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
  const latestFullWeekLabel = issueLifecycle?.meta?.latest_full_week_start || "latest full week";
  const latestFullWeekDisplay = `${latestFullWeekLabel} week`;

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="Flaky"
        title="Separate noisy instability from the failures we still cannot classify cleanly"
        description="This page follows the same intent as the pilot logic, but makes the signals explorable by repo, branch, job, and cloud."
      />

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

      <Panel
        title="Issue lifecycle by week"
        subtitle="Weekly created, closed, and reopened issue counts in the current repo and branch scope."
        loading={page.loading}
        error={page.error}
      >
        <TrendChart
          series={page.data?.issue_lifecycle_weekly?.series}
          yFormatter={formatNumber}
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
            <strong>Above: issue-filtered, case-level panels</strong>
            <span>
              The two issue panels use the tracked GitHub issue set in the current repo, branch,
              time range, and issue status.
            </span>
          </div>
          <div className="scope-note__block">
            <strong>Below: job-level, build-based panels</strong>
            <span>
              Summary cards and charts below ignore issue status. They use repo, branch, job,
              cloud, and time only.
            </span>
          </div>
        </div>
      </div>

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
        <StatCard
          label={`Issues created (${latestFullWeekDisplay})`}
          value={formatNumber(issueLifecycle.latest_week_created_count || 0)}
          detail={`${formatNumber(issueLifecycle.scoped_open_count || 0)} open now (current status)`}
          tone="rose"
        />
        <StatCard
          label={`Issues closed (${latestFullWeekDisplay})`}
          value={formatNumber(issueLifecycle.latest_week_closed_count || 0)}
          detail={`${formatNumber(issueLifecycle.latest_week_reopened_count || 0)} reopened this week`}
          tone="teal"
        />
      </section>

      <div className="page-grid page-grid--two-column">
        <Panel
          title="Flaky rate trend"
          subtitle="Flaky, blind-retry-loop, and noisy rates together, each using failure-like builds as the denominator."
          loading={weeklyFlakyTrend.loading}
          error={weeklyFlakyTrend.error}
        >
        <TrendChart
          series={weeklyFlakyTrend.data?.series}
          rightYFormatter={formatPercent}
          compactY
        />
      </Panel>

        <Panel
          title="Top noisy jobs"
          subtitle="Jobs ranked by noisy rate, with raw noisy and failure-like build counts kept visible for context."
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.top_jobs?.items}
            valueFormatter={formatPercent}
          />
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
            height={188}
          />
        </Panel>

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
