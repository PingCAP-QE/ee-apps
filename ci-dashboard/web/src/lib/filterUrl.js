export const CI_STATUS_PATH = "/ci-status";
export const MIGRATE_STATUS_PATH = "/migrate-status";
export const RUNTIME_INSIGHTS_PATH = "/runtime-insights";
export const COST_PATH = "/cost";
export const FILTER_QUERY_KEYS = [
  "start_date",
  "end_date",
  "repo",
  "branch",
  "job_name",
  "cloud_phase",
  "issue_status",
  "granularity",
];
export const WEEK_GRANULARITY_PATHS = new Set([
  CI_STATUS_PATH,
  MIGRATE_STATUS_PATH,
  RUNTIME_INSIGHTS_PATH,
  COST_PATH,
]);
export const NAV_PATHS = [
  "/",
  CI_STATUS_PATH,
  "/flaky",
  MIGRATE_STATUS_PATH,
  COST_PATH,
  RUNTIME_INSIGHTS_PATH,
];

export function buildDefaultFilters(defaultRange, pathname) {
  const costRange =
    pathname === COST_PATH
      ? {
          start_date: defaultRange.end_date.slice(0, 8) + "01",
          end_date: defaultRange.end_date,
        }
      : defaultRange;
  const baseFilters = {
    repo: "",
    branch: "",
    job_name: "",
    cloud_phase: "",
    issue_status: "",
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

export function normalizeFiltersForPath(pathname, filters) {
  const next = { ...filters };
  const allowedGranularities = pathname === COST_PATH
    ? new Set(["week", "month"])
    : new Set(["day", "week", "month"]);
  if (!allowedGranularities.has(next.granularity)) {
    next.granularity = WEEK_GRANULARITY_PATHS.has(pathname) ? "week" : "day";
  }
  if (WEEK_GRANULARITY_PATHS.has(pathname) && pathname !== COST_PATH) {
    next.granularity = "week";
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
