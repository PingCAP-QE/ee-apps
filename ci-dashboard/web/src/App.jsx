import { useEffect, useState } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { DashboardLayout } from "./components/layout";
import WeeklySummaryPage from "./pages/WeeklySummaryPage";
import BuildTrendPage from "./pages/BuildTrendPage";
import MigrateStatusPage from "./pages/MigrateStatusPage";
import FlakyPage from "./pages/FlakyPage";
import RuntimeInsightsPage from "./pages/RuntimeInsightsPage";
import CostPage from "./pages/CostPage";
import {
  buildCostSourceOptions,
  buildScopeLabel,
  getDefaultDateRange,
  useApiData,
} from "./lib/api";
import {
  buildFilterSearch,
  buildNavSearchByPath,
  CI_STATUS_PATH,
  COST_PATH,
  DEFAULT_COST_SOURCE,
  MIGRATE_STATUS_PATH,
  readFiltersFromSearch,
  RUNTIME_INSIGHTS_PATH,
  sameFilters,
} from "./lib/filterUrl";

const REPO_OPTIONS = [
  { value: "pingcap/docs", label: "pingcap/docs" },
  { value: "pingcap/ticdc", label: "pingcap/ticdc" },
  { value: "pingcap/tidb", label: "pingcap/tidb" },
  { value: "pingcap/tiflash", label: "pingcap/tiflash" },
  { value: "pingcap/tiflow", label: "pingcap/tiflow" },
  { value: "tidbcloud/cloud-storage-engine", label: "tidbcloud/cloud-storage-engine" },
  { value: "tikv/pd", label: "tikv/pd" },
  { value: "tikv/tikv", label: "tikv/tikv" },
];

export default function App() {
  const [defaultRange] = useState(() => getDefaultDateRange());
  const location = useLocation();
  const navigate = useNavigate();
  // Remember route-specific selections only when building links to other dashboard tabs.
  const [filtersByPath, setFiltersByPath] = useState(() => ({
    [location.pathname]: readFiltersFromSearch(defaultRange, location.pathname, location.search),
  }));
  const filters = readFiltersFromSearch(
    defaultRange,
    location.pathname,
    location.search,
  );
  const isCostPage = location.pathname === COST_PATH;
  const isWeeklySummaryPage = location.pathname === "/";

  useEffect(() => {
    const urlFilters = readFiltersFromSearch(defaultRange, location.pathname, location.search);
    setFiltersByPath((current) => {
      if (sameFilters(current[location.pathname], urlFilters)) {
        return current;
      }

      return {
        ...current,
        [location.pathname]: urlFilters,
      };
    });
  }, [defaultRange, location.pathname, location.search]);

  // Canonicalize bookmarked and browser-history URLs. Local filter changes already
  // navigate to the same canonical search string, so this is a no-op for those changes.
  useEffect(() => {
    const nextSearch = buildFilterSearch(filters, location.pathname);
    if (nextSearch === location.search) {
      return;
    }
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch,
      },
      { replace: true },
    );
  }, [filters, location.pathname, location.search, navigate]);

  const jobs = useApiData(
    "/api/v1/filters/jobs",
    {
      repo: filters.repo,
      branch: filters.branch,
      start_date: filters.start_date,
      end_date: filters.end_date,
    },
    !isCostPage && !isWeeklySummaryPage,
  );
  const branches = useApiData(
    "/api/v1/filters/branches",
    {
      repo: filters.repo,
    },
    !isCostPage && !isWeeklySummaryPage,
  );
  const cloudPhases = useApiData("/api/v1/filters/cloud-phases", {
    repo: filters.repo,
    branch: filters.branch,
    job_name: filters.job_name,
    start_date: filters.start_date,
    end_date: filters.end_date,
  }, !isCostPage && !isWeeklySummaryPage);
  const costSources = useApiData(
    "/api/v1/pages/cost-sources",
    {},
    isCostPage,
  );
  const costSourceOptions = buildCostSourceOptions(
    costSources.data?.items,
    filters.cost_source || DEFAULT_COST_SOURCE,
  );
  const selectedCostSource = costSourceOptions.find(
    (item) => item.value === (filters.cost_source || DEFAULT_COST_SOURCE),
  ) || costSourceOptions[0];

  function handleFilterChange(key, value) {
    const nextFilters = {
      ...filters,
      [key]: value,
    };
    if (key === "repo") {
      nextFilters.branch = "";
      nextFilters.job_name = "";
    }
    if (key === "branch") {
      nextFilters.job_name = "";
    }
    navigate(
      {
        pathname: location.pathname,
        search: buildFilterSearch(nextFilters, location.pathname),
      },
      { replace: true },
    );
  }

  const navSearchByPath = buildNavSearchByPath(filtersByPath, defaultRange, filters);

  const filterOptions = {
    isCostPage,
    repos: REPO_OPTIONS,
    branches: branches.data?.items || [],
    jobs: jobs.data?.items || [],
    cloudPhases: cloudPhases.data?.items || [],
    costSources: costSourceOptions,
    scopeLabel: buildScopeLabel(filters, location.pathname, selectedCostSource?.label),
  };

  return (
    <DashboardLayout
      filters={filters}
      onFilterChange={handleFilterChange}
      filterOptions={filterOptions}
      navSearchByPath={navSearchByPath}
      showFilters={!isWeeklySummaryPage}
    >
      <Routes>
        <Route path="/" element={<WeeklySummaryPage />} />
        <Route path={CI_STATUS_PATH} element={<BuildTrendPage filters={filters} />} />
        <Route path="/flaky" element={<FlakyPage filters={filters} />} />
        <Route path={MIGRATE_STATUS_PATH} element={<MigrateStatusPage filters={filters} />} />
        <Route
          path={RUNTIME_INSIGHTS_PATH}
          element={<RuntimeInsightsPage filters={filters} />}
        />
        <Route path={COST_PATH} element={<CostPage filters={filters} />} />
      </Routes>
    </DashboardLayout>
  );
}
