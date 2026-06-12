CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_error_analysis_candidates
  ON ci_l1_builds (
    state,
    revise_error_l1_category,
    revise_error_l2_subcategory,
    error_l1_category,
    error_l2_subcategory,
    start_time DESC,
    id DESC,
    log_gcs_uri(16)
  );

CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_archive_candidates
  ON ci_l1_builds (
    build_system,
    state,
    start_time DESC,
    id DESC,
    log_gcs_uri(16)
  );

CREATE INDEX IF NOT EXISTS idx_prow_jobs_aborted_candidates
  ON prow_jobs (state, startTime DESC, prowJobId DESC);

CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_jenkins_job_samples
  ON ci_l1_builds (
    build_system,
    job_name,
    start_time DESC,
    id DESC,
    pod_name(16),
    normalized_build_url(16)
  );

CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_repo_pr_time_id
  ON ci_l1_builds (repo_full_name, pr_number, start_time DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_problem_case_runs_repo_branch_time_case
  ON problem_case_runs (repo, branch, report_time, case_name);

CREATE INDEX IF NOT EXISTS idx_ci_l1_pr_events_repo_pr_branch
  ON ci_l1_pr_events (repo, pr_number, target_branch);
