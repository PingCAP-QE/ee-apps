import { useEffect, useState } from "react";

import {
  formatCostSourceLabel,
  formatCompactCurrency,
  formatCurrency,
  formatPercent,
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
  ResourceBreakdownTable,
} from "../components/charts";
import { SegmentedControl, buildDimensionChipClassName } from "../components/controls";

const NO_OWNER_LABEL = "(no owner)";

export default function CostPage({ filters }) {
  const [costBreakdownGroupBy, setCostBreakdownGroupBy] = useState("owner");
  const [costBreakdownDrilldown, setCostBreakdownDrilldown] = useState(null);
  const [selectedCostStackName, setSelectedCostStackName] = useState("");
  const [resourceScope, setResourceScope] = useState({
    dimension: "owner",
    value: NO_OWNER_LABEL,
  });
  const [resourceBreakdownRequested, setResourceBreakdownRequested] = useState(false);
  const [unmatchedServiceName, setUnmatchedServiceName] = useState("");
  const [unmatchedSortBy, setUnmatchedSortBy] = useState("list_cost");
  const [resourceCursor, setResourceCursor] = useState(null);
  const [resourceItems, setResourceItems] = useState([]);
  const selectedCostSource = filters.cost_source || DEFAULT_COST_SOURCE;
  const selectedCostSourceLabel = formatCostSourceLabel(selectedCostSource);
  const selectedCostSourceValue =
    selectedCostSource === ALL_COST_SOURCES ? "" : selectedCostSource;
  const isAws7266SplitCostSource = selectedCostSource === "aws:946646677266";
  const netCostLabel = isAws7266SplitCostSource
    ? "Net cost (excluding credits)"
    : "Net cost";

  const budgetPaceFilters = { cost_source: selectedCostSourceValue };
  const costFilters = {
    start_date: filters.start_date,
    end_date: filters.end_date,
    granularity: filters.granularity === "month" ? "month" : "week",
    cost_source: selectedCostSourceValue,
    branch: filters.branch,
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
  const engineeringGroupFilters = {
    ...costFilters,
  };
  const resourceBreakdownScope = {
    ...costFilters,
    ...(resourceScope.owner
      ? {
          owner: resourceScope.owner,
          scope_dimension: resourceScope.dimension,
          scope_value: resourceScope.value,
        }
      : resourceScope.dimension === "owner"
        ? { owner: resourceScope.value }
        : {
            scope_dimension: resourceScope.dimension,
            scope_value: resourceScope.value,
          }),
    service_name: unmatchedServiceName,
    sort_by: unmatchedSortBy,
  };
  const resourceBreakdownScopeKey = JSON.stringify(resourceBreakdownScope);
  const unmatchedResourceFilters = {
    ...resourceBreakdownScope,
    cursor: resourceCursor,
  };
  const unmatchedResourceRequestKey = JSON.stringify(unmatchedResourceFilters);
  const budgetPace = useApiData("/api/v1/pages/cost-budget-pace", budgetPaceFilters);
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
    resourceBreakdownRequested,
  );
  const summary = trend.data?.meta?.summary || {};
  const budgetHealth = budgetPace.data?.budget_health;
  const configuredAnnualBudget = Number(budgetHealth?.annual_budget || 0);
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
  const isResourceScopeGroup = ["owner", "team", "project"].includes(
    effectiveCostBreakdownGroupBy,
  );
  const costBreakdownSubtitle = costBreakdownDrilldown
    ? `${activeCostBreakdownGroup.label} share and bucketed stack under ${parentCostBreakdownGroup?.label || "parent"}: ${costBreakdownDrilldown.parentName}.`
    : `Share and bucketed stack grouped by ${activeCostBreakdownGroup.description}.`;
  const costShareItems = withCostBreakdownDrilldown(
    costShare.data?.items,
    canDrillDownCostBreakdown || isResourceScopeGroup,
  );

  const selectResourceScope = (dimension, item) => {
    const teamOwnerDrilldown =
      dimension === "owner" && costBreakdownDrilldown?.parentGroup === "team";
    setResourceScope(
      teamOwnerDrilldown
        ? {
            dimension: "team",
            value: costBreakdownDrilldown.parentName,
            owner: item.name,
          }
        : { dimension, value: item.name },
    );
    setResourceBreakdownRequested(true);
    setUnmatchedServiceName("");
    setResourceCursor(null);
    setResourceItems([]);
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
    if (costBreakdownGroupBy === "team") {
      setResourceScope({ dimension: "team", value: item.name });
      setResourceBreakdownRequested(true);
      setUnmatchedServiceName("");
      setResourceCursor(null);
      setResourceItems([]);
    }
    setSelectedCostStackName("");
  };

  const resetCostBreakdownDrilldown = () => {
    setCostBreakdownDrilldown(null);
    setSelectedCostStackName("");
  };

  const resetResourceScope = () => {
    setResourceScope({ dimension: "owner", value: NO_OWNER_LABEL });
    setUnmatchedServiceName("");
    setResourceCursor(null);
    setResourceItems([]);
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
    setResourceCursor(null);
    setResourceItems([]);
  }, [resourceBreakdownScopeKey]);

  useEffect(() => {
    if (unmatchedResources.responseKey !== unmatchedResourceRequestKey) {
      return;
    }
    if (unmatchedResources.data?.meta?.pending_dates?.length) {
      setResourceItems([]);
      return;
    }
    setResourceItems((current) => (
      resourceCursor ? [...current, ...(unmatchedResources.data?.items || [])] : (unmatchedResources.data?.items || [])
    ));
  }, [
    resourceCursor,
    unmatchedResourceRequestKey,
    unmatchedResources.data,
    unmatchedResources.responseKey,
  ]);

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
        title="Budget pace"
        subtitle="Observed fiscal-period net cost, a lag-adjusted checkpoint, and a period-end forecast from the prior 14 observed days."
        loading={budgetPace.loading}
        error={budgetPace.error}
        className="cost-budget-pace"
      >
        <BudgetHealthGauge
          title="Fiscal-period forecast"
          data={budgetHealth}
          emptyMessage="Budget pace is not configured for this source yet."
        />
      </Panel>
      <section
        className="stats-grid cost-summary-grid"
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
                setResourceScope({ dimension: "owner", value: NO_OWNER_LABEL });
                setResourceBreakdownRequested(false);
                setUnmatchedServiceName("");
                setResourceCursor(null);
                setResourceItems([]);
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
            onItemSelect={
              canDrillDownCostBreakdown
                ? startCostBreakdownDrilldown
                : isResourceScopeGroup
                  ? (item) => selectResourceScope(effectiveCostBreakdownGroupBy, item)
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
        title={`Resource breakdown: ${resourceScope.owner || resourceScope.value}`}
        subtitle={
          resourceBreakdownRequested
            ? "Complete resource list for the selected Cost breakdown segment."
            : "Load resource details for the selected Cost breakdown segment on demand."
        }
        loading={unmatchedResources.loading}
        error={unmatchedResources.error}
        actions={
          resourceBreakdownRequested ? (
            <>
              <UnmatchedResourcesControls
                serviceName={unmatchedServiceName}
                serviceOptions={unmatchedResources.data?.meta?.services}
                sortBy={unmatchedSortBy}
                onServiceChange={(value) => {
                  setUnmatchedServiceName(value);
                  setResourceCursor(null);
                  setResourceItems([]);
                }}
                onSortChange={(value) => {
                  setUnmatchedSortBy(value);
                  setResourceCursor(null);
                  setResourceItems([]);
                }}
              />
              {resourceScope.dimension !== "owner" || resourceScope.value !== NO_OWNER_LABEL ? (
                <button
                  type="button"
                  className="donut-card__action"
                  onClick={resetResourceScope}
                >
                  Reset scope
                </button>
              ) : null}
            </>
          ) : null
        }
      >
        {resourceBreakdownRequested ? (
          unmatchedResources.data?.meta?.pending_dates?.length ? (
            <div className="empty-state">
              Resource data is unavailable for {unmatchedResources.data.meta.pending_dates.join(", ")}.{" "}
              <a href="https://github.com/PingCAP-QE/ee-apps/tree/main/cost-insight#billing-summary-pipeline">
                Refresh the resource-serving projection
              </a>{" "}
              before retrying.
            </div>
          ) : (
            <>
              <ResourceBreakdownTable items={resourceItems} />
              {unmatchedResources.data?.meta?.next_cursor ? (
                <button
                  type="button"
                  className="donut-card__action"
                  onClick={() => setResourceCursor(unmatchedResources.data.meta.next_cursor)}
                >
                  Load more
                </button>
              ) : null}
            </>
          )
        ) : (
          <button
            type="button"
            className="donut-card__action"
            onClick={() => setResourceBreakdownRequested(true)}
          >
            Load resource breakdown
          </button>
        )}
      </Panel>

      <section className="cost-analysis-grid">
        <Panel
          title="Engineering Group cost share"
          subtitle="List cost share under Engineering Group, split once by direct child groups and once by second-level groups."
          loading={engineeringGroupShare.loading}
          error={engineeringGroupShare.error}
        >
          <div className="donut-grid">
            <DonutShareChart
              className="engineering-group-share__chart"
              title="Level 1 groups"
              subtitle="Direct children under Engineering Group."
              items={engineeringGroupShare.data?.level1?.items}
              totalLabel="list cost"
              emptyMessage="No Engineering Group level-1 cost share data yet."
            />
            <DonutShareChart
              className="engineering-group-share__chart"
              title="Level 2 groups"
              subtitle="Second-level teams under Engineering Group."
              items={engineeringGroupShare.data?.level2?.items}
              totalLabel="list cost"
              emptyMessage="No Engineering Group level-2 cost share data yet."
            />
          </div>
        </Panel>

        <Panel
          title="Budget pace"
          subtitle="Observed fiscal-period net cost, a lag-adjusted checkpoint, and a period-end forecast from the prior 14 observed days."
          loading={budgetPace.loading}
          error={budgetPace.error}
          className="cost-budget-pace"
        >
          <BudgetHealthGauge
            title="Fiscal-period forecast"
            data={budgetHealth}
            emptyMessage="Budget pace is not configured for this source yet."
          />
        </Panel>
      </section>

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
