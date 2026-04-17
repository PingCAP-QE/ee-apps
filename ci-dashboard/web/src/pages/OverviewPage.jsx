import {
  formatDelta,
  formatNumber,
  formatPercent,
  sumSeriesPoints,
  useApiData,
} from "../lib/api";
import {
  CloudComparisonPanel,
  FreshnessStrip,
  PageIntro,
  Panel,
  PeriodComparisonTable,
  RankingList,
  ShareBars,
  StatCard,
  TrendChart,
} from "../components/charts";

export default function OverviewPage({ filters }) {
  const page = useApiData("/api/v1/pages/overview", filters);
  const totalBuilds = sumSeriesPoints(page.data?.outcome_trend?.series, "total_count");
  const successBuilds = sumSeriesPoints(page.data?.outcome_trend?.series, "success_count");
  const failureBuilds = sumSeriesPoints(page.data?.outcome_trend?.series, "failure_count");
  const successRate = totalBuilds ? (successBuilds * 100) / totalBuilds : 0;
  const currentPeriod = page.data?.period_comparison?.groups?.find((group) => group.name === "period_a")?.values;
  const previousPeriod = page.data?.period_comparison?.groups?.find((group) => group.name === "period_b")?.values;

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="Overview"
        title="One place to see whether CI feels stable or twitchy"
        description="This landing page stays intentionally selective: enough signal to spot drift, then a short path into the deeper build and flaky views."
      />

      <section className="stats-grid">
        <StatCard
          label="Build volume"
          value={formatNumber(totalBuilds)}
          detail={`${formatNumber(successBuilds)} succeeded · ${formatNumber(failureBuilds)} failure-like`}
          delta={
            currentPeriod && previousPeriod
              ? formatDelta(currentPeriod.total_build_count, previousPeriod.total_build_count)
              : null
          }
        />
        <StatCard
          label="Success rate"
          value={formatPercent(successRate)}
          detail="Across the current filter window"
          tone="teal"
          delta={
            currentPeriod && previousPeriod
              ? formatDelta(
                  totalBuilds ? successRate : 0,
                  previousPeriod.total_build_count
                    ? ((previousPeriod.total_build_count - previousPeriod.failure_like_build_count) *
                        100) /
                        previousPeriod.total_build_count
                    : 0,
                  "%",
                )
              : null
          }
        />
        <StatCard
          label="Noisy builds"
          value={formatNumber(currentPeriod?.noisy_build_count || 0)}
          detail={`Flaky rate ${formatPercent(currentPeriod?.flaky_rate_pct || 0)}`}
          tone="rose"
          delta={
            currentPeriod && previousPeriod
              ? formatDelta(currentPeriod.noisy_build_count, previousPeriod.noisy_build_count)
              : null
          }
        />
        <StatCard
          label="Tracked repos"
          value={formatNumber(page.data?.repos?.items?.length || 0)}
          detail="Repos visible in the current date window"
          tone="amber"
        />
      </section>

      <div className="page-grid page-grid--overview">
        <Panel
          title="Build signal"
          subtitle="Volume, pass rate, and failure pressure over the selected time window."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart series={page.data?.outcome_trend?.series} />
        </Panel>

        <Panel
          title="Freshness and job cadence"
          subtitle="Useful when the page feels too quiet or a branch suddenly has null enrichment."
          loading={page.loading}
          error={page.error}
        >
          <FreshnessStrip
            jobs={page.data?.freshness?.jobs}
            generatedAt={page.data?.freshness?.generated_at || "n/a"}
          />
        </Panel>

        <Panel
          title="Cloud comparison"
          subtitle="First-pass read on throughput and latency differences between IDC and GCP."
          loading={page.loading}
          error={page.error}
        >
          <CloudComparisonPanel groups={page.data?.cloud_comparison?.groups} />
        </Panel>

        <Panel
          title="Top noisy jobs"
          subtitle="Jobs with the highest combined flaky and blind-retry-loop signal."
          loading={page.loading}
          error={page.error}
        >
          <RankingList items={page.data?.top_noisy_jobs?.items} />
        </Panel>

        <Panel
          title="Failure category share (draft)"
          subtitle="V1 keeps the taxonomy conservative, so this panel mainly separates flaky evidence from unclassified failures."
          loading={page.loading}
          error={page.error}
        >
          <ShareBars
            categories={page.data?.failure_category_share?.categories}
            groups={page.data?.failure_category_share?.groups}
          />
        </Panel>

        <Panel
          title="Current vs previous window"
          subtitle="A compact pulse check before drilling into the dedicated Flaky page."
          loading={page.loading}
          error={page.error}
        >
          <PeriodComparisonTable groups={page.data?.period_comparison?.groups} />
        </Panel>
      </div>
    </div>
  );
}
