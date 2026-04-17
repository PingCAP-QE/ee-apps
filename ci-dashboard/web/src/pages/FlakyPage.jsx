import {
  formatDelta,
  formatNumber,
  formatPercent,
  useApiData,
} from "../lib/api";
import {
  PageIntro,
  Panel,
  DistinctCaseCountTable,
  IssueWeeklyRateTable,
  PeriodComparisonTable,
  RankingList,
  ShareBars,
  StatCard,
  TrendChart,
} from "../components/charts";

export default function FlakyPage({ filters }) {
  const page = useApiData("/api/v1/pages/flaky", filters);
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
  const unclassifiedFailureCount = Number(
    (page.data?.failure_category_share?.groups || []).reduce(
      (sum, group) => sum + (group.values?.[1] || 0),
      0,
    ),
  );

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
        <strong>Job-level scope below this point</strong>
        <span>
          Summary cards and the charts below use repo, branch, job, cloud, and time only.
          The issue status filter applies only to the issue-filtered panels above.
        </span>
      </div>

      <section className="stats-grid">
        <StatCard
          label="Flaky Noisy Rate"
          value={formatPercent(currentPeriod?.flaky_rate_pct || 0)}
          detail={`${formatNumber(currentPeriod?.flaky_build_count || 0)} / ${formatNumber(failureLikeBuildCount)}`}
          tone="rose"
          delta={
            currentPeriod && previousPeriod
              ? formatDelta(currentPeriod.flaky_rate_pct, previousPeriod.flaky_rate_pct, "%")
              : null
          }
        />
        <StatCard
          label="Blind-retry-loop Rate"
          value={formatPercent(currentPeriod?.retry_loop_rate_pct || 0)}
          detail={`${formatNumber(currentPeriod?.retry_loop_build_count || 0)} / ${formatNumber(failureLikeBuildCount)}`}
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
          detail={`${formatNumber(currentPeriod?.noisy_build_count || 0)} / ${formatNumber(failureLikeBuildCount)}`}
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
          label="Unclassified failures"
          value={formatNumber(unclassifiedFailureCount)}
          detail="A reminder that V1 keeps taxonomy intentionally conservative"
        />
      </section>

      <div className="page-grid page-grid--two-column">
        <Panel
          title="Flaky rate trend"
          subtitle="Flaky rate alongside total failure-like volume."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={page.data?.trend?.series}
            rightYFormatter={formatPercent}
            rightYMax={100}
          />
        </Panel>

        <Panel
          title="Signal composition"
          subtitle="Flaky, blind-retry-loop, and noisy rates together in one view."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={page.data?.composition?.series}
            rightYFormatter={formatPercent}
            rightYMax={100}
          />
        </Panel>

        <Panel
          title="Top noisy jobs"
          subtitle="Jobs where pilot-style noisy behavior is most concentrated."
          loading={page.loading}
          error={page.error}
        >
          <RankingList items={page.data?.top_jobs?.items} />
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
          <PeriodComparisonTable groups={page.data?.period_comparison?.groups} />
        </Panel>
      </div>
    </div>
  );
}
