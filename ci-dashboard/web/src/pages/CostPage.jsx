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
  UnattachedBlockVolumeTable,
  UnmatchedResourceTable,
} from "../components/charts";
import { SegmentedControl, buildDimensionChipClassName } from "../components/controls";

const SHARED_COST_GROUP = "Efficiency & Quality";

export default function CostPage({ filters }) {
  const [isWeeklyLevel2Shared, setIsWeeklyLevel2Shared] = useState(false);
  const [isSelectedLevel2Shared, setIsSelectedLevel2Shared] = useState(false);
  const [costBreakdownGroupBy, setCostBreakdownGroupBy] = useState("owner");
  const [costBreakdownDrilldown, setCostBreakdownDrilldown] = useState(null);
  const [selectedCostStackName, setSelectedCostStackName] = useState("");
  const [unmatchedServiceName, setUnmatchedServiceName] = useState("");
  const [unmatchedSortBy, setUnmatchedSortBy] = useState("list_cost");
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
  const costBreakdownDrilldownTargetGroup =
    COST_BREAKDOWN_DRILLDOWN_GROUPS[costBreakdownGroupBy] || null;
  const costBreakdownDrilldownTitleSuffix = costBreakdownDrilldown
    ? `: ${costBreakdownDrilldown.parentName}`
    : "";
  const effectiveCostBreakdownGroupBy =
    costBreakdownDrilldown?.childGroup || costBreakdownGroupBy;
  const costDrilldownFilters = costBreakdownDrilldown
    ? {
        drilldown_group: costBreakdownDrilldown.parentGroup,
        drilldown_value: costBreakdownDrilldown.parentName,
      }
    : {};
  const costTrendFilters = {
    ...costFilters,
    ...costDrilldownFilters,
  };
  const costStackFilters = {
    ...costFilters,
    ...costDrilldownFilters,
    group_by: effectiveCostBreakdownGroupBy,
  };
  const costShareFilters = {
    ...costFilters,
    ...costDrilldownFilters,
    dimension: effectiveCostBreakdownGroupBy,
  };
  const unmatchedResourceFilters = {
    ...costFilters,
    service_name: unmatchedServiceName,
    sort_by: unmatchedSortBy,
  };
  const weeklyOverview = useApiData("/api/v1/pages/cost-weekly-overview", weeklyOverviewFilters);
  const trend = useApiData("/api/v1/pages/cost-trend", costTrendFilters);
  const costShare = useApiData("/api/v1/pages/cost-share", costShareFilters);
  const repoGroupStack = useApiData("/api/v1/pages/cost-repo-group-stack", costStackFilters);
  const engineeringGroupShare = useApiData("/api/v1/pages/cost-engineering-group-share", costFilters);
  const unmatchedResources = useApiData(
    "/api/v1/pages/cost-unmatched-resources",
    unmatchedResourceFilters,
  );
  const unattachedBlockVolumes = useApiData(
    "/api/v1/pages/cost-unattached-block-volumes",
    costFilters,
  );
  const summary = trend.data?.meta?.summary || {};
  const configuredAnnualBudget = Number(weeklyOverview.data?.budget_health?.annual_budget || 0);
  const weeklyBudget = Number(weeklyOverview.data?.budget_health?.weekly_budget || 0);
  const hasConfiguredBudget = configuredAnnualBudget > 0;
  const activeCostBreakdownGroup = COST_BREAKDOWN_GROUPS.find(
    (group) => group.key === effectiveCostBreakdownGroupBy,
  ) || COST_BREAKDOWN_GROUPS[0];
  const parentCostBreakdownGroup = COST_BREAKDOWN_GROUPS.find(
    (group) => group.key === costBreakdownDrilldown?.parentGroup,
  );
  const canDrillDownCostBreakdown =
    Boolean(costBreakdownDrilldownTargetGroup) && !costBreakdownDrilldown;
  const costBreakdownSubtitle = costBreakdownDrilldown
    ? `${activeCostBreakdownGroup.label} share and bucketed stack under ${parentCostBreakdownGroup?.label || "parent"}: ${costBreakdownDrilldown.parentName}.`
    : `Share and bucketed stack grouped by ${activeCostBreakdownGroup.description}.`;
  const weeklyLevel2Items = withSharedCostAllocation(
    weeklyOverview.data?.level2_share?.items,
    isWeeklyLevel2Shared,
  );
  const selectedLevel2Items = withSharedCostAllocation(
    engineeringGroupShare.data?.level2?.items,
    isSelectedLevel2Shared,
  );
  const costShareItems = withCostBreakdownDrilldown(
    costShare.data?.items,
    canDrillDownCostBreakdown,
  );

  const startCostBreakdownDrilldown = (item) => {
    if (!costBreakdownDrilldownTargetGroup) {
      return;
    }
    setCostBreakdownDrilldown({
      parentGroup: costBreakdownGroupBy,
      parentName: item.name,
      childGroup: costBreakdownDrilldownTargetGroup,
    });
    setSelectedCostStackName("");
  };

  const resetCostBreakdownDrilldown = () => {
    setCostBreakdownDrilldown(null);
    setSelectedCostStackName("");
  };

  useEffect(() => {
    if (!selectedCostStackName) {
      return;
    }
    if (!hasCostStackItem(repoGroupStack.data?.items, selectedCostStackName)) {
      setSelectedCostStackName("");
    }
  }, [repoGroupStack.data?.items, selectedCostStackName]);

  useEffect(() => {
    if (!unmatchedServiceName || !unmatchedResources.data?.meta?.services) {
      return;
    }
    if (
      !unmatchedResources.data.meta.services.some((item) => item.value === unmatchedServiceName)
    ) {
      setUnmatchedServiceName("");
    }
  }, [unmatchedResources.data?.meta?.services, unmatchedServiceName]);

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
            title="services rate"
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

      <Panel
        title="Cost breakdown (list cost)"
        subtitle={costBreakdownSubtitle}
        loading={
          costShare.loading || repoGroupStack.loading || trend.loading
        }
        error={costShare.error || repoGroupStack.error || trend.error}
        className="cost-breakdown-panel"
        actions={
          <>
            {costBreakdownDrilldown ? (
              <button
                type="button"
                className="donut-card__action"
                onClick={resetCostBreakdownDrilldown}
              >
                Back
              </button>
            ) : null}
            <CostBreakdownGroupSelector
              value={costBreakdownGroupBy}
              onChange={(nextGroup) => {
                setCostBreakdownGroupBy(nextGroup);
                setCostBreakdownDrilldown(null);
                setSelectedCostStackName("");
              }}
            />
          </>
        }
      >
        <div className="cost-breakdown-grid">
          <DonutShareChart
            className="cost-share-donut"
            title={`${activeCostBreakdownGroup.label} share${costBreakdownDrilldownTitleSuffix}`}
            items={costShareItems}
            totalValue={costShare.data?.meta?.total_list_cost}
            totalLabel="list cost"
            emptyMessage="No cost share data for the current filters."
            onItemSelect={canDrillDownCostBreakdown ? startCostBreakdownDrilldown : undefined}
          />
          <article className="cost-stack-card">
            <header className="donut-card__header">
              <div>
                <strong>Cost trend{costBreakdownDrilldownTitleSuffix}</strong>
              </div>
            </header>
            <CostStackTrend
              data={repoGroupStack.data}
              trendData={trend.data}
              granularity={costFilters.granularity}
              selectedName={selectedCostStackName}
              onSelect={setSelectedCostStackName}
              drilldownEnabled={canDrillDownCostBreakdown}
              onDrilldown={startCostBreakdownDrilldown}
              showComparisonLines={!costBreakdownDrilldown}
            />
          </article>
        </div>
      </Panel>

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
        subtitle="Top 20 named resources without an employee match and without GKE workload allocation."
        loading={unmatchedResources.loading}
        error={unmatchedResources.error}
        actions={
          <UnmatchedResourcesControls
            serviceName={unmatchedServiceName}
            serviceOptions={unmatchedResources.data?.meta?.services}
            sortBy={unmatchedSortBy}
            onServiceChange={setUnmatchedServiceName}
            onSortChange={setUnmatchedSortBy}
          />
        }
      >
        <UnmatchedResourceTable items={unmatchedResources.data?.items} />
      </Panel>

      <Panel
        title="Unattached Block Volumes"
        subtitle="AWS available EBS volumes and GCP Persistent Disk / Hyperdisk volumes with no users. Cost is shown when billing rows can be matched by volume id."
        loading={unattachedBlockVolumes.loading}
        error={unattachedBlockVolumes.error}
      >
        <UnattachedBlockVolumeTable items={unattachedBlockVolumes.data?.items} />
      </Panel>
    </div>
  );
}

