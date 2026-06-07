import { useState } from "react";

import {
  formatCostSourceLabel,
  formatCompactCurrency,
  formatCurrency,
  formatDateRangeLabel,
  formatPercent,
  getLaggedTrailingDateRange,
  useApiData,
} from "../lib/api";
import { ALL_COST_SOURCES, DEFAULT_COST_SOURCE } from "../lib/filterUrl";
import {
  BudgetHealthGauge,
  DonutShareChart,
  PageIntro,
  Panel,
  StatCard,
  TrendChart,
  UnmatchedResourceTable,
} from "../components/charts";

const ANNUAL_GCP_BUDGET = 17300 * 12;
const ANNUAL_TICDC_BUDGET = 4500 * 12;
const SHARED_COST_GROUP = "Efficiency & Quality";

export default function CostPage({ filters }) {
  const [isWeeklyLevel2Shared, setIsWeeklyLevel2Shared] = useState(false);
  const [isSelectedLevel2Shared, setIsSelectedLevel2Shared] = useState(false);
  const weeklyOverviewRange = getLaggedTrailingDateRange();
  const selectedCostSource = filters.cost_source || DEFAULT_COST_SOURCE;
  const selectedCostSourceLabel = formatCostSourceLabel(selectedCostSource);
  const isPrimaryBudgetScope = selectedCostSource === DEFAULT_COST_SOURCE;
  const selectedCostSourceValue =
    selectedCostSource === ALL_COST_SOURCES ? "" : selectedCostSource;

  const weeklyOverviewFilters = {
    ...weeklyOverviewRange,
    granularity: "week",
    cost_source: selectedCostSourceValue,
  };
  const costFilters = {
    start_date: filters.start_date,
    end_date: filters.end_date,
    granularity: filters.granularity === "month" ? "month" : "week",
    cost_source: selectedCostSourceValue,
  };
  const weeklyOverview = useApiData("/api/v1/pages/cost-weekly-overview", weeklyOverviewFilters);
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
    isPrimaryBudgetScope,
  );
  const weeklyLevel2Items = withSharedCostAllocation(
    weeklyOverview.data?.level2_share?.items,
    isWeeklyLevel2Shared,
  );
  const selectedLevel2Items = withSharedCostAllocation(
    engineeringGroupShare.data?.level2?.items,
    isSelectedLevel2Shared,
  );

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="Cost Insight"
        title="Cloud spend by time, repo, and engineering ownership"
        description="Cloud cost attribution across configured billing sources after billing rows are joined with roster ownership."
        kicker={`${costFilters.granularity} buckets · ${selectedCostSourceLabel}`}
      />

      <Panel
        title="Weekly overview"
        subtitle={formatDateRangeLabel(weeklyOverviewRange.start_date, weeklyOverviewRange.end_date)}
        loading={weeklyOverview.loading}
        error={weeklyOverview.error}
        className="cost-weekly-overview"
      >
        <div className="cost-weekly-overview__grid">
          <div className="cost-weekly-overview__cards">
            <StatCard
              label="List cost"
              value={formatCurrency(weeklyOverview.data?.summary?.list_cost)}
              detail="Previous complete week"
              delta={formatDelta(weeklyOverview.data?.summary?.list_cost_wow_pct)}
              tone="teal"
            />
            <StatCard
              label="Net cost"
              value={formatCurrency(weeklyOverview.data?.summary?.net_cost)}
              detail={
                isPrimaryBudgetScope
                  ? `Weekly budget ${formatCurrency(ANNUAL_GCP_BUDGET / 52)}`
                  : "Budget not configured for this source"
              }
              delta={formatDelta(weeklyOverview.data?.summary?.net_cost_wow_pct)}
              tone="amber"
            />
          </div>
          <DonutShareChart
            title="Level 2 groups"
            subtitle={
              isWeeklyLevel2Shared
                ? `${SHARED_COST_GROUP} cost redistributed proportionally.`
                : "Groups above 1% of list cost."
            }
            items={weeklyLevel2Items}
            totalValue={weeklyOverview.data?.level2_share?.meta?.total_list_cost}
            totalLabel="list cost"
            emptyMessage="No Level 2 group above 1% for the previous complete week."
            onItemSelect={(item) => {
              if (item.name === SHARED_COST_GROUP) {
                setIsWeeklyLevel2Shared(true);
              }
            }}
            headerAction={
              isWeeklyLevel2Shared ? (
                <button
                  type="button"
                  className="donut-card__action"
                  onClick={() => setIsWeeklyLevel2Shared(false)}
                >
                  Reset
                </button>
              ) : null
            }
          />
          <DonutShareChart
            title="GCP services"
            subtitle="Services above 1% of list cost."
            items={weeklyOverview.data?.service_share?.items}
            totalValue={weeklyOverview.data?.service_share?.meta?.total_list_cost}
            totalLabel="list cost"
            emptyMessage="No service cost data for the previous complete week."
          />
          <BudgetHealthGauge
            title="Budget pace"
            subtitle="Observed YTD net cost, a lag-adjusted checkpoint, and a year-end forecast from the prior 14 observed days."
            data={weeklyOverview.data?.budget_health}
            emptyMessage="Budget pace is not configured for this source yet."
          />
        </div>
      </Panel>

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
          value={isPrimaryBudgetScope ? formatCurrency(ANNUAL_GCP_BUDGET) : "--"}
          detail={
            isPrimaryBudgetScope
              ? `Includes ticdc ${formatCurrency(ANNUAL_TICDC_BUDGET)} / year`
              : "Budget not configured for the selected source"
          }
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
            subtitle={
              isSelectedLevel2Shared
                ? `${SHARED_COST_GROUP} cost redistributed proportionally.`
                : "Second-level teams under Engineering Group."
            }
            items={selectedLevel2Items}
            totalLabel="list cost"
            emptyMessage="No Engineering Group level-2 cost share data yet."
            onItemSelect={(item) => {
              if (item.name === SHARED_COST_GROUP) {
                setIsSelectedLevel2Shared(true);
              }
            }}
            headerAction={
              isSelectedLevel2Shared ? (
                <button
                  type="button"
                  className="donut-card__action"
                  onClick={() => setIsSelectedLevel2Shared(false)}
                >
                  Reset
                </button>
              ) : null
            }
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

function withSharedCostAllocation(items, enabled) {
  if (!items?.length) {
    return items;
  }

  const originalTotal = items.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const sharedItem = items.find((item) => item.name === SHARED_COST_GROUP);
  const sharedValue = Number(sharedItem?.value || 0);
  const recipients = items.filter((item) => item.name !== SHARED_COST_GROUP);
  const recipientTotal = recipients.reduce((sum, item) => sum + Number(item.value || 0), 0);

  if (!enabled || !sharedItem || !sharedValue || !recipientTotal || !originalTotal) {
    return items.map((item) => ({
      ...item,
      interactive: item.name === SHARED_COST_GROUP,
    }));
  }

  return recipients.map((item) => {
    const originalValue = Number(item.value || 0);
    const value = originalValue + sharedValue * (originalValue / recipientTotal);
    return {
      ...item,
      value,
      share_pct: (value / originalTotal) * 100,
      interactive: false,
    };
  });
}

function formatDelta(value) {
  const numeric = Number(value || 0);
  const sign = numeric > 0 ? "+" : "";
  return `WoW ${sign}${formatPercent(numeric)}`;
}

function withBudgetSeries(series, granularity, enabled) {
  if (!series?.length) {
    return series;
  }
  if (!enabled) {
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
