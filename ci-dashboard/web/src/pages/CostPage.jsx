import {
  formatCompactCurrency,
  formatCurrency,
  formatPercent,
  useApiData,
} from "../lib/api";
import {
  DonutShareChart,
  PageIntro,
  Panel,
  StatCard,
  TrendChart,
  UnmatchedResourceTable,
} from "../components/charts";

const ANNUAL_GCP_BUDGET = 17300 * 12;
const ANNUAL_TICDC_BUDGET = 4500 * 12;

export default function CostPage({ filters }) {
  const costFilters = {
    start_date: filters.start_date,
    end_date: filters.end_date,
    granularity: filters.granularity === "month" ? "month" : "week",
  };
  const trend = useApiData("/api/v1/pages/cost-trend", costFilters);
  const repoGroupStack = useApiData("/api/v1/pages/cost-repo-group-stack", costFilters);
  const engineeringGroupShare = useApiData("/api/v1/pages/cost-engineering-group-share", costFilters);
  const unmatchedResources = useApiData("/api/v1/pages/cost-unmatched-resources", costFilters);
  const summary = trend.data?.meta?.summary || {};
  const stackTotal = (repoGroupStack.data?.items || []).reduce(
    (sum, item) => sum + Number(item.value || 0),
    0,
  );
  const level1Total = engineeringGroupShare.data?.level1?.meta?.total_list_cost || 0;
  const level2Total = engineeringGroupShare.data?.level2?.meta?.total_list_cost || 0;
  const costTrendSeries = withBudgetSeries(
    trend.data?.series,
    costFilters.granularity,
  );

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="Cost Insight"
        title="Cloud spend by time, repo, and engineering ownership"
        description="A first read on GCP cost attribution after raw billing rows are joined with roster ownership."
        kicker={`${costFilters.granularity} buckets`}
      />

      <section className="stats-grid">
        <StatCard
          label="Net cost"
          value={formatCurrency(summary.net_cost)}
          detail="After credits in the selected window"
        />
        <StatCard
          label="List cost"
          value={formatCurrency(summary.list_cost)}
          detail="At public SKU pricing before negotiated discounts"
          tone="teal"
        />
        <StatCard
          label="Resource labeled rate"
          value={formatPercent(summary.matched_resource_pct)}
          detail={`${formatCurrency(summary.matched_resource_cost)} / ${formatCurrency(summary.total_resource_cost)} list cost matched to employee`}
          tone="amber"
        />
        <StatCard
          label="Annual budget"
          value={formatCurrency(ANNUAL_GCP_BUDGET)}
          detail={`Includes ticdc ${formatCurrency(ANNUAL_TICDC_BUDGET)} / year`}
          tone="rose"
        />
      </section>

      <div className="page-grid page-grid--two-column">
        <Panel
          title="Cost trend"
          subtitle="Weekly or monthly list cost with net cost as the final billing comparison line."
          loading={trend.loading}
          error={trend.error}
        >
          <TrendChart
            series={costTrendSeries}
            yFormatter={formatCompactCurrency}
            height={320}
            yTickMode="thousands-rounded"
            yTickSegments={5}
          />
        </Panel>

        <Panel
          title="Repo cost stack"
          subtitle="Top repos stacked by list cost bucket."
          loading={repoGroupStack.loading}
          error={repoGroupStack.error}
        >
          <TrendChart
            series={repoGroupStack.data?.series}
            yFormatter={formatCompactCurrency}
            height={300}
            compactY
            stackBars
            yTickMode="thousands-rounded"
            yTickSegments={5}
            barGroupWidthFactor={0.56}
            barMaxWidth={58}
          />
        </Panel>
      </div>

      <Panel
        title="Engineering Group allocation"
        subtitle="List cost share under Engineering Group, split once by direct child groups and once by second-level groups."
        loading={engineeringGroupShare.loading}
        error={engineeringGroupShare.error}
      >
        <div className="donut-grid">
          <DonutShareChart
            title="Level 1 groups"
            subtitle="Direct children under Engineering Group."
            items={engineeringGroupShare.data?.level1?.items}
            totalLabel="list cost"
            emptyMessage="No Engineering Group level-1 cost share data yet."
          />
          <DonutShareChart
            title="Level 2 groups"
            subtitle="Second-level teams under Engineering Group."
            items={engineeringGroupShare.data?.level2?.items}
            totalLabel="list cost"
            emptyMessage="No Engineering Group level-2 cost share data yet."
          />
        </div>
      </Panel>

      <Panel
        title="Top unmatched resources"
        subtitle="Top 20 named resources without an employee match and without GKE workload allocation, ordered by unallocated list cost."
        loading={unmatchedResources.loading}
        error={unmatchedResources.error}
      >
        <UnmatchedResourceTable items={unmatchedResources.data?.items} />
      </Panel>
    </div>
  );
}

function withBudgetSeries(series, granularity) {
  if (!series?.length) {
    return series;
  }
  const labels = Array.from(
    new Set(series.flatMap((item) => item.points.map((point) => point[0]))),
  ).sort();
  const budgetPerBucket =
    granularity === "month" ? ANNUAL_GCP_BUDGET / 12 : ANNUAL_GCP_BUDGET / 52;

  return [
    ...series,
    {
      key: "gcp_budget",
      label: granularity === "month" ? "Monthly budget" : "Weekly budget",
      type: "line",
      dash: true,
      showPoints: false,
      points: labels.map((label) => [label, budgetPerBucket]),
    },
  ];
}