const COST_BREAKDOWN_GROUPS = [
  { key: "owner", label: "Owner", description: "owners" },
  { key: "team", label: "Team", description: "teams" },
  { key: "sku", label: "SKU", description: "SKUs" },
  { key: "cost_driver", label: "SKU class", description: "SKU classes" },
  { key: "project", label: "Project", description: "projects" },
  { key: "region", label: "Region", description: "regions" },
  { key: "service_exec_id", label: "Exec ID", description: "service exec IDs" },
];

const COST_BREAKDOWN_DRILLDOWN_GROUPS = {
  team: "owner",
  cost_driver: "sku",
};

const UNMATCHED_RESOURCE_SORT_OPTIONS = [
  { key: "list_cost", label: "List cost" },
  { key: "duration", label: "Duration" },
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

function UnmatchedResourcesControls({
  serviceName,
  serviceOptions,
  sortBy,
  onServiceChange,
  onSortChange,
}) {
  return (
    <>
      <label className="panel-select-control">
        <span>Service</span>
        <select value={serviceName} onChange={(event) => onServiceChange(event.target.value)}>
          <option value="">All services</option>
          {(serviceOptions || []).map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <SegmentedControl
        ariaLabel="Unmatched resource sort"
        options={UNMATCHED_RESOURCE_SORT_OPTIONS}
        value={sortBy}
        onChange={onSortChange}
      />
    </>
  );
}

function CostStackTrend({
  data,
  trendData,
  granularity,
  selectedName,
  onSelect,
  drilldownEnabled = false,
  onDrilldown = null,
  showComparisonLines = true,
}) {
  const items = data?.items || [];
  const totalValue = items.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const baseSeries = selectedName
    ? (data?.series || []).filter((item) => item.label === selectedName)
    : data?.series;
  const series = selectedName
    ? baseSeries
    : showComparisonLines
      ? withCostComparisonLines(
          baseSeries,
          trendData?.series,
          granularity,
          trendData?.meta?.budget_targets,
        )
      : baseSeries;

  if (!items.length || !series?.length) {
    return <div className="empty-state">No cost stack data for the current filters.</div>;
  }

  return (
    <div className="build-count-breakdown">
      <TrendChart
        series={series}
        yFormatter={formatCompactCurrency}
        height={340}
        compactY
        stackBars={!selectedName}
        yTickMode="thousands-rounded"
        yTickSegments={5}
        barGroupWidthFactor={0.56}
        barMaxWidth={58}
        xLabelFormatter={granularity === "month" ? formatMonthAxisLabel : undefined}
        showTooltipSum={!selectedName}
      />
      <div className="dimension-selector" aria-label="Cost trend value selector">
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
              onClick={() => {
                if (drilldownEnabled && typeof onDrilldown === "function") {
                  onDrilldown(item);
                  return;
                }
                onSelect(selectedName === item.name ? "" : item.name);
              }}
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

function formatMonthAxisLabel(value) {
  const text = String(value || "");
  const match = text.match(/^\d{4}-(\d{2})(?:-\d{2})?$/);
  if (!match) {
    return text;
  }
  return match[1];
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

function withCostBreakdownDrilldown(items, enabled) {
  if (!enabled || !items?.length) {
    return items;
  }

  return items.map((item) => ({
    ...item,
    interactive: true,
  }));
}

function formatDelta(value) {
  const numeric = Number(value || 0);
  const sign = numeric > 0 ? "+" : "";
  return `WoW ${sign}${formatPercent(numeric)}`;
}

function withCostComparisonLines(baseSeries, trendSeries, granularity, budgetTargets) {
  if (!baseSeries?.length) {
    return baseSeries;
  }

  const labels = Array.from(
    new Set(baseSeries.flatMap((item) => item.points.map((point) => point[0]))),
  ).sort();
  const overlays = [];
  const netCostSeries = trendSeries?.find((item) => item.key === "net_cost");
  if (netCostSeries) {
    const netCostByBucket = new Map(netCostSeries.points);
    const netCostPoints = labels.map((label) => [
      label,
      netCostByBucket.has(label) ? netCostByBucket.get(label) : null,
    ]);
    if (netCostPoints.some(([, value]) => value != null)) {
      overlays.push({
        key: "net_cost",
        label: "Net cost",
        type: "line",
        points: netCostPoints,
      });
    }
  }

  const targetsByBucket =
    budgetTargets && typeof budgetTargets === "object" ? budgetTargets : {};
  if (!Object.keys(targetsByBucket).length) {
    return [...baseSeries, ...overlays];
  }
  const budgetPoints = labels.map((label) => {
    const budgetTarget = Number(targetsByBucket[label] || 0);
    if (!budgetTarget) {
      return [label, null];
    }
    return [label, budgetTarget];
  });
  if (!budgetPoints.some(([, value]) => value != null)) {
    return [...baseSeries, ...overlays];
  }

  return [
    ...baseSeries,
    ...overlays,
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
