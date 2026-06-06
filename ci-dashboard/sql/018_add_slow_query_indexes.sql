CREATE INDEX IF NOT EXISTS idx_prow_jobs_id
  ON prow_jobs (id);

CREATE INDEX IF NOT EXISTS idx_problem_case_runs_flaky_repo_key_time
  ON problem_case_runs (flaky, repo, normalized_build_key(255), report_time);

CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_pr_job_start_id
  ON ci_l1_builds (repo_full_name, pr_number, job_name, start_time, id);

CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_error_review_state_prow
  ON ci_l1_builds (
    state,
    revise_error_l1_category,
    revise_error_l2_subcategory,
    source_prow_job_id
  );
