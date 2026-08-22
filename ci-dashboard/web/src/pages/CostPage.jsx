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

const COST_ALLOCATION_BASIS_OPTIONS = [
  { key: "current_attribution", label: "Native" },
  { key: "residual_allocated", label: "K8S allocated" },
  { key: "eq_allocated", label: "EQ allocated" },
  { key: "residual_eq_allocated", label: "K8S + EQ allocated" },
];
const COST_ALLOCATION_BASIS_LABELS = Object.fromEntries(
  COST_ALLOCATION_BASIS_OPTIONS.map(({ key, label }) => [key, label]),
);
const NO_OWNER_LABEL = "(no owner)";

export default function CostPage({ filters }) {
  const [costBreakdownGroupBy, setCostBreakdownGroupBy] = useState("owner");
  const [allocationBasis, setAllocationBasis] = useState("current_attribution");
  const [allocationNotice, setAllocationNotice] = useState("");
  const [costBreakdownDrilldown, setCostBreakdownDrilldown] = useState(null);
  const [selectedCostStackName, setSelectedCostStackName] = useState("");
  const [selectedResourceOwner, setSelectedResourceOwner] = useState(NO_OWNER_LABEL);
  const [unmatchedServiceName, setUnmatchedServiceName] = useState("");
  const [unmatchedSortBy, setUnmatchedSortBy] = useState("list_cost");
  const weeklyOverviewRange = getLaggedTrailingDateRange();
  const selectedCostSource = filters.cost_source || DEFAULT_COST_SOURCE;
  const selectedCostSourceLabel = formatCostSourceLabel(selectedCostSource);
  const selectedCostSourceValue =
    selectedCostSource === ALL_COST_SOURCES ? "" : selectedCostSource;
  const isAws7266SplitCostSource = selectedCostSource === "aws:946646677266";
  const netCostLabel = isAws7266SplitCostSource
    ? "Net cost (excluding credits)"
    : "Net cost";

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
    allocation_basis: allocationBasis,
  };
  const costStackFilters = {
    ...costFilters,
    ...costDrilldownFilters,
    group_by: effectiveCostBreakdownGroupBy,
    allocation_basis: allocationBasis,
  };
  const costShareFilters = {
    ...costFilters,
    ...costDrilldownFilters,
    dimension: effectiveCostBreakdownGroupBy,
    allocation_basis: allocationBasis,
  };
  const engineeringGroupFilters = {
    ...costFilters,
    allocation_basis: allocationBasis,
  };
  const unmatchedResourceFilters = {
    ...costFilters,
    owner: selectedResourceOwner,
    service_name: unmatchedServiceName,
    sort_by: unmatchedSortBy,
    allocation_basis: allocationBasis,
  };
  const weeklyOverview = useApiData("/api/v1/pages/cost-weekly-overview", weeklyOverviewFilters);
  const allocationOverview = useApiData("/api/v1/pages/cost-allocation-overview", costFilters);
  const trend = useApiData("/api/v1/pages/cost-trend", costTrendFilters);
  const costShare = useApiData("/api/v1/pages/cost-share", costShareFilters);
  const repoGroupStack = useApiData("/api/v1/pages/cost-repo-group-stack", costStackFilters);
  const engineeringGroupShare = useApiData(
    "/api/v1/pages/cost-engineering-group-share",
    engineeringGroupFilters,
  );
  const unmatchedResources = useApiData(
    "/api/v1/pages/cost-unmatched-resources",
    unmatchedResourceFilters,
  );
  const unattachedBlockVolumes = useApiData(
    "/api/v1/pages/cost-unattached-block-volumes",
    costFilters,
  );
  const summary = trend.data?.meta?.summary || {};
  const budgetHealth = weeklyOverview.data?.budget_health;
  const configuredAnnualBudget = Number(budgetHealth?.annual_budget || 0);
  const weeklyBudget = Number(budgetHealth?.weekly_budget || 0);
  const hasConfiguredBudget = configuredAnnualBudget > 0;
  const budgetPeriodLabel =
    budgetHealth?.budget_start_date && budgetHealth?.budget_end_date
      ? `${budgetHealth.budget_start_date}～${budgetHealth.budget_end_date}`
      : "Budget period unavailable";
  const activeCostBreakdownGroup = COST_BREAKDOWN_GROUPS.find(
    (group) => group.key === effectiveCostBreakdownGroupBy,
  ) || COST_BREAKDOWN_GROUPS[0];
  const parentCostBreakdownGroup = COST_BREAKDOWN_GROUPS.find(
    (group) => group.key === costBreakdownDrilldown?.parentGroup,
  );
  const canDrillDownCostBreakdown =
    Boolean(costBreakdownDrilldownTargetGroup) && !costBreakdownDrilldown;
  const isOwnerResourceDrilldown = effectiveCostBreakdownGroupBy === "owner";
  const costBreakdownSubtitle = costBreakdownDrilldown
    ? `${COST_ALLOCATION_BASIS_LABELS[allocationBasis]}: ${activeCostBreakdownGroup.label} share and bucketed stack under ${parentCostBreakdownGroup?.label || "parent"}: ${costBreakdownDrilldown.parentName}.`
    : `${COST_ALLOCATION_BASIS_LABELS[allocationBasis]}: share and bucketed stack grouped by ${activeCostBreakdownGroup.description}.`;
  const allocationOverviewMatchesFilters =
    allocationOverview.data?.scope?.cost_source ===
      (selectedCostSourceValue || null) &&
    allocationOverview.data?.scope?.start_date === costFilters.start_date &&
    allocationOverview.data?.scope?.end_date === costFilters.end_date;
  const hasCurrentAllocationOverview =
    allocationOverviewMatchesFilters &&
    allocationOverview.data?.is_available &&
    !allocationOverview.loading &&
    !allocationOverview.error;
  const showKubernetesAllocation =
    allocationOverview.loading ||
    Boolean(allocationOverview.error) ||
    hasCurrentAllocationOverview;
  const costShareItems = withCostBreakdownDrilldown(
    costShare.data?.items,
    canDrillDownCostBreakdown || isOwnerResourceDrilldown,
  );

  const selectResourceOwner = (item) => {
    setSelectedResourceOwner(item.name);
    setUnmatchedServiceName("");
  };

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

  const resetResourceOwner = () => {
    setSelectedResourceOwner(NO_OWNER_LABEL);
    setUnmatchedServiceName("");
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

  useEffect(() => {
    if (costShare.loading || costShare.error || costShare.responseKey !== JSON.stringify(costShareFilters)) {
      return;
    }
    setAllocationNotice(
      allocationBasis !== "current_attribution" &&
        costShare.data?.meta?.allocation_basis !== allocationBasis
        ? "This allocation is unavailable for the selected scope; showing native attribution."
        : "",
    );
  }, [
    allocationBasis,
    costShare.data?.meta?.allocation_basis,
    costShare.error,
    costShare.loading,
    costShare.responseKey,
    JSON.stringify(costShareFilters),
  ]);

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
              label={netCostLabel}
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
            subtitle="Groups above 1% of list cost."
            items={weeklyOverview.data?.level2_share?.items}
            totalValue={weeklyOverview.data?.level2_share?.meta?.total_list_cost}
            totalLabel="list cost"
            emptyMessage="No Level 2 group above 1% for the previous complete week."
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

      <section
        className={`stats-grid cost-summary-grid${
          showKubernetesAllocation ? " cost-summary-grid--with-allocation" : ""
        }`}
      >
        <StatCard
          label={netCostLabel}
          value={formatCurrency(summary.net_cost)}
          detail={
            isAws7266SplitCostSource
              ? null
              : "After credits in the selected window"
          }
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
              ? budgetPeriodLabel
              : "Budget not configured for the selected source"
          }
          tone="rose"
        />
        {showKubernetesAllocation ? (
          <div className="cost-allocation-slot">
            <section
              className="cost-allocation-overview stat-card stat-card--teal"
              title="K8S cards exclude control-plane costs with a matched owner; those costs remain in the standard owner cost view."
            >
              <span className="stat-card__label">K8S allocated cost</span>
              <strong className="stat-card__value">
                {allocationOverview.loading
                  ? "Loading..."
                  : allocationOverview.error
                    ? "Unavailable"
                    : formatCurrency(allocationOverview.data.workload_split_cost)}
              </strong>
              <div className="stat-card__meta">
                <span className="cost-allocation-overview__detail">
                  {allocationOverview.loading
                    ? "Loading Kubernetes allocation..."
                    : allocationOverview.error
                      ? `Could not load allocation: ${allocationOverview.error}`
                      : <>
                          <span>K8S unallocated cost</span>
                          <strong>{formatCurrency(allocationOverview.data.kubernetes_unallocated_cost)}</strong>
                        </>}
                </span>
              </div>
            </section>
          </div>
        ) : null}
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
            <CostAllocationBasisSelector
              value={allocationBasis}
              onChange={(nextBasis) => {
                setAllocationBasis(nextBasis);
                setAllocationNotice("");
                setSelectedCostStackName("");
              }}
            />
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
        {allocationNotice ? <p className="panel-notice">{allocationNotice}</p> : null}
        <div className="cost-breakdown-grid">
          <DonutShareChart
            className="cost-share-donut"
            title={`${activeCostBreakdownGroup.label} share${costBreakdownDrilldownTitleSuffix}`}
            items={costShareItems}
            totalValue={costShare.data?.meta?.total_list_cost}
            totalLabel="list cost"
            emptyMessage="No cost share data for the current filters."
            onItemSelect={
              isOwnerResourceDrilldown
                ? selectResourceOwner
                : canDrillDownCostBreakdown
                  ? startCostBreakdownDrilldown
                  : undefined
            }
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
        title={`Resource breakdown: ${selectedResourceOwner}`}
        subtitle="Top 10 billable resource rows for the selected Owner share segment, with their available labels."
        loading={unmatchedResources.loading}
        error={unmatchedResources.error}
        actions={
          <>
            <UnmatchedResourcesControls
              serviceName={unmatchedServiceName}
              serviceOptions={unmatchedResources.data?.meta?.services}
              sortBy={unmatchedSortBy}
              onServiceChange={setUnmatchedServiceName}
              onSortChange={setUnmatchedSortBy}
            />
            {selectedResourceOwner !== NO_OWNER_LABEL ? (
              <button
                type="button"
                className="donut-card__action"
                onClick={resetResourceOwner}
              >
                Reset owner
              </button>
            ) : null}
          </>
        }
      >
        <UnmatchedResourceTable items={unmatchedResources.data?.items} />
      </Panel>

      <Panel
        title="Engineering Group allocation"
        subtitle={`${COST_ALLOCATION_BASIS_LABELS[allocationBasis]}: list cost share under Engineering Group, split once by direct child groups and once by second-level groups.`}
        loading={engineeringGroupShare.loading}
        error={engineeringGroupShare.error}
        actions={
          <CostAllocationBasisSelector
            value={allocationBasis}
            onChange={(nextBasis) => {
              setAllocationBasis(nextBasis);
              setAllocationNotice("");
              setSelectedCostStackName("");
            }}
          />
        }
      >
        {allocationNotice ? <p className="panel-notice">{allocationNotice}</p> : null}
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

function CostAllocationBasisSelector({ value, onChange }) {
  return (
    <SegmentedControl
      ariaLabel="Cost allocation basis"
      options={COST_ALLOCATION_BASIS_OPTIONS}
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
          const interactive = item.interactive !== false;
          return (
            <button
              key={item.name}
              type="button"
              className={buildDimensionChipClassName(selectedName === item.name)}
              title={`${item.name}: ${formatCurrency(item.value)} (${formatPercent(sharePct)})`}
              disabled={!interactive}
              onClick={() => {
                if (!interactive) {
                  return;
                }
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

function withCostBreakdownDrilldown(items, enabled) {
  if (!enabled || !items?.length) {
    return items;
  }

  return items.map((item) => ({
    ...item,
    interactive: item.name !== "Others",
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
