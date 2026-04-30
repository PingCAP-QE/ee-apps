import { useEffect, useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";

import { DashboardLayout } from "./components/layout";
import OverviewPage from "./pages/OverviewPage";
import BuildTrendPage from "./pages/BuildTrendPage";
import MigrateStatusPage from "./pages/MigrateStatusPage";
import FlakyPage from "./pages/FlakyPage";
import RuntimeInsightsPage from "./pages/RuntimeInsightsPage";
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
const WEEK_GRANULARITY_PATHS = new Set([CI_STATUS_PATH, MIGRATE_STATUS_PATH, RUNTIME_INSIGHTS_PATH]);

function buildDefaultFilters(defaultRange, pathname) {
  const baseFilters = {
    repo: "",
    branch: "",
    job_name: "",
    cloud_phase: "",
    issue_status: "",
    granularity: WEEK_GRANULARITY_PATHS.has(pathname) ? "week" : "day",
    start_date: defaultRange.start_date,
    end_date: defaultRange.end_date,
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
  const defaultRange = getDefaultDateRange();
  const location = useLocation();
  const [filters, setFilters] = useState(() => buildDefaultFilters(defaultRange, location.pathname));

  useEffect(() => {
    if (location.pathname !== "/flaky") {
      return;
    }

    setFilters((current) => {
      if (current.repo || current.branch || current.issue_status) {
        return current;
      }

      return {
        ...current,
        repo: "pingcap/tidb",
        branch: "master",
        issue_status: "closed",
      };
    });
  }, [location.pathname]);

  useEffect(() => {
    if (!WEEK_GRANULARITY_PATHS.has(location.pathname)) {
      return;
    }

    setFilters((current) => {
      if (current.granularity === "week") {
        return current;
      }

      return {
        ...current,
        granularity: "week",
      };
    });
  }, [location.pathname]);

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
    setFilters((current) => {
      if (key === "repo") {
        return {
          ...current,
          repo: value,
          branch: "",
          job_name: "",
        };
      }
      if (key === "branch") {
        return {
          ...current,
          branch: value,
          job_name: "",
        };
      }
      return {
        ...current,
        [key]: value,
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
    >
      <Routes>
        <Route path="/" element={<OverviewPage filters={filters} />} />
        <Route path={CI_STATUS_PATH} element={<BuildTrendPage filters={filters} />} />
        <Route path={MIGRATE_STATUS_PATH} element={<MigrateStatusPage filters={filters} />} />
        <Route path={RUNTIME_INSIGHTS_PATH} element={<RuntimeInsightsPage filters={filters} />} />
        <Route path="/flaky" element={<FlakyPage filters={filters} />} />
      </Routes>
    </DashboardLayout>
  );
}
