import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { DashboardLayout } from "./components/layout";
import OverviewPage from "./pages/OverviewPage";
import BuildTrendPage from "./pages/BuildTrendPage";
import MigrateStatusPage from "./pages/MigrateStatusPage";
import FlakyPage from "./pages/FlakyPage";
import RuntimeInsightsPage from "./pages/RuntimeInsightsPage";
import CostPage from "./pages/CostPage";
import { buildScopeLabel, getDefaultDateRange, useApiData } from "./lib/api";

const REPO_OPTIONS = [{ value: "pingcap/tidb", label: "pingcap/tidb" }];
const BRANCH_OPTIONS = [
  { value: "main", label: "main" },
  { value: "master", label: "master" },
  { value: "release-8.5", label: "release-8.5" },
];
const CI_STATUS_PATH = "/ci-status";
const MIGRATE_STATUS_PATH = "/migrate-status";
const RUNTIME_INSIGHTS_PATH = "/runtime-insights";
const COST_PATH = "/cost";
const WEEK_GRANULARITY_PATHS = new Set([
  CI_STATUS_PATH,
  MIGRATE_STATUS_PATH,
  RUNTIME_INSIGHTS_PATH,
  COST_PATH,
]);

function buildDefaultFilters(defaultRange, pathname) {
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

export default function App() {
  const [defaultRange] = useState(() => getDefaultDateRange());
  const location = useLocation();
  const [filtersByPath, setFiltersByPath] = useState(() => ({
    [location.pathname]: buildDefaultFilters(defaultRange, location.pathname),
  }));
  const filters = filtersByPath[location.pathname] || buildDefaultFilters(defaultRange, location.pathname);
  const navigation = useApiData("/api/v1/pages/navigation");
  const runtimeInsightsEnabled = navigation.data?.features?.runtime_insights_enabled === true;
  const costDashboardEnabled = navigation.data?.features?.cost_dashboard_enabled === true;
  const runtimeInsightsReady = !navigation.loading;
  const costDashboardReady = !navigation.loading;
  const runtimeInsightsRoute = runtimeInsightsEnabled ? (
    <RuntimeInsightsPage filters={filters} />
  ) : runtimeInsightsReady ? (
    <Navigate to={CI_STATUS_PATH} replace />
  ) : (
    <div className="empty-state">Loading feature settings...</div>
  );
  const costDashboardRoute = costDashboardEnabled ? (
    <CostPage filters={filters} />
  ) : costDashboardReady ? (
    <Navigate to={CI_STATUS_PATH} replace />
  ) : (
    <div className="empty-state">Loading feature settings...</div>
  );

  useEffect(() => {
    setFiltersByPath((current) => {
      if (current[location.pathname]) {
        return current;
      }

      return {
        ...current,
        [location.pathname]: buildDefaultFilters(defaultRange, location.pathname),
      };
    });
  }, [defaultRange, location.pathname]);

  useEffect(() => {
    if (location.pathname !== "/flaky") {
      return;
    }

    setFiltersByPath((current) => {
      const routeFilters = current[location.pathname] || buildDefaultFilters(defaultRange, location.pathname);
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
  }, [defaultRange, location.pathname]);

  useEffect(() => {
    if (!WEEK_GRANULARITY_PATHS.has(location.pathname)) {
      return;
    }

    setFiltersByPath((current) => {
      const routeFilters = current[location.pathname] || buildDefaultFilters(defaultRange, location.pathname);
      if (routeFilters.granularity === "week") {
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
  }, [defaultRange, location.pathname]);

  const jobs = useApiData(
    "/api/v1/filters/jobs",
    {
      repo: filters.repo,
      branch: filters.branch,
      start_date: filters.start_date,
      end_date: filters.end_date,
    },
  );
  const cloudPhases = useApiData("/api/v1/filters/cloud-phases", {
    repo: filters.repo,
    branch: filters.branch,
    job_name: filters.job_name,
    start_date: filters.start_date,
    end_date: filters.end_date,
  });

  function handleFilterChange(key, value) {
    setFiltersByPath((current) => {
      const routeFilters = current[location.pathname] || buildDefaultFilters(defaultRange, location.pathname);
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

  const filterOptions = {
    repos: REPO_OPTIONS,
    branches: BRANCH_OPTIONS,
    jobs: jobs.data?.items || [],
    cloudPhases: cloudPhases.data?.items || [],
    scopeLabel: buildScopeLabel(filters),
  };

  return (
    <DashboardLayout
      filters={filters}
      onFilterChange={handleFilterChange}
      filterOptions={filterOptions}
      features={{ runtimeInsightsEnabled, costDashboardEnabled }}
    >
      <Routes>
        <Route path="/" element={<OverviewPage filters={filters} />} />
        <Route path={CI_STATUS_PATH} element={<BuildTrendPage filters={filters} />} />
        <Route path="/flaky" element={<FlakyPage filters={filters} />} />
        <Route path={MIGRATE_STATUS_PATH} element={<MigrateStatusPage filters={filters} />} />
        <Route path={RUNTIME_INSIGHTS_PATH} element={runtimeInsightsRoute} />
        <Route path={COST_PATH} element={costDashboardRoute} />
      </Routes>
    </DashboardLayout>
  );
}
