-- Keep report-time rankings window-bounded even when repo or branch is unset.
-- Apply before deploying /api/v1/flaky/top-cases; existing indexes put repo,
-- branch or build key before report_time and cannot cover every filter scope.
CREATE INDEX IF NOT EXISTS idx_problem_case_runs_flaky_report_time
  ON problem_case_runs (flaky, report_time);
