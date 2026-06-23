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
  WEEK_GRANULARITY_PATHS,
} from "./lib/filterUrl";

const REPO_OPTIONS = [
  { value: "pingcap/tidb", label: "pingcap/tidb" },
  { value: "tikv/pd", label: "tikv/pd" },
];

export default function App() {
  const [defaultRange] = useState(() => getDefaultDateRange());
  const location = useLocation();
  const navigate = useNavigate();
  const [filtersByPath, setFiltersByPath] = useState(() => ({
    [location.pathname]: readFiltersFromSearch(defaultRange, location.pathname, location.search),
  }));
  const filters = filtersByPath[location.pathname]
    || readFiltersFromSearch(defaultRange, location.pathname, location.search);
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

  useEffect(() => {
    if (location.pathname !== "/flaky") {
      return;
    }

    setFiltersByPath((current) => {
      const routeFilters = current[location.pathname]
        || readFiltersFromSearch(defaultRange, location.pathname, location.search);
      if (routeFilters.repo || routeFilters.branch || routeFilters.issue_status) {
        return current;
      }

      return {
        ...current,
        [location.pathname]: {
          ...routeFilters,
          repo: "pingcap/tidb",
          branch: "master",
          issue_status: "closed",
        },
      };
    });
  }, [defaultRange, location.pathname, location.search]);

  useEffect(() => {
    if (!WEEK_GRANULARITY_PATHS.has(location.pathname)) {
      return;
    }

    setFiltersByPath((current) => {
      const routeFilters = current[location.pathname]
        || readFiltersFromSearch(defaultRange, location.pathname, location.search);
      const hasValidGranularity = location.pathname === COST_PATH
        ? routeFilters.granularity === "week" || routeFilters.granularity === "month"
        : routeFilters.granularity === "week";
      if (hasValidGranularity) {
        return current;
      }

      return {
        ...current,
        [location.pathname]: {
          ...routeFilters,
          granularity: "week",
        },
      };
    });
  }, [defaultRange, location.pathname, location.search]);

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
    setFiltersByPath((current) => {
      const routeFilters = current[location.pathname]
        || readFiltersFromSearch(defaultRange, location.pathname, location.search);
      if (key === "repo") {
        return {
          ...current,
          [location.pathname]: {
            ...routeFilters,
            repo: value,
            branch: "",
            job_name: "",
          },
        };
      }
      if (key === "branch") {
        return {
          ...current,
          [location.pathname]: {
            ...routeFilters,
            branch: value,
            job_name: "",
          },
        };
      }
      return {
        ...current,
        [location.pathname]: {
          ...routeFilters,
          [key]: value,
        },
      };
    });
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
