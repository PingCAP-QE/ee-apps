ALTER TABLE ci_l1_builds
  MODIFY COLUMN source_prow_row_id BIGINT NULL,
  MODIFY COLUMN source_prow_job_id CHAR(36) NULL,
  MODIFY COLUMN namespace VARCHAR(255) NULL,
  MODIFY COLUMN job_name VARCHAR(255) NULL,
  MODIFY COLUMN job_type VARCHAR(32) NULL,
  MODIFY COLUMN org VARCHAR(63) NULL,
  MODIFY COLUMN repo VARCHAR(63) NULL,
  MODIFY COLUMN repo_full_name VARCHAR(127) NULL,
  MODIFY COLUMN start_time DATETIME NULL;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS log_gcs_uri VARCHAR(512) NULL AFTER build_system;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS error_l1_category VARCHAR(32) NULL AFTER log_gcs_uri;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS error_l2_subcategory VARCHAR(64) NULL AFTER error_l1_category;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS revise_error_l1_category VARCHAR(32) NULL AFTER error_l2_subcategory;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS revise_error_l2_subcategory VARCHAR(64) NULL AFTER revise_error_l1_category;
