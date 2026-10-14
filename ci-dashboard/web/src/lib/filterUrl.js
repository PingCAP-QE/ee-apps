export const CI_STATUS_PATH = "/ci-status";
export const MIGRATE_STATUS_PATH = "/migrate-status";
export const RUNTIME_INSIGHTS_PATH = "/runtime-insights";
export const COST_PATH = "/cost";
export const WEEKLY_COST_PATH = "/qa-cost-weekly";
export const CI_WEEKLY_COST_PATH = "/ci-cost-weekly";
export const ALL_COST_SOURCES = "all";
export const DEFAULT_COST_SOURCE = "gcp:pingcap-testing-account";
export const COST_DEFAULT_LAG_DAYS = 3;
export const FILTER_QUERY_KEYS = [
  "start_date",
  "end_date",
  "repo",
  "branch",
  "job_name",
  "cloud_phase",
  "issue_status",
  "cost_source",
  "owner_include",
  "owner_exclude",
  "team_include",
  "team_exclude",
  "project_include",
  "project_exclude",
  "granularity",
];
export const WEEK_GRANULARITY_PATHS = new Set([
  RUNTIME_INSIGHTS_PATH,
  COST_PATH,
]);
export const NAV_PATHS = [
  "/",
  CI_STATUS_PATH,
  "/flaky",
  MIGRATE_STATUS_PATH,
  COST_PATH,
  WEEKLY_COST_PATH,
  CI_WEEKLY_COST_PATH,
  RUNTIME_INSIGHTS_PATH,
];

export function buildDefaultFilters(defaultRange, pathname) {
  const costRange = pathname === COST_PATH ? getLaggedCostDefaultRange(defaultRange) : defaultRange;
  const baseFilters = {
    repo: "",
    branch: "",
    job_name: "",
    cloud_phase: "",
    issue_status: "",
    cost_source: pathname === COST_PATH ? DEFAULT_COST_SOURCE : "",
    owner_include: "",
    owner_exclude: "",
    team_include: "",
    team_exclude: "",
    project_include: "",
    project_exclude: "",
    granularity: WEEK_GRANULARITY_PATHS.has(pathname) ? "week" : "day",
    start_date: costRange.start_date,
    end_date: costRange.end_date,
  };

  if (pathname === "/flaky") {
    return {
      ...baseFilters,
      repo: "pingcap/tidb",
      branch: "master",
      issue_status: "closed",
    };
  }

  return baseFilters;
}

export function getCostBucketOptions({ start_date, end_date }) {
  const start = parseCostDate(start_date);
  const end = parseCostDate(end_date);
  const validRange = start && end && start <= end;
  const weekLimit = start && new Date(start);
  if (weekLimit) {
    weekLimit.setUTCMonth(start.getUTCMonth() + 2, 1);
    const monthEnd = new Date(weekLimit);
    monthEnd.setUTCMonth(monthEnd.getUTCMonth() + 1, 0);
    weekLimit.setUTCDate(Math.min(start.getUTCDate(), monthEnd.getUTCDate()));
  }
  const invalidRangeReason = "Select valid start and end dates, with Start on or before End.";
  const dayAllowed = Boolean(validRange && (end - start) / 86400000 + 1 <= 14);
  const weekAllowed = Boolean(validRange && end < weekLimit);

  return [
    {
      value: "day",
      label: "Day",
      disabled: !dayAllowed,
      reason: dayAllowed ? "" : validRange
        ? "Day requires at most 14 days, including both dates."
        : invalidRangeReason,
    },
    {
      value: "week",
      label: "Week",
      disabled: !weekAllowed,
      reason: weekAllowed ? "" : validRange
        ? `Week requires End before ${weekLimit.toISOString().slice(0, 10)} (Start + 2 calendar months).`
        : invalidRangeReason,
    },
    { value: "month", label: "Month", disabled: false, reason: "" },
  ];
}

function parseCostDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.getUTCFullYear() > 0
    && date.toISOString().slice(0, 10) === value ? date : null;
}

export function normalizeFiltersForPath(pathname, filters) {
  const next = { ...filters };
  const allowedGranularities = new Set(["day", "week", "month"]);
  if (!allowedGranularities.has(next.granularity)) {
    next.granularity = WEEK_GRANULARITY_PATHS.has(pathname) ? "week" : "day";
  }
  if (WEEK_GRANULARITY_PATHS.has(pathname) && pathname !== COST_PATH) {
    next.granularity = "week";
  }
  if (pathname === COST_PATH) {
    const latestCostDate = getLatestCostDate();
    if (parseCostDate(next.end_date) && next.end_date > latestCostDate) {
      next.end_date = latestCostDate;
    }
    const bucketOptions = getCostBucketOptions(next);
    const selectedIndex = bucketOptions.findIndex((option) => option.value === next.granularity);
    next.granularity = bucketOptions.slice(selectedIndex).find((option) => !option.disabled).value;
    next.repo = "";
    next.branch = "";
    next.job_name = "";
    next.cloud_phase = "";
    next.issue_status = "";
    next.cost_source = next.cost_source || DEFAULT_COST_SOURCE;
  } else {
    next.cost_source = "";
    next.owner_include = "";
    next.owner_exclude = "";
    next.team_include = "";
    next.team_exclude = "";
    next.project_include = "";
    next.project_exclude = "";
  }
  return next;
}

export function readFiltersFromSearch(defaultRange, pathname, search) {
  const params = new URLSearchParams(search);
  const next = buildDefaultFilters(defaultRange, pathname);
  FILTER_QUERY_KEYS.forEach((key) => {
    if (params.has(key)) {
      next[key] = params.get(key) || "";
    }
  });
  return normalizeFiltersForPath(pathname, next);
}

export function buildFilterSearch(filters, pathname) {
  if (pathname === "/" || pathname === WEEKLY_COST_PATH || pathname === CI_WEEKLY_COST_PATH) {
    return "";
  }
  const normalized = normalizeFiltersForPath(pathname, filters);
  const params = new URLSearchParams();
  FILTER_QUERY_KEYS.forEach((key) => {
    const value = normalized[key];
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  });
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function sameFilters(left, right) {
  return FILTER_QUERY_KEYS.every((key) => (left?.[key] || "") === (right?.[key] || ""));
}

export function buildNavSearchByPath(filtersByPath, defaultRange, currentFilters) {
  const activeRangeIsDefault =
    currentFilters.start_date === defaultRange.start_date && currentFilters.end_date === defaultRange.end_date;

  return NAV_PATHS.reduce((accumulator, pathname) => {
    const hasRouteFilters = Boolean(filtersByPath[pathname]);
    const routeFilters = filtersByPath[pathname] || buildDefaultFilters(defaultRange, pathname);
    const useLaggedCostDefault = pathname === COST_PATH && !hasRouteFilters && activeRangeIsDefault;
    return {
      ...accumulator,
      [pathname]: buildFilterSearch(
        {
          ...routeFilters,
          start_date: useLaggedCostDefault ? routeFilters.start_date : currentFilters.start_date,
          end_date: useLaggedCostDefault ? routeFilters.end_date : currentFilters.end_date,
        },
        pathname,
      ),
    };
  }, {});
}

export function getLatestCostDate(referenceDate = new Date()) {
  const end = new Date(referenceDate);
  end.setUTCDate(end.getUTCDate() - COST_DEFAULT_LAG_DAYS);
  return end.toISOString().slice(0, 10);
}

function getLaggedCostDefaultRange(defaultRange) {
  const endDate = getLatestCostDate(`${defaultRange.end_date}T00:00:00Z`);
  return {
    start_date: `${endDate.slice(0, 8)}01`,
    end_date: endDate,
  };
}
