import {
  formatPercent,
  formatRoundedThousands,
  formatSeconds,
  useApiData,
} from "../lib/api";
import {
  MigrationFixedWindowComparisonTable,
  PageIntro,
  Panel,
  RuntimeComparisonBoard,
  TrendChart,
} from "../components/charts";

export default function MigrateStatusPage({ filters }) {
  const page = useApiData("/api/v1/pages/ci-status", filters);
  const cloudPostureAnnotations = buildCloudPostureAnnotations(page.data?.cloud_posture_trend?.series);
  const bucketLabel = `${filters.granularity[0].toUpperCase()}${filters.granularity.slice(1)}`;
  const fixedWindowComparisonMeta = page.data?.migration_fixed_window_comparison?.meta;
  const fixedWindowComparisonSubtitle = buildFixedWindowComparisonSubtitle(
    fixedWindowComparisonMeta,
  );
  const fixedWindowDurationSeries = buildFixedWindowDurationSeries(
    page.data?.migration_fixed_window_comparison?.rows,
  );

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="Migrate Status"
        title="Track rollout volume and runtime drift from GCP to Tencent"
        description="This page isolates the migration view so we can compare rollout posture and same-job runtime changes without mixing it into the broader CI health page."
      />

      <Panel
        title="Migration status"
        subtitle={`${bucketLabel} build counts on GCP and Tencent. The value shown above each bar is Tencent build count % of total builds in that bucket.`}
        loading={page.loading}
        error={page.error}
      >
        <TrendChart
          series={page.data?.cloud_posture_trend?.series}
          yFormatter={formatRoundedThousands}
          bucketAnnotations={cloudPostureAnnotations}
          height={188}
          stackBars
          yTickMode="thousands-rounded"
          axisLabelSize={9}
          bottomLabelSize={10}
          annotationLabelSize={9}
          barGroupWidthFactor={0.3}
          barMaxWidth={16}
          leftPadding={64}
        />
      </Panel>

      <Panel
        title="Migration runtime comparison"
        subtitle="Same-job Jenkins success run-time comparison. GCP baseline is the 14 days before first Tencent success; Tencent uses the latest 14 days ending at the selected end date. This panel ignores start date, bucket, and cloud filters."
        loading={page.loading}
        error={page.error}
      >
        <RuntimeComparisonBoard
          improved={page.data?.migration_runtime_comparison?.improved}
          regressed={page.data?.migration_runtime_comparison?.regressed}
          windowDays={page.data?.migration_runtime_comparison?.meta?.window_days}
          minSuccessRuns={page.data?.migration_runtime_comparison?.meta?.min_success_runs_each_side}
        />
      </Panel>

      <Panel
        title="Matched Jenkins jobs: GCP baseline vs recent Tencent"
        subtitle={fixedWindowComparisonSubtitle}
        loading={page.loading}
        error={page.error}
      >
        <div className="chart-table-stack">
          <TrendChart
            series={fixedWindowDurationSeries}
            yFormatter={formatSeconds}
            height={220}
            preserveLabelOrder
          />
          <MigrationFixedWindowComparisonTable
            rows={page.data?.migration_fixed_window_comparison?.rows}
          />
        </div>
      </Panel>
    </div>
  );
}

function buildFixedWindowComparisonSubtitle(meta) {
  if (!meta?.baseline_start_date || !meta?.baseline_end_date || !meta?.recent_start_date) {
    return "Fixed-window comparison for All repos, TiDB, and TiCDC.";
  }

  const recentEndDate = meta.recent_end_date || "latest Tencent success date";
  return `All repos, TiDB, and TiCDC compared side by side. Only Jenkins job names successful in both windows are included. GCP baseline uses ${meta.baseline_start_date} to ${meta.baseline_end_date} and is weighted by each job's Tencent success count; recent Tencent uses ${meta.recent_start_date} to ${recentEndDate}. Avg duration uses successful builds only, and this panel ignores repo, branch, job, start, bucket, and cloud filters.`;
}

function buildFixedWindowDurationSeries(rows) {
  if (!rows?.length) {
    return [];
  }

  return [
    {
      key: "baseline_avg_total_s",
      label: "Tencent-weighted GCP avg duration",
      type: "bar",
      axis: "left",
      points: rows.map((row) => [row.scope_label, Number(row.baseline?.success_avg_total_s || 0)]),
    },
    {
      key: "recent_tencent_avg_total_s",
      label: "Recent Tencent Jenkins avg duration",
      type: "bar",
      axis: "left",
      points: rows.map((row) => [row.scope_label, Number(row.recent_tencent?.success_avg_total_s || 0)]),
    },
  ];
}

function buildCloudPostureAnnotations(series) {
  const tencentSeries = series?.find((item) => item.key === "tencent_build_count");
  if (!tencentSeries) {
    return [];
  }

  const countsBySeries = series.map((item) => new Map(
    item.points.map(([label, value]) => [label, Number(value || 0)]),
  ));
  return Array.from(new Set(series.flatMap((item) => item.points.map(([label]) => label))))
    .sort()
    .map((label) => {
      const totalBuilds = countsBySeries.reduce((total, counts) => total + (counts.get(label) || 0), 0);
      const tencentBuilds = Number(
        tencentSeries.points.find(([pointLabel]) => pointLabel === label)?.[1] || 0,
      );
      return {
        label,
        text: formatPercent(totalBuilds ? (tencentBuilds * 100) / totalBuilds : 0),
      };
    });
}
