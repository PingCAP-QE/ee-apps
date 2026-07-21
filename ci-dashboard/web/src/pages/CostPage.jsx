import { useEffect, useState } from "react";

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
import { SegmentedControl, buildDimensionChipClassName } from "../components/controls";

const SHARED_COST_GROUP = "Efficiency & Quality";

export default function CostPage({ filters }) {
  const [isWeeklyLevel2Shared, setIsWeeklyLevel2Shared] = useState(false);
  const [isSelectedLevel2Shared, setIsSelectedLevel2Shared] = useState(false);
  const [costBreakdownGroupBy, setCostBreakdownGroupBy] = useState("owner");
  const [selectedCostStackName, setSelectedCostStackName] = useState("");
  const weeklyOverviewRange = getLaggedTrailingDateRange();
  const selectedCostSource = filters.cost_source || DEFAULT_COST_SOURCE;
  const selectedCostSourceLabel = formatCostSourceLabel(selectedCostSource);
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
  const costStackFilters = {
    ...costFilters,
    group_by: costBreakdownGroupBy,
  };
  const costShareFilters = {
    ...costFilters,
    dimension: costBreakdownGroupBy,
  };
  const regionShareFilters = {
    ...costFilters,
    dimension: "region",
  };
  const weeklyOverview = useApiData("/api/v1/pages/cost-weekly-overview", weeklyOverviewFilters);
  const trend = useApiData("/api/v1/pages/cost-trend", costFilters);
  const costShare = useApiData("/api/v1/pages/cost-share", costShareFilters);
  const regionShare = useApiData("/api/v1/pages/cost-share", regionShareFilters);
  const repoGroupStack = useApiData("/api/v1/pages/cost-repo-group-stack", costStackFilters);
  const engineeringGroupShare = useApiData("/api/v1/pages/cost-engineering-group-share", costFilters);
  const unmatchedResources = useApiData("/api/v1/pages/cost-unmatched-resources", costFilters);
  const summary = trend.data?.meta?.summary || {};
  const configuredAnnualBudget = Number(weeklyOverview.data?.budget_health?.annual_budget || 0);
  const weeklyBudget = Number(weeklyOverview.data?.budget_health?.weekly_budget || 0);
  const hasConfiguredBudget = configuredAnnualBudget > 0;
  const costTrendSeries = withBudgetSeries(
    trend.data?.series,
    costFilters.granularity,
    trend.data?.meta?.budget_targets,
  );
  const activeCostBreakdownGroup = COST_BREAKDOWN_GROUPS.find(
    (group) => group.key === costBreakdownGroupBy,
  ) || COST_BREAKDOWN_GROUPS[0];
  const weeklyLevel2Items = withSharedCostAllocation(
    weeklyOverview.data?.level2_share?.items,
    isWeeklyLevel2Shared,
  );
  const selectedLevel2Items = withSharedCostAllocation(
    engineeringGroupShare.data?.level2?.items,
    isSelectedLevel2Shared,
  );

  useEffect(() => {
    if (!selectedCostStackName) {
      return;
    }
    if (!hasCostStackItem(repoGroupStack.data?.items, selectedCostStackName)) {
      setSelectedCostStackName("");
    }
  }, [repoGroupStack.data?.items, selectedCostStackName]);

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
                hasConfiguredBudget
                  ? `Weekly budget ${formatCurrency(weeklyBudget)}`
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
            subtitle="Observed fiscal-period net cost, a lag-adjusted checkpoint, and a period-end forecast from the prior 14 observed days."
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
          label="Employee matched rate"
          value={formatPercent(summary.matched_resource_pct)}
          detail={`${formatCurrency(summary.matched_resource_cost)} / ${formatCurrency(summary.total_resource_cost)} list cost matched by author or owner email`}
          tone="amber"
        />
        <StatCard
          label="Fiscal budget"
          value={hasConfiguredBudget ? formatCurrency(configuredAnnualBudget) : "--"}
          detail={
            hasConfiguredBudget
              ? "Configured budget for the selected fiscal period"
              : "Budget not configured for the selected source"
          }
          tone="rose"
        />
      </section>

      <div className="cost-analysis-grid">
        <Panel
          title="Cost breakdown (list cost)"
          subtitle={`Share and bucketed stack grouped by ${activeCostBreakdownGroup.description}.`}
          loading={costShare.loading || regionShare.loading || repoGroupStack.loading}
          error={costShare.error || regionShare.error || repoGroupStack.error}
          className="cost-breakdown-panel"
          actions={
            <CostBreakdownGroupSelector
              value={costBreakdownGroupBy}
              onChange={(nextGroup) => {
                setCostBreakdownGroupBy(nextGroup);
                setSelectedCostStackName("");
              }}
            />
          }
        >
          <div className="cost-breakdown-grid">
            <DonutShareChart
              className="cost-share-donut"
              title={`${activeCostBreakdownGroup.label} share`}
              items={costShare.data?.items}
              totalValue={costShare.data?.meta?.total_list_cost}
              totalLabel="list cost"
              emptyMessage="No cost share data for the current filters."
            />
            <DonutShareChart
              className="cost-share-donut cost-region-donut"
              title="Region share"
              items={regionShare.data?.items}
              totalValue={regionShare.data?.meta?.total_list_cost}
              totalLabel="list cost"
              emptyMessage="No region cost data for the current filters."
            />
            <article className="cost-stack-card">
              <header className="donut-card__header">
                <div>
                  <strong>Cost stack</strong>
                </div>
              </header>
              <CostStackTrend
                data={repoGroupStack.data}
                selectedName={selectedCostStackName}
                onSelect={setSelectedCostStackName}
              />
            </article>
          </div>
        </Panel>

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

const COST_BREAKDOWN_GROUPS = [
  { key: "owner", label: "Owner", description: "owners" },
  { key: "team", label: "Team", description: "teams" },
  { key: "service", label: "SKU", description: "SKUs" },
  { key: "cost_driver", label: "Driver", description: "cost drivers" },
  { key: "project", label: "Project", description: "projects" },
  { key: "service_exec_id", label: "Exec ID", description: "service exec IDs" },
];

function CostBreakdownGroupSelector({ value, onChange }) {
  return (
    <SegmentedControl
      ariaLabel="Cost breakdown grouping"
      options={COST_BREAKDOWN_GROUPS}
      value={value}
      onChange={onChange}
    />
  );
}

function CostStackTrend({ data, selectedName, onSelect }) {
  const items = data?.items || [];
  const totalValue = items.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const series = selectedName
    ? (data?.series || []).filter((item) => item.label === selectedName)
    : data?.series;

  if (!items.length || !series?.length) {
    return <div className="empty-state">No cost stack data for the current filters.</div>;
  }

  return (
    <div className="build-count-breakdown">
      <TrendChart
        series={series}
        yFormatter={formatCompactCurrency}
        height={300}
        compactY
        stackBars={!selectedName}
        yTickMode="thousands-rounded"
        yTickSegments={5}
        barGroupWidthFactor={0.56}
        barMaxWidth={58}
        showTooltipSum={!selectedName}
      />
      <div className="dimension-selector" aria-label="Cost stack value selector">
        <button
          type="button"
          className={buildDimensionChipClassName(!selectedName)}
          onClick={() => onSelect("")}
        >
          All
        </button>
        {items.map((item) => {
          const sharePct = totalValue
            ? (Number(item.value || 0) / totalValue) * 100
            : 0;
          return (
            <button
              key={item.name}
              type="button"
              className={buildDimensionChipClassName(selectedName === item.name)}
              title={`${item.name}: ${formatCurrency(item.value)} (${formatPercent(sharePct)})`}
              onClick={() => onSelect(selectedName === item.name ? "" : item.name)}
            >
              <span>{item.name}</span>
              <strong>{formatCompactCurrency(item.value)}</strong>
              <small>{formatPercent(sharePct)}</small>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function hasCostStackItem(items, name) {
  return (items || []).some((item) => item.name === name);
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

function withBudgetSeries(series, granularity, budgetTargets) {
  if (!series?.length) {
    return series;
  }
  const targetsByBucket =
    budgetTargets && typeof budgetTargets === "object" ? budgetTargets : {};
  if (!Object.keys(targetsByBucket).length) {
    return series;
  }
  const labels = Array.from(
    new Set(series.flatMap((item) => item.points.map((point) => point[0]))),
  ).sort();
  const budgetPoints = labels.map((label) => {
    const budgetTarget = Number(targetsByBucket[label] || 0);
    if (!budgetTarget) {
      return [label, null];
    }
    return [label, budgetTarget];
  });
  if (!budgetPoints.some(([, value]) => value != null)) {
    return series;
  }

  return [
    ...series,
    {
      key: "budget_target",
      label: granularity === "month" ? "Monthly budget" : "Weekly budget",
      type: "line",
      dash: true,
      showPoints: false,
      points: budgetPoints,
    },
  ];
}
